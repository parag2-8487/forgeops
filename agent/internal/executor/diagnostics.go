// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/parag8487/ForgeOps/agent/internal/models"
)

// AssembleDiagnosticBundle collects all execution and container state upon gate failure.
func AssembleDiagnosticBundle(
	ctx context.Context,
	workDir string,
	gateID string,
	stage string,
	bp *models.ProjectBlueprint,
	cmdLine string,
	exitCode int,
	attempt int,
	stdout string,
	stderr string,
	execErr *ExecutionError,
) models.DiagnosticBundle {
	bundle := models.DiagnosticBundle{
		GateIdentifier: gateID,
		Stage:          stage,
		CommandLine:    cmdLine,
		ExitCode:       exitCode,
		AttemptCount:   attempt,
		Stdout:         stdout,
		Stderr:         stderr,
		DeploymentManifest: make(map[string]string),
	}

	if bp != nil {
		bundle.Blueprint = *bp
	}

	if execErr != nil {
		bundle.ErrorClassification = string(execErr.Class)
	}

	// Capture existing Dockerfile / Compose content
	dfPath := filepath.Join(workDir, "Dockerfile")
	if data, err := os.ReadFile(dfPath); err == nil {
		bundle.DeploymentManifest["Dockerfile"] = string(data)
	}

	composePath := filepath.Join(workDir, "docker-compose.yml")
	if data, err := os.ReadFile(composePath); err == nil {
		bundle.DeploymentManifest["docker-compose.yml"] = string(data)
	}

	// Capture container logs
	logCmd := exec.CommandContext(ctx, "docker", "compose", "logs", "--tail", "100")
	logCmd.Dir = workDir
	if logOut, err := logCmd.CombinedOutput(); err == nil {
		bundle.ContainerLogs = string(logOut)
	}

	// Capture repository tree snippet around workDir
	var treeLines []string
	_ = filepath.Walk(workDir, func(path string, info os.FileInfo, err error) error {
		if err != nil || len(treeLines) > 50 {
			return nil
		}
		rel, _ := filepath.Rel(workDir, path)
		if strings.Contains(rel, ".git") || strings.Contains(rel, "node_modules") {
			if info.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		treeLines = append(treeLines, rel)
		return nil
	})
	bundle.TreeSnippet = strings.Join(treeLines, "\n")

	return bundle
}
