// SPDX-License-Identifier: Apache-2.0
package app

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/config"
	"github.com/parag8487/ForgeOps/agent/internal/connection"
	"github.com/parag8487/ForgeOps/agent/internal/scanner"
	"github.com/parag8487/ForgeOps/agent/internal/session"
)

// The two glyphs `doctor` prints.
//
// Constants rather than literals inline, which is not only tidiness: these were
// double-encoded in the source (finding 62) and printed as `âœ“`, and a single named
// constant is the only version of this file where that can be wrong in one place instead
// of seven.
const (
	glyphOK   = "\u2713" // ✓
	glyphFail = "\u2717" // ✗
	glyphInfo = "\u2022" // •
)

// NewRootCommand builds the Cobra command tree rooted at forgeops-agent.
func NewRootCommand(a *App) *cobra.Command {
	root := &cobra.Command{
		Use:           "forgeops-agent",
		Short:         "ForgeOps local agent",
		Long:          "AI-powered DevOps automation agent.",
		SilenceUsage:  true,
		SilenceErrors: true,
	}

	root.AddCommand(
		newVersionCmd(a),
		newDoctorCmd(a),
		newPairCmd(a),
		// Listed after `pair` and before `run` because that is the order it performs them in, and
		// `cobra` prints help in registration order.
		newConnectCmd(a),
		newRunCmd(a),
		newScanCmd(a),
		newWatchCmd(a),
		newMCPServeCmd(a),
	)

	return root
}

func newVersionCmd(a *App) *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Print version information",
		Run: func(cmd *cobra.Command, _ []string) {
			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "forgeops-agent %s (commit: %s, built: %s)\n",
				a.bi.Version, a.bi.Commit, a.bi.Date)
		},
	}
}

func newDoctorCmd(a *App) *cobra.Command {
	return &cobra.Command{
		Use:   "doctor",
		Short: "Check system prerequisites",
		RunE: func(cmd *cobra.Command, _ []string) error {
			ctx := cmd.Context()
			var issues []string

			// Docker check
			dr := a.docker.Check(ctx)
			if dr.Status == "healthy" {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s Docker: %s (%s/%s)\n", glyphOK, dr.ServerVersion, dr.OS, dr.Arch)
			} else {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s Docker: %v\n", glyphFail, dr.Error)
				issues = append(issues, "Docker is not available")
			}

			// K8s check
			kr := a.k8s.Check(ctx)
			if kr.Status == "healthy" {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s Kubernetes: %s (context: %s)\n", glyphOK, kr.ServerVersion, kr.Context)
			} else {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s Kubernetes: %v\n", glyphFail, kr.Error)
				issues = append(issues, "Kubernetes is not available")
			}

			// OpenTofu check
			_, tofuErr := a.tofu.Validate(ctx, os.TempDir())
			if tofuErr == nil {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s OpenTofu: available\n", glyphOK)
			} else {
				if strings.Contains(tofuErr.Error(), "not found") {
					_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s OpenTofu: not found (install tofu 1.12.5)\n", glyphFail)
					issues = append(issues, "OpenTofu is not installed")
				} else {
					_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s OpenTofu: available (validate skipped)\n", glyphOK)
				}
			}

			issues = append(issues, reportPairing(cmd, a)...)

			if len(issues) > 0 {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%d issue(s) found. Remediation:\n", len(issues))
				for _, issue := range issues {
					_, _ = fmt.Fprintf(cmd.OutOrStdout(), "  - %s\n", issue)
				}
			} else {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\nAll checks passed.\n")
			}
			return nil
		},
	}
}

