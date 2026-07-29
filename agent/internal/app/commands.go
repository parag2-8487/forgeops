// SPDX-License-Identifier: Apache-2.0
package app

import (
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"
	"go.uber.org/zap"
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
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "âœ“ Docker: %s (%s/%s)\n", dr.ServerVersion, dr.OS, dr.Arch)
			} else {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "âœ— Docker: %v\n", dr.Error)
				issues = append(issues, "Docker is not available")
			}

			// K8s check
			kr := a.k8s.Check(ctx)
			if kr.Status == "healthy" {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "âœ“ Kubernetes: %s (context: %s)\n", kr.ServerVersion, kr.Context)
			} else {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "âœ— Kubernetes: %v\n", kr.Error)
				issues = append(issues, "Kubernetes is not available")
			}

			// OpenTofu check
			_, tofuErr := a.tofu.Validate(ctx, os.TempDir())
			if tofuErr == nil {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "âœ“ OpenTofu: available\n")
			} else {
				if strings.Contains(tofuErr.Error(), "not found") {
					_, _ = fmt.Fprintf(cmd.OutOrStdout(), "âœ— OpenTofu: not found (install tofu 1.12.5)\n")
					issues = append(issues, "OpenTofu is not installed")
				} else {
					_, _ = fmt.Fprintf(cmd.OutOrStdout(), "âœ“ OpenTofu: available (validate skipped)\n")
				}
			}

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
