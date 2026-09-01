// SPDX-License-Identifier: Apache-2.0

package app

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"
	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/config"
	"github.com/parag8487/ForgeOps/agent/internal/session"
)

// `forgeops-agent connect` — one command from nothing to a running, indexed agent.
//
// WHAT IT REPLACES. Getting connected took: open a terminal, cd into the source tree, know Go is
// installed, `go build -o forgeops-agent.exe ./cmd/agent`, know to prefix `.\`, know the backend
// URL, set an environment variable, `pair`, then `scan`, then `run` — and beat a five-minute clock
// while doing it. Six of those eight steps are not the user's problem.
//
// WHAT IT IS NOT. It gains NO authority the individual verbs do not have. It calls `pair`, then the
// same indexer `scan` calls, then the same `Run` loop `run` calls, in that order, against the same
// session manager. There is no combined credential, no new route, and nothing here can do anything a
// user could not do by typing the three verbs. `pair`, `scan`, `run` and `watch` all keep working
// exactly as before; this is a caller of them, not a replacement.
//
// EVERY STAGE REPORTS, so a failure names the stage. A single command that fails with one line is
// worse than three commands that fail one at a time — this prints each stage as it completes and
// prefixes a failure with the stage that produced it.
func newConnectCmd(a *App) *cobra.Command {
	var (
		code      string
		backend   string
		projectID string
		workspace string
	)

	cmd := &cobra.Command{
		Use:   "connect",
		Short: "Pair, index this workspace, and stay running — the whole first run in one command",
		Long: "Do everything a first run needs, in order, reporting each stage:\n" +
			"  1. pair      exchange the one-time code for a device credential\n" +
			"  2. scan      index the workspace into the project's codebase index\n" +
			"  3. run       hold the session open and execute approved change sets\n\n" +
			"The individual verbs still exist and still work. This adds no authority: it calls\n" +
			"them in order against the same session, and refuses rather than guessing at every\n" +
			"step, exactly as they do.\n\n" +
			"--project is optional. Pairing reports the project the code was minted for, and that\n" +
			"is the project the scan uses unless you name a different one.",
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			ctx := cmd.Context()
			out := cmd.OutOrStdout()

			if strings.TrimSpace(code) == "" {
				return errors.New("connect: --code is required (mint one from the ForgeOps UI)")
			}

			// Stage 0 is not a stage, it is the precondition: resolve the backend before anything
			// is spent, exactly as `pair` does, and for the same reason.
			resolved, source := a.cfg.BackendWSSURL, a.cfg.BackendWSSURLSource
			if explicit := strings.TrimSpace(backend); explicit != "" {
				var err error
				resolved, source, err = config.DiscoverBackendURL(explicit, "", "")
				if err != nil {
					return err
				}
			}
			if resolved == "" {
				workingDir, _ := os.Getwd()
				return fmt.Errorf("connect: %s", config.BackendURLRemedy(workingDir))
			}
			if strings.TrimSpace(workspace) != "" {
				a.UseWorkspaceRoot(workspace)
			}
			a.UseBackendURL(resolved, source)
			_, _ = fmt.Fprintf(out, "Backend:   %s (from %s)\n", resolved, source)
			_, _ = fmt.Fprintf(out, "Workspace: %s\n\n", a.cfg.Executor.WorkspaceRoot)

			manager, err := a.Session()
			if err != nil {
				return fmt.Errorf("connect: stage 1 (pair): %w", err)
			}

			// ── stage 1: pair ────────────────────────────────────────────────────────────
			//
			// Skipped when this agent already holds a credential, so `connect` is safe to re-run.
			// Re-pairing would spend a second code for nothing and, worse, `pair` refuses a
			// second exchange on a healthy agent — so a naive `connect` would fail on its second
			// invocation, which is the invocation a user makes after a laptop reboot.
			paired, err := alreadyPaired(ctx, manager)
			if err != nil {
				return fmt.Errorf("connect: stage 1 (pair): %w", err)
			}
			resolvedProject := strings.TrimSpace(projectID)
			switch {
			case paired:
				_, _ = fmt.Fprintf(out, "[1/3] pair   already paired; keeping the existing credential\n")
			default:
				result, perr := manager.Pair(ctx, code, resolved)
				if perr != nil {
					return fmt.Errorf("connect: stage 1 (pair): %w", perr)
				}
				_, _ = fmt.Fprintf(out, "[1/3] pair   device %s, credentials in %s\n",
					result.DeviceID, result.StoreBackend)
				if resolvedProject == "" {
					// The code was minted for a project, so the user does not have to repeat it.
					resolvedProject = result.ProjectID
				}
			}

			if resolvedProject == "" {
				// Reached only when the agent was already paired and no --project was given: the
				// pairing response that carried the project id belongs to an earlier run.
				return errors.New(
					"connect: stage 2 (scan): this agent is already paired, so there is no pairing " +
						"response to read the project from. Pass --project <id>, which the ForgeOps " +
						"UI shows beside the project name")
			}

			// ── stage 2: scan ────────────────────────────────────────────────────────────
			indexer, err := a.codebaseIndexer()
			if err != nil {
				return fmt.Errorf("connect: stage 2 (scan): %w", err)
			}
			summary, err := indexer.IndexFull(ctx, resolvedProject)
			if err != nil {
				return fmt.Errorf("connect: stage 2 (scan): %w", err)
			}
			_, _ = fmt.Fprintf(out,
				"[2/3] scan   %d file(s), %d chunk(s), %d dependency edge(s), %d redaction(s)\n",
				summary.FilesIndexed, summary.ChunksIndexed, summary.Dependencies,
				summary.RedactionCount)
			if summary.VectorsAbsentReason != "" {
				_, _ = fmt.Fprintf(out, "             no vectors were written: %s\n",
					summary.VectorsAbsentReason)
			}

			// ── stage 3: run ─────────────────────────────────────────────────────────────
			//
			// Blocks until the context is cancelled. Announced BEFORE it blocks, because a command
			// that goes quiet with no explanation reads as a hang — and this one is meant to sit
			// there for the rest of the session.
			_, _ = fmt.Fprintf(out,
				"[3/3] run    holding the session open for project %s; press Ctrl+C to stop\n",
				resolvedProject)
			a.logger.Info("agent starting after connect",
				zap.String("version", a.bi.Version),
				zap.String("project_id", resolvedProject))
			if err := a.Run(ctx); err != nil {
				return fmt.Errorf("connect: stage 3 (run): %w", err)
			}
			return nil
		},
	}

	cmd.Flags().StringVar(&code, "code", "", "the one-time pairing code from the ForgeOps UI (required)")
	cmd.Flags().StringVar(&backend, "backend", "",
		"backend URL; overrides AGENT_BACKEND_WSS_URL and any value discovered from .env")
	cmd.Flags().StringVar(&projectID, "project", "",
		"the project to index; defaults to the project the pairing code was minted for")
	cmd.Flags().StringVar(&workspace, "workspace", "",
		"the directory to index and operate on; defaults to AGENT_WORKSPACE_ROOT")

	return cmd
}

// alreadyPaired reports whether a usable credential is already stored.
//
// `ErrUnpaired` and `ErrNoCredentials` both mean "no", and are the expected answers on a first run.
// `ErrCredentialsIncomplete` is NOT treated as "no": it means a token exists with no certificate
// beside it, and pairing over the top would leave the stale half in place. It is returned so the
// user is told to wipe.
func alreadyPaired(ctx context.Context, manager *session.Manager) (bool, error) {
	_, err := manager.Status(ctx)
	switch {
	case err == nil:
		return true, nil
	case errors.Is(err, session.ErrUnpaired), errors.Is(err, session.ErrNoCredentials):
		return false, nil
	default:
		return false, err
	}
}