// reportPairing prints the pairing state and returns any remediation lines (§10.10).
//
// Three outcomes, deliberately not two. `connection.ErrDisabled` means no backend URL is
// configured, which is the normal state for an agent used purely as a local CLI and
// therefore NOT an issue; `session.ErrUnpaired` means a URL is configured but no device
// token exists, which is a real gap with a specific fix. Collapsing them would either
// nag every local user or hide a half-configured agent, and §10.3 exists so `doctor` can
// tell a user which of the two they are in.
func reportPairing(cmd *cobra.Command, a *App) []string {
	out := cmd.OutOrStdout()

	// THE STORE IS REPORTED FIRST, AND WITHOUT ASKING FOR A SESSION. `a.Session()` needs a
	// backend URL, so when none is configured it fails — and this function used to report that
	// failure as "credential store unusable", naming the wrong one of two independent facts.
	issues := reportCredentialStore(cmd, a)

	manager, err := a.Session()
	if err != nil {
		if errors.Is(err, connection.ErrDisabled) {
			// Not an issue. An agent used purely as a local CLI has no backend, and §10.10 exists
			// so `doctor` can tell that apart from a half-configured one.
			_, _ = fmt.Fprintf(out, "%s Pairing: no backend configured\n", glyphInfo)
			_, _ = fmt.Fprintf(out, "  set AGENT_BACKEND_WSS_URL, or pass --backend to `pair`\n")
			return issues
		}
		_, _ = fmt.Fprintf(out, "%s Pairing: session unavailable: %v\n", glyphFail, err)
		return append(issues, "The session manager could not be built: "+err.Error())
	}

	status, err := manager.Status(cmd.Context())
	switch {
	case errors.Is(err, connection.ErrDisabled):
		_, _ = fmt.Fprintf(out, "%s Pairing: no backend configured (AGENT_BACKEND_WSS_URL is unset)\n", glyphInfo)
		return issues
	case errors.Is(err, session.ErrUnpaired):
		_, _ = fmt.Fprintf(out, "%s Pairing: unpaired (backend configured, no device token)\n", glyphFail)
		return append(issues,
			"Run `forgeops-agent pair --code <code>` with a code from the ForgeOps UI")
	case errors.Is(err, session.ErrCredentialsIncomplete):
		// Its own case, because the remedy is different from every other failure: retrying
		// achieves nothing and the agent must be wiped before it can pair again.
		_, _ = fmt.Fprintf(out, "%s Pairing: incomplete — %v\n", glyphFail, err)
		return append(issues,
			"Run `forgeops-agent pair --wipe`, then pair again with a new code")
	case err != nil:
		_, _ = fmt.Fprintf(out, "%s Pairing: %v\n", glyphFail, err)
		return append(issues, "The stored credential could not be read; re-pair this agent")
	}

	_, _ = fmt.Fprintf(out, "%s Pairing: device %s (credentials: %s)\n",
		glyphOK, status.DeviceID, status.StoreBackend)
	if !status.CertNotAfter.IsZero() {
		_, _ = fmt.Fprintf(out, "  client certificate expires %s\n",
			status.CertNotAfter.UTC().Format(time.RFC3339))
	}
	if status.Degraded {
		// Printed as a note, not an issue: OQ-26 accepts the file fallback, and the
		// whole point of accepting it is that the operator is told rather than left to
		// discover where the credential went.
		_, _ = fmt.Fprintf(out,
			"  note: no OS keychain was usable, so credentials are in a 0600 file\n")
	}
	return issues
}

// reportCredentialStore names the backend and, crucially, says whether a credential would fit
// BEFORE the user spends a pairing code finding out.
//
// This exists because `pair` on Windows could never succeed and said so only after the exchange
// had burned the code: the OS Credential Manager refuses a blob over 2560 bytes, and the full
// credential set is an order of magnitude past that. `doctor` is where a user looks first, so it
// is where the answer belongs.
func reportCredentialStore(cmd *cobra.Command, a *App) []string {
	out := cmd.OutOrStdout()

	store, err := a.CredentialStore()
	if err != nil {
		_, _ = fmt.Fprintf(out, "%s Credential store: unusable: %v\n", glyphFail, err)
		return []string{"The credential store cannot be opened; check AGENT_CREDENTIAL_STORE and AGENT_STATE_DIR"}
	}

	where := store.Backend()
	if path := store.Path(); path != "" {
		where += " at " + path
	}

	// A real trial write of a real-sized credential, not a comparison against a constant. The
	// numbers differ per platform and the only one that matters is this machine's.
	if err := store.CheckCapacity(cmd.Context(), session.CapacityProbeForDoctor()); err != nil {
		_, _ = fmt.Fprintf(out, "%s Credential store: %s cannot hold a device credential\n", glyphFail, where)
		_, _ = fmt.Fprintf(out, "  %v\n", err)
		return []string{
			"This machine's credential store will refuse a device credential, so `pair` would " +
				"fail after spending the code. Set AGENT_CREDENTIAL_STORE=file to use a 0600 " +
				"file in the state directory instead",
		}
	}

	_, _ = fmt.Fprintf(out, "%s Credential store: %s, and a device credential fits\n", glyphOK, where)
	return nil
}

