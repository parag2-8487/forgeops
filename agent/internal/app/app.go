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
}

type namedCloser struct {
	name string
	fn   func() error
}

// New constructs a fully-wired App from the validated config.
func New(cfg *config.Config, bi BuildInfo) (*App, error) {
	logger, err := logging.New(cfg.LogLevel, cfg.LogFormat)
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
