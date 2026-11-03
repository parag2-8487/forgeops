// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"context"
	"fmt"
	"os/exec"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/models"
)

// WorkloadGateResult records the outcome of the G6 Workload Verification Gate.
type WorkloadGateResult struct {
	GateID          string                      `json:"gate_id"`
	Passed          bool                        `json:"passed"`
	Verification    *WorkloadVerificationResult `json:"verification,omitempty"`
	ExecutionError  *ExecutionError             `json:"execution_error,omitempty"`
	OutputSummary   string                      `json:"output_summary"`
	DurationSeconds float64                     `json:"duration_seconds"`
}

// ExecuteWorkloadGate executes target-aware verification and collects container logs on failure.
func ExecuteWorkloadGate(ctx context.Context, workDir string, bp *models.ProjectBlueprint, host string) (*WorkloadGateResult, error) {
	startTime := time.Now()

	verResult, err := VerifyWorkloadHealth(ctx, workDir, bp, host)
	duration := time.Since(startTime).Seconds()

	if err != nil || !verResult.Healthy {
		// Collect recent logs for diagnostic bundle
		logCmd := exec.CommandContext(ctx, "docker", "compose", "logs", "--tail", "100")
		logCmd.Dir = workDir
		logOut, _ := logCmd.CombinedOutput()
		logText := string(logOut)

		classified := ClassifyError("health_check", 1, "", logText)
		return &WorkloadGateResult{
			GateID:          "G6",
			Passed:          false,
			Verification:    verResult,
			ExecutionError:  &classified,
			OutputSummary:   fmt.Sprintf("G6 workload health check failed: %s", verResult.Message),
			DurationSeconds: duration,
		}, fmt.Errorf("G6 health check failed: %w", err)
	}

	return &WorkloadGateResult{
		GateID:          "G6",
		Passed:          true,
		Verification:    verResult,
		OutputSummary:   fmt.Sprintf("G6 workload health verified successfully: %s", verResult.Message),
		DurationSeconds: duration,
	}, nil
}
