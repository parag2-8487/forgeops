// SPDX-License-Identifier: Apache-2.0
package app

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"go.uber.org/zap"
	"golang.org/x/sync/errgroup"

	"github.com/parag8487/ForgeOps/agent/internal/config"
	"github.com/parag8487/ForgeOps/agent/internal/connection"
	dockerx "github.com/parag8487/ForgeOps/agent/internal/docker"
	"github.com/parag8487/ForgeOps/agent/internal/fileops"
	"github.com/parag8487/ForgeOps/agent/internal/git"
	"github.com/parag8487/ForgeOps/agent/internal/iac"
	k8sx "github.com/parag8487/ForgeOps/agent/internal/k8s"
	"github.com/parag8487/ForgeOps/agent/internal/logging"
	"github.com/parag8487/ForgeOps/agent/internal/mcp"
	"github.com/parag8487/ForgeOps/agent/internal/session"
	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

// BuildInfo holds version metadata injected at build time.
type BuildInfo struct {
	Version string
	Commit  string
	Date    string
}

// App is the top-level composition root. It owns all subsystems and coordinates
// graceful startup and shutdown via constructor injection.
type App struct {
	cfg       *config.Config
	bi        BuildInfo
	logger    *zap.Logger
	tracer    telemetry.Tracer
	files     fileops.Ops
	tofu      *iac.TofuRunner
	gitClient git.Client
	mcpSrv    *mcp.Server
	conn      *connection.Manager
	docker    *dockerx.Probe
	k8s       *k8sx.Probe
	closers   []namedCloser
	closeOnce sync.Once
	closeErr  error

	// The session manager is built on demand; see Session for why.
	sessionOnce sync.Once
	sessionMgr  *session.Manager
	sessionErr  error
}

type namedCloser struct {
	name string
	fn   func() error
}

// New constructs a fully-wired App from the validated config.
func New(cfg *config.Config, bi BuildInfo) (*App, error) {
	// design §7.2, §14.5: the agent has exactly ONE logger constructor, and it
	// redacts. `logging.New` produced an unfiltered logger, so any subsystem that
	// logged a value it had not thought about — a git remote with a token in the URL,
	// a validator echoing file content, an error wrapping a DSN — wrote it verbatim.
	// Q-24 forbids that, and a "remember to use the redacting one" rule is not a
	// mechanism. `app_wiring_test.go` asserts `logging.New` is unreachable from the
	// agent's own packages.
	//
	// The secret set is assembled from configuration rather than discovered: these are
	// the values the agent is GIVEN, so they are the ones it can name. Pattern-based
	// detection of values it was never told about is secretscan's job (task 10.1).
	logger, err := logging.NewRedacted(cfg.LogLevel, cfg.LogFormat, knownSecrets(cfg))
	if err != nil {
		return nil, fmt.Errorf("logger: %w", err)
	}

	tracer := telemetry.NoopTracer{}
	files := fileops.New()

	tofuCfg := iac.TofuConfig{
		BinaryPath:     cfg.Tofu.BinaryPath,
		DefaultTimeout: cfg.Tofu.DefaultTimeout,
		KillGrace:      cfg.Tofu.KillGrace,
		PluginCacheDir: cfg.Tofu.PluginCacheDir,
		ExtraEnvAllow:  cfg.Tofu.ExtraEnvAllow,
	}
	tofu := iac.NewTofuRunner(tofuCfg, logger.Named("tofu"), tracer)

	gitCfg := git.Config{
		GitHubAPIBaseURL: cfg.Git.APIBaseURL,
		Repo:             cfg.Git.Repo,
		AuthorName:       cfg.Git.AuthorName,
		AuthorEmail:      cfg.Git.AuthorEmail,
		BranchPrefix:     cfg.Git.BranchPrefix,
		PollInterval:     cfg.Git.PollInterval,
		PollTimeout:      cfg.Git.PollTimeout,
	}
	tokens := &git.EnvTokenSource{EnvVar: "GITHUB_TOKEN"}
	gitClient := git.NewClient(gitCfg, tokens, logger.Named("git"), tracer)

	mcpDeps := mcp.Deps{
		Logger: logger.Named("mcp"),
		Tracer: tracer,
		Tofu:   tofu,
		Files:  files,
	}
	mcpSrv := mcp.NewServer(mcpDeps, bi.Version)
	conn := connection.NewManager(cfg.BackendWSSURL, logger.Named("conn"), tracer)
	docker := dockerx.New(logger.Named("docker"))
	k8s := k8sx.New(logger.Named("k8s"))

	a := &App{
		cfg: cfg, bi: bi, logger: logger, tracer: tracer,
		files: files, tofu: tofu, gitClient: gitClient,
		mcpSrv: mcpSrv, conn: conn, docker: docker, k8s: k8s,
	}

	// Register closers in construction order; Close walks them in reverse.
	a.closers = []namedCloser{
		{"connection", conn.Close},
		{"mcp", mcpSrv.Close},
		{"logger", func() error { return logger.Sync() }},
	}

	return a, nil
}

