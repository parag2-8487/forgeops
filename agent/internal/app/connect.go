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
		replace   bool
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
			paired, pairedProject, pairedDevice, err := alreadyPaired(ctx, manager)
			if err != nil {
				return fmt.Errorf("connect: stage 1 (pair): %w", err)
			}
			resolvedProject := strings.TrimSpace(projectID)
			switch {
			case paired:
				// A DEVICE CERTIFICATE AUTHORISES EXACTLY ONE PROJECT, so keeping the existing
				// credential while indexing a different one cannot work. The backend refuses the
				// scan report with
				//
				//     403 Forbidden You do not have permission to perform this action
				//
				// which is the correct answer and names neither the project nor the credential. The
				// shortcut used to take that path silently: it saw a usable credential, said
				// "already paired", scanned a tree, and failed two stages later on an authorisation
				// decision the agent had all the information to predict.
				//
				// Refused BEFORE the scan, because a scan walks the whole tree and uploads nothing.
				//
				// Only when both ids are known. An empty `pairedProject` is a credential written
				// before the field existed, and an empty `resolvedProject` means the user named no
				// project — in neither case is there a disagreement to report, so it behaves exactly
				// as it did before rather than refusing on a fact it does not have.
				if resolvedProject != "" && pairedProject != "" && resolvedProject != pairedProject {
					// THE ONE-STEP PATH, and it is opt-in for a reason.
					//
					// Wiping a credential destroys this device's identity for the project it was
					// paired to. Doing that automatically because `--project` disagreed would
					// deauthorise a working agent on a mistyped id, so `--replace` makes the
					// operator say it. What it does NOT do is make them run a second command and
					// obtain a second code.
					if replace {
						if code == "" {
							return fmt.Errorf(
								"connect: stage 1 (pair): --replace needs --code, because wiping the "+
									"credential for project %s leaves this agent unpaired and only a "+
									"pairing code can pair it again",
								pairedProject)
						}
						if err := manager.Wipe(ctx); err != nil {
							return fmt.Errorf("connect: stage 1 (pair): unpairing from %s: %w", pairedProject, err)
						}
						_, _ = fmt.Fprintf(out,
							"[1/3] pair   unpaired from project %s at your request; pairing to %s\n",
							pairedProject, resolvedProject)
						// SAID AT THE MOMENT IT HAPPENS, because nobody goes looking for it later.
						//
						// Wiping removes the credential from THIS machine. It does not revoke it: the
						// certificate stays valid and `agent_devices` still lists the device as active,
						// so a copy of the old credential store would still be authorised for the old
						// project. `DELETE /api/v1/agents/{device_id}` is admin-only and the agent
						// cannot revoke itself, so the operator has to do it — and cannot revoke a
						// device nobody named.
						if pairedDevice != "" {
							_, _ = fmt.Fprintf(out,
								"[1/3] pair   NOTE the old credential is removed from this machine but "+
									"NOT revoked;\n"+
									"             device %s stays authorised for project %s until an "+
									"admin revokes it\n"+
									"             (Agents screen, or DELETE /api/v1/agents/%s)\n",
								pairedDevice, pairedProject, pairedDevice)
						}
						result, perr := manager.Pair(ctx, code, resolved)
						if perr != nil {
							return fmt.Errorf("connect: stage 1 (pair): %w", perr)
						}
						_, _ = fmt.Fprintf(out, "[1/3] pair   device %s, credentials in %s\n",
							result.DeviceID, result.StoreBackend)
						break
					}
					// TWO OPTIONS, DISTINGUISHED BY A FACT ONLY THE OPERATOR HAS. A device certificate
					// authorises one project, so this agent serves one project — that part is not a
					// choice. What varies is whether the new project REPLACES the old one or sits
					// beside it, and that depends on whether they are the same codebase.
					//
					// An earlier version of this message led with the multi-agent option as though it
					// were generally better. It is not: several projects pointing at one directory is
					// the common shape while somebody is finding their way around, and starting a
					// second agent on the same folder indexes one tree into two projects for no
					// benefit. Neither option is recommended over the other here, because the agent
					// cannot see the other project's workspace path and so cannot know which applies.
					return fmt.Errorf(
						"connect: stage 1 (pair): this agent is already paired to project %s, but "+
							"--project names %s. A device certificate authorises one project, so one "+
							"agent serves one project and the backend would refuse the scan with 403.\n\n"+
							"Your pairing code has NOT been used — this was decided locally, before "+
							"anything was sent.\n\n"+
							"If %s REPLACES the project this agent was serving, move the agent and give "+
							"up the old pairing:\n"+
							"    re-run this command with --replace, using the same code.\n\n"+
							"If both projects are separate codebases you want indexed at the same time, "+
							"give this one its own credential store and run a second agent:\n"+
							"    $env:AGENT_STATE_DIR=\"$env:LOCALAPPDATA\\ForgeOps\\%s\"\n"+
							"    forgeops-agent connect --code <code> --project %s --workspace <path>\n"+
							"  One state directory holds one credential, so both stay paired. Only worth "+
							"it when the two projects are different directories.\n\n"+
							"To carry on with the project this device already has, drop --project.",
						pairedProject, resolvedProject, resolvedProject,
						shortID(resolvedProject), resolvedProject)
				}
				if resolvedProject == "" {
					// The stored credential names the project, so the user need not repeat it — the
					// same courtesy the freshly-paired branch below extends.
					resolvedProject = pairedProject
				}
				_, _ = fmt.Fprintf(out,
					"[1/3] pair   already paired; keeping the existing credential for project %s\n",
					pairedProject)
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
	cmd.Flags().BoolVar(&replace, "replace", false,
		"when this agent is already paired to a DIFFERENT project, unpair from it and pair to the one "+
			"--project names, using --code. Requires --code, because wiping leaves the agent unpaired. "+
			"Opt-in rather than automatic: it destroys this device's identity for the old project, which "+
			"a mistyped --project must not be able to do")
	cmd.Flags().StringVar(&backend, "backend", "",
		"backend URL; overrides AGENT_BACKEND_WSS_URL and any value discovered from .env")
	cmd.Flags().StringVar(&projectID, "project", "",
		"the project to index; defaults to the project the pairing code was minted for")
	cmd.Flags().StringVar(&workspace, "workspace", "",
		"the directory to index and operate on; defaults to AGENT_WORKSPACE_ROOT")

	return cmd
}

