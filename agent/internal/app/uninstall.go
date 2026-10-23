// SPDX-License-Identifier: Apache-2.0
package app

import (
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/parag8487/ForgeOps/agent/internal/uninstall"
)

func newUninstallCmd(a *App) *cobra.Command {
	var dryRun bool
	var force bool
	var wipeCreds bool
	var jsonOutput bool

	cmd := &cobra.Command{
		Use:   "uninstall",
		Short: "Uninstall all verified ForgeOps agent installations from this machine",
		Long: "Discovers all verified ForgeOps agent installations belonging to the current user,\n" +
			"terminates active agent processes, removes the binaries and dedicated directories,\n" +
			"and removes stale PATH entries. Credentials and user configuration are preserved\n" +
			"by default unless --wipe-credentials is specified.",
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			ctx := cmd.Context()
			out := cmd.OutOrStdout()

			opts := uninstall.Options{
				DryRun:          dryRun,
				WipeCredentials: wipeCreds,
			}

			// Pre-flight discovery
			installations := uninstall.DiscoverInstallations(ctx)
			if len(installations) == 0 {
				if jsonOutput {
					rep := uninstall.Report{
						VerifiedClean:        true,
						Summary:              "No ForgeOps agent installations found.",
						CredentialsPreserved: !wipeCreds,
					}
					enc := json.NewEncoder(out)
					enc.SetIndent("", "  ")
					return enc.Encode(rep)
				}
				_, _ = fmt.Fprintf(out, "%s No ForgeOps agent installations found on this system.\n", glyphOK)
				return nil
			}

			if !force && !dryRun {
				_, _ = fmt.Fprintf(out, "Found %d ForgeOps agent installation(s):\n", len(installations))
				for i, inst := range installations {
					_, _ = fmt.Fprintf(out, "  [%d] %s (version: %s)\n", i+1, inst.Path, inst.Version)
				}
				_, _ = fmt.Fprintf(out, "\nThis will stop running ForgeOps agent processes and delete these binaries.\n")
				if wipeCreds {
					_, _ = fmt.Fprintf(out, "WARNING: --wipe-credentials was passed; stored device tokens and certificates will be removed.\n")
				} else {
					_, _ = fmt.Fprintf(out, "Notice: Device pairing credentials and configuration will be preserved.\n")
				}
			}

			report, err := uninstall.UninstallAll(ctx, opts)
			if err != nil {
				return fmt.Errorf("uninstall failed: %w", err)
			}

			if wipeCreds && !dryRun {
				store, storeErr := a.CredentialStore()
				if storeErr == nil {
					_ = store.Wipe(ctx)
					report.CredentialsPreserved = false
				}
			}

			if jsonOutput {
				enc := json.NewEncoder(out)
				enc.SetIndent("", "  ")
				return enc.Encode(report)
			}

			if dryRun {
				_, _ = fmt.Fprintf(out, "\n[DRY RUN] Would remove %d installation(s):\n", len(report.Found))
				for _, inst := range report.Found {
					_, _ = fmt.Fprintf(out, "  %s %s\n", glyphInfo, inst.Path)
				}
				return nil
			}

			_, _ = fmt.Fprintf(out, "\nUninstall summary:\n")
			_, _ = fmt.Fprintf(out, "  %s %s\n", glyphOK, report.Summary)
			if len(report.Stopped) > 0 {
				_, _ = fmt.Fprintf(out, "  %s Stopped %d active agent process(es):\n", glyphOK, len(report.Stopped))
				for _, p := range report.Stopped {
					_, _ = fmt.Fprintf(out, "      PID %d (%s)\n", p.PID, p.Path)
				}
			}
			if len(report.Removed) > 0 {
				_, _ = fmt.Fprintf(out, "  %s Removed %d binary/directory path(s):\n", glyphOK, len(report.Removed))
				for _, p := range report.Removed {
					_, _ = fmt.Fprintf(out, "      %s\n", p)
				}
			}
			if len(report.PathEntriesRemoved) > 0 {
				_, _ = fmt.Fprintf(out, "  %s Cleaned from user PATH: %s\n", glyphOK, report.PathEntriesRemoved[0])
			}
			if len(report.Skipped) > 0 {
				_, _ = fmt.Fprintf(out, "  %s Skipped %d item(s):\n", glyphFail, len(report.Skipped))
				for _, s := range report.Skipped {
					_, _ = fmt.Fprintf(out, "      %s: %s\n", s.Path, s.Reason)
				}
			}
			if len(report.LeftBehind) > 0 {
				_, _ = fmt.Fprintf(out, "  %s Left behind %d item(s):\n", glyphFail, len(report.LeftBehind))
				for _, lb := range report.LeftBehind {
					_, _ = fmt.Fprintf(out, "      %s: %s\n", lb.Path, lb.Reason)
				}
			}

			if report.CredentialsPreserved {
				_, _ = fmt.Fprintf(out, "\n  %s Note: Stored credentials were preserved. Use `forgeops-agent pair --wipe` to remove them.\n", glyphInfo)
			}

			return nil
		},
	}

	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "report what would be removed without modifying anything")
	cmd.Flags().BoolVar(&force, "force", false, "proceed with uninstallation without interactive confirmation")
	cmd.Flags().BoolVar(&wipeCreds, "wipe-credentials", false, "also wipe stored device credentials")
	cmd.Flags().BoolVar(&jsonOutput, "json", false, "output report as structured JSON")

	return cmd
}