// Run starts the agent subsystems and blocks until all finish or ctx is cancelled.
func (a *App) Run(ctx context.Context) error {
	g, gctx := errgroup.WithContext(ctx)
	g.Go(func() error { return a.mcpSrv.Serve(gctx) })
	g.Go(func() error { return a.conn.Serve(gctx) })

	err := g.Wait()
	if errors.Is(err, context.Canceled) || errors.Is(err, connection.ErrDisabled) {
		err = nil
	}
	return err
}

// Close performs graceful shutdown of all subsystems in reverse construction order.
//
// The whole sequence is bounded by cfg.ShutdownTimeout (P-07). A closer that
// blocks past the deadline is abandoned rather than allowed to hang the process:
// the goroutine is deliberately left running because the process is exiting.
func (a *App) Close() error {
	a.closeOnce.Do(func() {
		ctx, cancel := context.WithTimeout(context.Background(), a.cfg.ShutdownTimeout)
		defer cancel()

		done := make(chan error, 1)
		go func() {
			var errs error
			for i := len(a.closers) - 1; i >= 0; i-- {
				c := a.closers[i]
				if err := c.fn(); err != nil {
					errs = errors.Join(errs, fmt.Errorf("%s: %w", c.name, err))
				}
			}
			done <- errs
		}()

		select {
		case err := <-done:
			a.closeErr = err
		case <-ctx.Done():
			a.closeErr = fmt.Errorf("shutdown exceeded %s: %w", a.cfg.ShutdownTimeout, ctx.Err())
		}
	})
	return a.closeErr
}

// Accessors for CLI commands.
func (a *App) Config() *config.Config { return a.cfg }
func (a *App) BuildInfo() BuildInfo   { return a.bi }
func (a *App) Logger() *zap.Logger    { return a.logger }
func (a *App) Docker() *dockerx.Probe { return a.docker }
func (a *App) K8s() *k8sx.Probe       { return a.k8s }
func (a *App) Tofu() *iac.TofuRunner  { return a.tofu }
func (a *App) MCP() *mcp.Server       { return a.mcpSrv }

// Session returns the session manager, constructing it on first use.
//
// Lazy rather than wired in New, and the reason is not tidiness: `session.NewStore`
// probes the OS keychain by writing a marker and creates the state directory. Doing that
// during composition would make `forgeops-agent version` touch the user's credential
// manager, and would make every existing App construction test depend on a keychain being
// present. `pair` and `doctor` are the only commands that need it, and they are the only
// ones that pay for it.
//
// The error is memoised with the manager: a failed keychain probe fails the same way on
// every call, and retrying it per command would produce a different diagnosis for the
// same machine depending on which command ran.
func (a *App) Session() (*session.Manager, error) {
	a.sessionOnce.Do(func() {
		store, err := session.NewStore(a.cfg.Session.StateDir, a.cfg.Session.CredentialStore)
		if err != nil {
			a.sessionErr = err
			return
		}
		a.sessionMgr, a.sessionErr = session.NewManager(a.cfg.BackendWSSURL, session.Deps{
			Store:        store,
			Logger:       a.logger.Named("session"),
			AgentVersion: a.bi.Version,
		})
	})
	return a.sessionMgr, a.sessionErr
}