// alreadyPaired reports whether a usable credential is already stored, and which project it is for.
//
// `ErrUnpaired` and `ErrNoCredentials` both mean "no", and are the expected answers on a first run.
// `ErrCredentialsIncomplete` is NOT treated as "no": it means a token exists with no certificate
// beside it, and pairing over the top would leave the stale half in place. It is returned so the
// user is told to wipe.
//
// THE PROJECT IS RETURNED because "is a credential stored" was never the whole question. A device
// certificate authorises one project, so a stored credential is only usable for the project it was
// issued for, and the caller cannot judge that from a bool. Empty means a credential written before
// the field existed.
// shortID is the leading segment of a uuid, for naming a directory a human has to type.
//
// A full uuid in a path is correct and unreadable; the first segment is unique enough to keep two projects
// apart on one machine and short enough that an operator can see which is which in a file browser.
func shortID(id string) string {
	if index := strings.Index(id, "-"); index > 0 {
		return id[:index]
	}
	if len(id) > 8 {
		return id[:8]
	}
	return id
}

// alreadyPaired reports whether a usable credential exists, and what it is for.
//
// The DEVICE ID is returned as well as the project because `--replace` discards this credential without
// revoking it — `DELETE /api/v1/agents/{device_id}` is admin-only, so the agent cannot revoke itself — and an
// operator cannot revoke what nobody named. Re-pairing therefore leaves the previous certificate valid and
// authorised, which is worth saying out loud at the moment it happens rather than leaving to be discovered in
// a device list months later.
func alreadyPaired(ctx context.Context, manager *session.Manager) (bool, string, string, error) {
	status, err := manager.Status(ctx)
	switch {
	case err == nil:
		return true, status.ProjectID, status.DeviceID, nil
	case errors.Is(err, session.ErrUnpaired), errors.Is(err, session.ErrNoCredentials):
		return false, "", "", nil
	default:
		return false, "", "", err
	}
}