// newPairCmd implements `forgeops-agent pair` (§3.1, §10.3).
func newPairCmd(a *App) *cobra.Command {
	var code string
	var backend string
	var wipe bool

	cmd := &cobra.Command{
		Use:   "pair",
		Short: "Pair this agent with a ForgeOps backend using a one-time code",
		Long: "Exchange a one-time pairing code for a device token, an envelope key and a\n" +
			"short-lived client certificate. The private key is generated on this machine\n" +
			"and never sent: only a certificate request is.\n\n" +
			"The code is single-use and expires in five minutes, so a second `pair` with the\n" +
			"same code fails by design. Use --wipe to unpair before pairing again.",
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			ctx := cmd.Context()

			// WIPE NEEDS NO BACKEND, and used to demand one. `a.Session()` ran first
			// unconditionally, and it fails when no backend URL is configured — so an agent that
			// could not reach a backend could not clear its own credentials either, which is
			// exactly the state a failed pairing leaves and exactly when wiping is needed.
			if wipe {
				if code != "" {
					return fmt.Errorf("pair: --wipe and --code are mutually exclusive")
				}
				store, err := a.CredentialStore()
				if err != nil {
					return err
				}
				if err := store.Wipe(ctx); err != nil {
					return err
				}
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Credentials wiped; this agent is unpaired.\n")
				return nil
			}
			if code == "" {
				return fmt.Errorf("pair: --code is required (get one from the ForgeOps UI)")
			}

			// THE BACKEND URL IS RESOLVED BEFORE THE SESSION IS BUILT, and the flag alone is
			// enough. It was not: `a.Session()` was constructed from `a.cfg.BackendWSSURL` before
			// the flag was consulted, so `--backend` was documented but unusable on its own and a
			// user had to set an environment variable as well. The refusal they got named the flag
			// they had just passed.
			//
			// The config already ran discovery and RECORDED WHICH SOURCE ANSWERED. Re-running
			// discovery here with `a.cfg.BackendWSSURL` in the environment slot would relabel a
			// value found in `.env` as having come from `AGENT_BACKEND_WSS_URL` — which it did,
			// briefly, and a message that is wrong about where a value came from is worse than no
			// message.
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
				return fmt.Errorf("pair: %s", config.BackendURLRemedy(workingDir))
			}
			a.UseBackendURL(resolved, source)
			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Using backend %s (from %s).\n", resolved, source)

			manager, err := a.Session()
			if err != nil {
				return err
			}

			result, err := manager.Pair(ctx, code, resolved)
			if err != nil {
				return err
			}

			// The result carries no secret by construction, so this whole block is safe
			// to print and safe to paste into a support thread.
			out := cmd.OutOrStdout()
			_, _ = fmt.Fprintf(out, "Paired.\n")
			_, _ = fmt.Fprintf(out, "  device id:         %s\n", result.DeviceID)
			_, _ = fmt.Fprintf(out, "  project id:        %s\n", result.ProjectID)
			_, _ = fmt.Fprintf(out, "  cert fingerprint:  %s\n", result.CertFingerprint)
			if !result.CertNotAfter.IsZero() {
				_, _ = fmt.Fprintf(out, "  cert expires:      %s\n", result.CertNotAfter.UTC().Format(time.RFC3339))
			}
			if !result.RenewAfter.IsZero() {
				_, _ = fmt.Fprintf(out, "  renew after:       %s\n", result.RenewAfter.UTC().Format(time.RFC3339))
			}
			_, _ = fmt.Fprintf(out, "  credentials in:    %s\n", result.StoreBackend)
			return nil
		},
	}

	cmd.Flags().StringVar(&code, "code", "", "the one-time pairing code from the ForgeOps UI")
	// Defaulted from configuration rather than required, so the common case is
	// `pair --code X` and the flag exists for an operator pairing against a backend other
	// than the one in the environment.
	// EMPTY DEFAULT, not a.cfg.BackendWSSURL. Defaulting the flag to the configured value would
	// make "the user passed --backend" indistinguishable from "the environment supplied it", and
	// the precedence chain could then not report its own source honestly. The resolution happens
	// in RunE, where all three sources are visible at once.
	cmd.Flags().StringVar(&backend, "backend", "",
		"backend URL; overrides AGENT_BACKEND_WSS_URL and any value discovered from .env")
	cmd.Flags().BoolVar(&wipe, "wipe", false, "remove stored credentials and return to the unpaired state")

	return cmd
}

