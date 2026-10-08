package executor

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/models"
)

// BuildGateResult records the outcome of the G4 Build/Compile Gate.
type BuildGateResult struct {
	GateID          string          `json:"gate_id"`
	Passed          bool            `json:"passed"`
	Attempts        int             `json:"attempts"`
	ExecutionError  *ExecutionError `json:"execution_error,omitempty"`
	OutputSummary   string          `json:"output_summary"`
	DurationSeconds float64         `json:"duration_seconds"`
}

// ExecuteBuildGate executes container image compilation enforcing fast-fail on deterministic errors.
func ExecuteBuildGate(ctx context.Context, workDir string, bp *models.ProjectBlueprint, composeFile string) (*BuildGateResult, error) {
	startTime := time.Now()
	maxTransientRetries := 2
	attempt := 0

	for {
		attempt++
		var cmd *exec.Cmd

		if composeFile != "" {
			cmd = exec.CommandContext(ctx, "docker", "compose", "-f", composeFile, "build")
		} else {
			cmd = exec.CommandContext(ctx, "docker", "build", "-t", "forgeops-app:latest", ".")
		}
		cmd.Dir = workDir

		var stdoutBuf, stderrBuf bytes.Buffer
		cmd.Stdout = &stdoutBuf
		cmd.Stderr = &stderrBuf

		err := cmd.Run()
		stdoutStr := stdoutBuf.String()
		stderrStr := stderrBuf.String()

		if err == nil {
			return &BuildGateResult{
				GateID:          "G4",
				Passed:          true,
				Attempts:        attempt,
				OutputSummary:   "Build succeeded successfully.",
				DurationSeconds: time.Since(startTime).Seconds(),
			}, nil
		}

		exitCode := 1
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
		}

		classified := ClassifyError("build", exitCode, stdoutStr, stderrStr)

		// Deterministic error: Fast-fail on Attempt 1
		if classified.Class == ErrorClassDeterministic || classified.Class == ErrorClassApplicationCode {
			return &BuildGateResult{
				GateID:          "G4",
				Passed:          false,
				Attempts:        attempt,
				ExecutionError:  &classified,
				OutputSummary:   fmt.Sprintf("Deterministic build error encountered on attempt %d: %s", attempt, classified.Message),
				DurationSeconds: time.Since(startTime).Seconds(),
			}, fmt.Errorf("G4 build gate fast-failed on attempt %d: %s", attempt, classified.Message)
		}

		// Transient error: retry up to maxTransientRetries
		if classified.Class == ErrorClassTransient && attempt <= maxTransientRetries {
			backoff := time.Duration(attempt*3) * time.Second
			select {
			case <-time.After(backoff):
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}

		// Retries exhausted or internal engine error
		return &BuildGateResult{
			GateID:          "G4",
			Passed:          false,
			Attempts:        attempt,
			ExecutionError:  &classified,
			OutputSummary:   fmt.Sprintf("Build failed after %d attempts: %s", attempt, classified.Message),
			DurationSeconds: time.Since(startTime).Seconds(),
		}, fmt.Errorf("G4 build gate failed after %d attempts: %s", attempt, classified.Message)
	}
}
