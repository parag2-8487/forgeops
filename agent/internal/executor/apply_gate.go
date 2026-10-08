// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/models"
)

// ApplyGateResult records the outcome of the G5 Apply/Startup Gate.
type ApplyGateResult struct {
	GateID          string          `json:"gate_id"`
	Passed          bool            `json:"passed"`
	ExecutionError  *ExecutionError `json:"execution_error,omitempty"`
	OutputSummary   string          `json:"output_summary"`
	DurationSeconds float64         `json:"duration_seconds"`
}

// ExecuteApplyGate applies the deployment manifests and verifies initial container startup stability.
func ExecuteApplyGate(ctx context.Context, workDir string, bp *models.ProjectBlueprint, composeFile string) (*ApplyGateResult, error) {
	startTime := time.Now()

	// Apply phase: run containers without rebuilding
	var cmd *exec.Cmd
	if composeFile != "" {
		cmd = exec.CommandContext(ctx, "docker", "compose", "-f", composeFile, "up", "-d", "--no-build")
	} else {
		cmd = exec.CommandContext(ctx, "docker", "run", "-d", "--name", "forgeops-app", "-P", "forgeops-app:latest")
	}
	cmd.Dir = workDir

	var stdoutBuf, stderrBuf bytes.Buffer
	cmd.Stdout = &stdoutBuf
	cmd.Stderr = &stderrBuf

	if err := cmd.Run(); err != nil {
		exitCode := 1
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
		}
		classified := ClassifyError("apply", exitCode, stdoutBuf.String(), stderrBuf.String())
		return &ApplyGateResult{
			GateID:          "G5",
			Passed:          false,
			ExecutionError:  &classified,
			OutputSummary:   fmt.Sprintf("Failed to apply manifests: %s", classified.Message),
			DurationSeconds: time.Since(startTime).Seconds(),
		}, fmt.Errorf("G5 apply gate command failed: %w", err)
	}

	// Startup stability observation: ensure container does not crash immediately
	observationPeriod := 5 * time.Second
	select {
	case <-time.After(observationPeriod):
	case <-ctx.Done():
		return nil, ctx.Err()
	}

	// Check container status
	var psCmd *exec.Cmd
	if composeFile != "" {
		psCmd = exec.CommandContext(ctx, "docker", "compose", "-f", composeFile, "ps", "--format", "{{.State}}")
	} else {
		psCmd = exec.CommandContext(ctx, "docker", "inspect", "-f", "{{.State.Status}}", "forgeops-app")
	}
	psCmd.Dir = workDir

	out, err := psCmd.Output()
	statusStr := strings.ToLower(strings.TrimSpace(string(out)))

	if err != nil || strings.Contains(statusStr, "exited") || strings.Contains(statusStr, "dead") {
		// Container crashed immediately: fetch logs
		var logCmd *exec.Cmd
		if composeFile != "" {
			logCmd = exec.CommandContext(ctx, "docker", "compose", "-f", composeFile, "logs", "--tail", "50")
		} else {
			logCmd = exec.CommandContext(ctx, "docker", "logs", "--tail", "50", "forgeops-app")
		}
		logCmd.Dir = workDir
		logsOut, _ := logCmd.CombinedOutput()
		logText := string(logsOut)

		classified := ClassifyError("startup", 1, "", logText)
		return &ApplyGateResult{
			GateID:          "G5",
			Passed:          false,
			ExecutionError:  &classified,
			OutputSummary:   fmt.Sprintf("Container terminated immediately upon startup: %s", statusStr),
			DurationSeconds: time.Since(startTime).Seconds(),
		}, fmt.Errorf("G5 apply gate startup failure: container state is %s", statusStr)
	}

	return &ApplyGateResult{
		GateID:          "G5",
		Passed:          true,
		OutputSummary:   "Containers started and remained stable.",
		DurationSeconds: time.Since(startTime).Seconds(),
	}, nil
}