func newRunCmd(a *App) *cobra.Command {
	return &cobra.Command{
		Use:   "run",
		Short: "Run the agent",
		RunE: func(cmd *cobra.Command, _ []string) error {
			a.logger.Info("agent starting", zap.String("version", a.bi.Version))
			return a.Run(cmd.Context())
		},
	}
}

// newScanCmd indexes the agent's workspace and submits the result (phases.md §1.3, §1.4).
//
// # WHY A CLI VERB AND NOT ONLY A COMMAND FROM THE BACKEND
//
// The two `scan.*` operations are implemented and dispatchable, so a backend that mints one gets a
// real index. But nothing on the backend mints one yet, and it cannot be added casually: §2.2.1
// makes `websocket.hub.send_command` a confined name that only `governance/` may reach, so a scan
// trigger is a governance decision rather than a route.
//
// That leaves a gap this verb closes honestly. The agent OWNS its workspace — it is the only party
// that can read the files at all — so an operator asking it to index that workspace needs no
// authority from the backend beyond the device token it already holds. The index write is still
// authorised server-side: `POST /analysis/codebase/{id}/index` requires the principal and scopes by
// tenant, so this verb cannot write into a project the device may not see.
//
// It is also what makes the readiness score honest. The score reads `file_tree`/`file_contents`, so
// before anything scans, a project's readiness is `0` and `indexed=false` — correct, and useless.
func newScanCmd(a *App) *cobra.Command {
	var projectID string
	cmd := &cobra.Command{
		Use:   "scan",
		Short: "Index this agent's workspace into a project's codebase index",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if projectID == "" {
				return errors.New("scan needs --project: the index is per project")
			}
			indexer, err := a.codebaseIndexer()
			if err != nil {
				return err
			}
			a.logger.Info("scanning the workspace",
				zap.String("project_id", projectID),
				zap.String("workspace_root", a.cfg.Executor.WorkspaceRoot))
			summary, err := indexer.IndexFull(cmd.Context(), projectID)
			if err != nil {
				return err
			}
			// Printed to stdout as well as logged, so a caller can read it without parsing the log
			// stream — the journey does exactly that.
			// The write is checked: a caller that parses this line (the journey does) would
			// otherwise read a truncated report as a smaller index.
			if _, werr := fmt.Fprintf(cmd.OutOrStdout(),
				"indexed %d file(s), %d chunk(s), %d dependency edge(s); %d redaction(s); inventory %s\n",
				summary.FilesIndexed, summary.ChunksIndexed, summary.Dependencies,
				summary.RedactionCount, summary.InventoryHash); werr != nil {
				return werr
			}
			if summary.VectorsAbsentReason != "" {
				// Not an error: the tree, the contents and the graph are persisted and the readiness
				// score works. Saying so is the difference between a sparse-only index and one the
				// operator believes is searchable.
				if _, werr := fmt.Fprintf(cmd.OutOrStdout(),
					"no vectors were written: %s\n", summary.VectorsAbsentReason); werr != nil {
					return werr
				}
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&projectID, "project", "", "the project id to index against (required)")
	return cmd
}

// newWatchCmd watches the workspace and re-indexes what changed, plus what depends on it (§1.3).
//
// # WHY THE AGENT SELF-TRIGGERS RATHER THAN WAITING TO BE TOLD
//
// The same reasoning that made `scan` a verb applies with more force here. §2.2.1 confines
// `websocket.hub.send_command` to `governance/`, so a backend-initiated re-index would have to be a
// governance decision — and a governance decision per keystroke is the wrong shape for something whose
// trigger is "a file on this machine changed". The agent is the only party that can observe its own
// workspace at all, so it needs no authority from the backend to notice; the WRITE is still authorised
// server-side by the device token and scoped by tenant, exactly as `scan` is.
//
// # WHAT MAKES IT INCREMENTAL RATHER THAN JUST REPEATED
//
// A change to a file that others import invalidates more than that file. `BuildIncrementalReport`
// computes `DirtyClosure` over the resolved dependency edges, so editing a module re-indexes the module
// AND its dependants, and leaves everything else alone. That is the §1.3 requirement that "first scan
// works" does not satisfy: the point is not that a scan can run again, it is that a small edit costs a
// small re-index while still keeping dependants correct.
//
// # WHY IT PRINTS EVERY BATCH
//
// The line naming the re-indexed paths is the only way an operator can tell a watch that is working
// from one that is running. It is also what makes this provable: a test can touch one file and read
// back which paths were re-indexed, which is a claim about behaviour rather than about logs existing.
func newWatchCmd(a *App) *cobra.Command {
	var (
		projectID  string
		debounceMs int
		once       bool
	)
	cmd := &cobra.Command{
		Use:   "watch",
		Short: "Watch this agent's workspace and incrementally re-index what changes",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if projectID == "" {
				return errors.New("watch needs --project: the index is per project")
			}
			indexer, err := a.codebaseIndexer()
			if err != nil {
				return err
			}
			root, err := workspaceRoot(a.cfg.Executor.WorkspaceRoot)
			if err != nil {
				return err
			}

			dirs, err := scanner.WatchableDirectories(root)
			if err != nil {
				return fmt.Errorf("enumerating directories to watch under %s: %w", root, err)
			}
			fsw, err := scanner.NewFSNotifyWatcher()
			if err != nil {
				return fmt.Errorf("starting the file system watcher: %w", err)
			}
			defer func() { _ = fsw.Close() }()

			// Concurrency 1: batches must be re-indexed IN ORDER. Two overlapping submissions for the
			// same project would race on the same rows, and the later-finishing one would win
			// regardless of which described the newer state.
			batches, err := scanner.NewDebouncedWatcher(fsw, debounceMs, 1).
				WatchCoalesced(cmd.Context(), dirs)
			if err != nil {
				return fmt.Errorf("watching %d directories under %s: %w", len(dirs), root, err)
			}

			a.logger.Info("watching the workspace",
				zap.String("project_id", projectID),
				zap.String("workspace_root", root),
				zap.Int("directories", len(dirs)),
				zap.Int("debounce_ms", debounceMs))
			// Printed as well as logged so a caller can wait for readiness without parsing the log
			// stream. Anything driving this needs to know the watch is established BEFORE it touches a
			// file, or it races the registration and the edit is never seen.
			if _, werr := fmt.Fprintf(cmd.OutOrStdout(),
				"watching %d directory(ies) under %s, debounce %dms\n",
				len(dirs), root, debounceMs); werr != nil {
				return werr
			}

			for batch := range batches {
				changed := make([]string, 0, len(batch))
				for _, ev := range batch {
					rel, rerr := filepath.Rel(root, ev.Path)
					if rerr != nil {
						continue
					}
					changed = append(changed, filepath.ToSlash(rel))
				}
				if len(changed) == 0 {
					continue
				}

				// A DELETION CANNOT BE HANDLED INCREMENTALLY, and running it is how that was found.
				//
				// `BuildIncrementalReport` computes the dirty closure from a FRESH full scan of the
				// tree. Once a file is gone, two things follow: it is absent from that scan, so the
				// partial report contains no entry for it; and every specifier that pointed at it now
				// fails to resolve, so it has no edges and `DirtyClosure` returns nothing. The report
				// was therefore EMPTY, and the backend refused it — observed exactly that, deleting
				// `depdemo/lib.js` gave `the backend refused the scan report (422)` and the file stayed
				// in the index for good.
				//
				// Finding the dependants of a deleted file would need the PREVIOUS graph, which the
				// agent does not keep. A full re-index is the correct answer rather than a fallback:
				// the deletion changes the RESOLUTION STATUS of every specifier that referred to it,
				// which is a whole-tree property, and a full scan also prunes the vanished path. It is
				// rare enough that the cost does not matter.
				missing := missingPaths(root, changed)
				if len(missing) > 0 {
					a.logger.Info("a deletion was seen, so the whole tree is re-indexed",
						zap.Strings("missing", missing))
					summary, ierr := indexer.IndexFull(cmd.Context(), projectID)
					if ierr != nil {
						a.logger.Error("the full re-index after a deletion failed", zap.Error(ierr))
						if _, werr := fmt.Fprintf(cmd.ErrOrStderr(),
							"full re-index after deleting %s failed: %v\n",
							strings.Join(missing, ", "), ierr); werr != nil {
							return werr
						}
						continue
					}
					if _, werr := fmt.Fprintf(cmd.OutOrStdout(),
						"re-indexed the whole tree after %s was deleted: %d file(s), %d chunk(s), %d edge(s); inventory %s\n",
						strings.Join(missing, ", "), summary.FilesIndexed, summary.ChunksIndexed,
						summary.Dependencies, summary.InventoryHash); werr != nil {
						return werr
					}
					if once {
						return nil
					}
					continue
				}
				summary, ierr := indexer.IndexChanged(cmd.Context(), projectID, changed)
				if ierr != nil {
					// A failed batch must not end the watch: the next save should still be indexed,
					// and an operator whose watch died silently on one transient error is worse off
					// than one who sees the error and keeps working.
					a.logger.Error("re-indexing the changed paths failed",
						zap.Strings("changed", changed), zap.Error(ierr))
					if _, werr := fmt.Fprintf(cmd.ErrOrStderr(),
						"re-index failed for %s: %v\n", strings.Join(changed, ", "), ierr); werr != nil {
						return werr
					}
					continue
				}
				// `submitted` rather than `re-indexed`, because those are different numbers and the
				// difference is observable. One changed file produces a closure of several — the
				// changed file plus everything importing it — and all of them are sent. The backend
				// then rewrites only the rows whose content actually differs, so editing a module
				// bumps `file_contents.updated_at` for the module and leaves its byte-identical
				// dependants alone. Both behaviours are correct, and a line claiming to have
				// re-indexed three files when one row moved would be describing neither.
				if _, werr := fmt.Fprintf(cmd.OutOrStdout(),
					"submitted %d file(s) in the closure of %s: %d chunk(s), %d new edge(s); inventory %s\n",
					summary.FilesIndexed, strings.Join(changed, ", "),
					summary.ChunksIndexed, summary.Dependencies, summary.InventoryHash); werr != nil {
					return werr
				}
				if once {
					return nil
				}
			}
			return cmd.Context().Err()
		},
	}
	cmd.Flags().StringVar(&projectID, "project", "", "the project id to index against (required)")
	cmd.Flags().IntVar(&debounceMs, "debounce", 500,
		"milliseconds of quiet before a batch of changes is re-indexed")
	cmd.Flags().BoolVar(&once, "once", false,
		"exit after the first batch, so a caller can observe one re-index without stopping a daemon")
	return cmd
}

// missingPaths returns the repository-relative paths in changed that no longer exist under root.
//
// Extracted from the watch loop so the DECISION is testable without a backend, a device token and a
// filesystem event. What it decides is which batches cannot be handled incrementally: a deleted file
// is absent from the fresh scan the incremental report is built from, and every specifier that pointed
// at it stops resolving, so the closure comes back empty and the report has nothing in it.
//
// A stat error other than "not there" is also treated as missing, deliberately. If the agent cannot
// see the file, it cannot include it in a report either, and a full re-index is the outcome that
// leaves the index consistent rather than the one that leaves a stale entry behind.
func missingPaths(root string, changed []string) []string {
	missing := make([]string, 0, len(changed))
	for _, rel := range changed {
		if _, err := os.Stat(filepath.Join(root, rel)); err != nil {
			missing = append(missing, rel)
		}
	}
	return missing
}

func newMCPServeCmd(a *App) *cobra.Command {
	return &cobra.Command{
		Use:   "mcp-serve",
		Short: "Run MCP server on stdio",
		RunE: func(cmd *cobra.Command, _ []string) error {
			return a.mcpSrv.Serve(cmd.Context())
		},
	}
}
