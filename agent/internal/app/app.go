// SPDX-License-Identifier: Apache-2.0
package app

import (
	"context"
	"errors"
	"fmt"
	"io"
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
	// credentialStore is opened without a backend URL, so `doctor` can report on it even when
	// the agent is not configured to reach a backend at all.
	credentialStore     *session.FileStore
	credentialStoreOnce sync.Once
	credentialStoreErr  error
	sessionErr          error
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
		{"logger", func() error { return logging.Sync(logger) }},
	}

	return a, nil
}

// Run starts the agent subsystems and blocks until all finish or ctx is cancelled.
func (a *App) Run(ctx context.Context) error {
	g, gctx := errgroup.WithContext(ctx)

	// THE MCP STDIO TRANSPORT MUST NOT END THE AGENT, and until this it did.
	//
	// `mcpSrv.Serve` is the stdio transport: it reads stdin. Started as a plain errgroup member, a
	// closed stdin makes `stdio.Listen` return io.EOF, errgroup treats any non-nil error as fatal and
	// cancels gctx, `conn.Serve` unwinds with context.Canceled, and the check below then maps that to
	// nil. So the agent printed "agent starting" and exited 0 with nothing logged.
	//
	// That is exactly how `forgeops-agent run` is started in production and in the journey:
	// `docker compose exec -d`, which attaches no stdin. The agent therefore never held a WebSocket
	// session, never received a command, and an approved change set was never applied -- visible only
	// as a step timing out on "waiting for the change set to be applied". Holding stdin open with a
	// pipe kept the same binary running for over an hour, which is what identified this.
	//
	// A daemon whose lifetime depends on stdin being attached is the defect. `mcp-serve` exists for
	// the stdio use case; here the stdio transport ending is one transport becoming unavailable, not
	// the agent's work being over, so this goroutine stays until the group itself ends.
	g.Go(func() error {
		err := a.mcpSrv.Serve(gctx)
		switch {
		case err == nil, errors.Is(err, io.EOF), errors.Is(err, context.Canceled):
			if err != nil {
				a.logger.Info(
					"the MCP stdio transport ended; the agent continues on its WebSocket session",
					zap.String("reason", err.Error()),
				)
			}
			<-gctx.Done()
			return gctx.Err()
		default:
			return err
		}
	})
	g.Go(func() error { return a.serveSession(gctx) })

	err := g.Wait()
	if errors.Is(err, context.Canceled) || errors.Is(err, connection.ErrDisabled) {
		err = nil
	}
	return err
}

// serveSession runs the agent's real session: dial, handshake, heartbeat, execute commands.
//
// `a.conn.Serve` was called here instead, and `connection.Manager.Serve` is a Phase 0 stub whose body
// is a comment and `return nil`:
//
//	// Phase 0: dial not yet wired to a read loop.
//	// Future phases will implement the full event loop here.
//	return nil
//
// So `forgeops-agent run` opened no socket, joined no session, and received no command -- while
// exiting 0 and logging only "agent starting". Everything downstream of an approval therefore did
// nothing: the backend minted and signed the command, `send_command` had no connected device to hand
// it to, and the change set stayed `approved` exactly as the chokepoint documents for a failed
// delivery. The journey saw "timed out waiting for the change set to be applied".
//
// The real loop already existed, in `session.Manager.Serve` -- handshake, per-message revocation
// check, envelope verification, executor dispatch, the lot, with its own tests. It was simply never
// reached from `run`. `a.Session()` constructs it lazily because opening the credential store can
// fail, and that failure has to be reportable rather than fatal at construction.
//
// `ErrUnpaired` is returned as-is. An agent with a backend URL and no device token is misconfigured,
// and `run` should say so rather than idle: `doctor` prints the same distinction.
func (a *App) serveSession(ctx context.Context) error {
	manager, err := a.Session()
	if err != nil {
		return fmt.Errorf("session: %w", err)
	}
	return manager.Serve(ctx)
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
//
// The dependency set is assembled by `buildSessionDeps`, which is where the reasoning for each
// collaborator lives. It used to be `{Store, Logger, AgentVersion}` and nothing else, so `Serve`
// refused at the first dial for want of an identity provider.
func (a *App) Session() (*session.Manager, error) {
	a.sessionOnce.Do(func() {
		store, err := a.CredentialStore()
		if err != nil {
			a.sessionErr = err
			return
		}
		deps, err := a.buildSessionDeps(store)
		if err != nil {
			a.sessionErr = err
			return
		}
		a.sessionMgr, a.sessionErr = session.NewManager(a.cfg.BackendWSSURL, deps)
	})
	return a.sessionMgr, a.sessionErr
}

// UseBackendURL applies a backend URL resolved on the command line.
//
// MUST BE CALLED BEFORE `Session()`, and that is enforced rather than documented: `Session` is
// memoised with a `sync.Once`, so a later call would be silently ignored and the agent would dial
// the configured host while printing the one the user asked for. Calling this after the manager
// exists is a programming error and says so.
//
// It exists because `--backend` was unusable on its own. `Session()` built its collaborators from
// `a.cfg.BackendWSSURL` before any flag was read, so an agent given `--backend` and nothing else
// refused with "no backend URL configured: pair needs --backend" — naming the flag the user had
// just passed.
func (a *App) UseBackendURL(rawURL string, source config.BackendURLSource) {
	if a.sessionMgr != nil {
		panic("app: UseBackendURL called after Session(); the memoised manager already holds a URL")
	}
	a.cfg.BackendWSSURL = rawURL
	a.cfg.BackendWSSURLSource = source
	a.conn = connection.NewManager(rawURL, a.logger.Named("conn"), a.tracer)
}

// UseWorkspaceRoot applies a workspace directory named on the command line.
//
// No guard against a late call, unlike `UseBackendURL`: `codebaseIndexer()` is built fresh on every
// call and reads `cfg.Executor.WorkspaceRoot` at that moment, so there is no memoised value this
// could contradict. If that ever becomes memoised, this needs the same panic `UseBackendURL` has.
//
// It exists so `connect --workspace <path>` can point the agent at a directory without the user
// having to set `AGENT_WORKSPACE_ROOT` first. Setting an environment variable in order to run one
// command is exactly the friction this change removes.
func (a *App) UseWorkspaceRoot(root string) {
	a.cfg.Executor.WorkspaceRoot = root
}

// CredentialStore is the credential store, opened independently of the session manager.
//
// INDEPENDENT ON PURPOSE. It used to be reachable only through `Session()`, and `Session()` needs a
// backend URL — so on a machine with no `AGENT_BACKEND_WSS_URL`, `doctor` reported "credential
// store unusable: no backend URL configured". The store was entirely fine. Two separate facts had
// been collapsed into one message, and the message named the wrong one, which is precisely the kind
// of misdirection that made the Windows first run take as long as it did.
//
// Memoised separately for the same reason `Session` is: a keychain probe writes and deletes a
// marker, and running it twice per command could produce two different verdicts on a machine where
// the keychain is intermittently available.
func (a *App) CredentialStore() (*session.FileStore, error) {
	a.credentialStoreOnce.Do(func() {
		a.credentialStore, a.credentialStoreErr = session.NewStore(
			a.cfg.Session.StateDir, a.cfg.Session.CredentialStore)
	})
	return a.credentialStore, a.credentialStoreErr
}
