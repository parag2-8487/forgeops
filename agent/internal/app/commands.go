// SPDX-License-Identifier: Apache-2.0
package app

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/connection"
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
		newRunCmd(a),
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

	manager, err := a.Session()
	if err != nil {
		_, _ = fmt.Fprintf(out, "%s Pairing: credential store unusable: %v\n", glyphFail, err)
		return []string{"The credential store cannot be opened; check AGENT_CREDENTIAL_STORE and AGENT_STATE_DIR"}
	}

	status, err := manager.Status(cmd.Context())
	switch {
	case errors.Is(err, connection.ErrDisabled):
		_, _ = fmt.Fprintf(out, "%s Pairing: no backend configured (AGENT_BACKEND_WSS_URL is unset)\n", glyphInfo)
		return nil
	case errors.Is(err, session.ErrUnpaired):
		_, _ = fmt.Fprintf(out, "%s Pairing: unpaired (backend configured, no device token)\n", glyphFail)
		return []string{"Run `forgeops-agent pair --code <code>` with a code from the ForgeOps UI"}
	case err != nil:
		_, _ = fmt.Fprintf(out, "%s Pairing: %v\n", glyphFail, err)
		return []string{"The stored credential could not be read; re-pair this agent"}
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
			manager, err := a.Session()
			if err != nil {
				return err
			}
			ctx := cmd.Context()

			if wipe {
				if code != "" {
					return fmt.Errorf("pair: --wipe and --code are mutually exclusive")
				}
				if err := manager.Wipe(ctx); err != nil {
					return err
				}
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Credentials wiped; this agent is unpaired.\n")
				return nil
			}
			if code == "" {
				return fmt.Errorf("pair: --code is required (get one from the ForgeOps UI)")
			}

			result, err := manager.Pair(ctx, code, backend)
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
	cmd.Flags().StringVar(&backend, "backend", a.cfg.BackendWSSURL,
		"backend URL (defaults to AGENT_BACKEND_WSS_URL)")
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

func newMCPServeCmd(a *App) *cobra.Command {
	return &cobra.Command{
		Use:   "mcp-serve",
		Short: "Run MCP server on stdio",
		RunE: func(cmd *cobra.Command, _ []string) error {
			return a.mcpSrv.Serve(cmd.Context())
		},
	}
}
