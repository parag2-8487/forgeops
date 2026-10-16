// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/models"
)

func TestAssembleDiagnosticBundle(t *testing.T) {
	tmpDir, err := os.MkdirTemp("", "diag-test-*")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(tmpDir)

	dfPath := filepath.Join(tmpDir, "Dockerfile")
	if err := os.WriteFile(dfPath, []byte("FROM alpine:latest\nWORKDIR /app\n"), 0644); err != nil {
		t.Fatal(err)
	}

	composePath := filepath.Join(tmpDir, "docker-compose.yml")
	if err := os.WriteFile(composePath, []byte("services:\n  app:\n    image: alpine\n"), 0644); err != nil {
		t.Fatal(err)
	}

	// Create subdirectories to exercise tree snippet walking
	if err := os.MkdirAll(filepath.Join(tmpDir, "src", "nested"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tmpDir, "src", "nested", "app.py"), []byte("print('hello')\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(tmpDir, "node_modules", "pkg"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(tmpDir, ".git"), 0755); err != nil {
		t.Fatal(err)
	}

	bp := &models.ProjectBlueprint{
		BlueprintID:    "test-diag-bp",
		RepositoryRoot: tmpDir,
	}

	execErr := &ExecutionError{
		Class:    ErrorClassDeterministic,
		ExitCode: 1,
		Stage:    "build",
	}

	bundle := AssembleDiagnosticBundle(
		context.Background(),
		tmpDir,
		"G2_SYNTAX",
		"build",
		bp,
		"docker compose build",
		1,
		1,
		"stdout sample",
		"stderr sample",
		execErr,
	)

	if bundle.GateIdentifier != "G2_SYNTAX" {
		t.Errorf("gate = %s, want G2_SYNTAX", bundle.GateIdentifier)
	}
	if bundle.Stage != "build" {
		t.Errorf("stage = %s, want build", bundle.Stage)
	}
	if bundle.ErrorClassification != string(ErrorClassDeterministic) {
		t.Errorf("error class = %s, want %s", bundle.ErrorClassification, ErrorClassDeterministic)
	}
	if bundle.CommandLine != "docker compose build" {
		t.Errorf("cmd = %s, want docker compose build", bundle.CommandLine)
	}
	if bundle.ExitCode != 1 {
		t.Errorf("exit code = %d, want 1", bundle.ExitCode)
	}
	if bundle.AttemptCount != 1 {
		t.Errorf("attempt = %d, want 1", bundle.AttemptCount)
	}
	if bundle.Stdout != "stdout sample" {
		t.Errorf("stdout = %s, want stdout sample", bundle.Stdout)
	}
	if bundle.Stderr != "stderr sample" {
		t.Errorf("stderr = %s, want stderr sample", bundle.Stderr)
	}
	if bundle.DeploymentManifest["Dockerfile"] != "FROM alpine:latest\nWORKDIR /app\n" {
		t.Errorf("Dockerfile manifest content mismatch")
	}
	if bundle.DeploymentManifest["docker-compose.yml"] != "services:\n  app:\n    image: alpine\n" {
		t.Errorf("docker-compose manifest content mismatch")
	}
	if bundle.Blueprint.BlueprintID != "test-diag-bp" {
		t.Errorf("blueprint ID = %s, want test-diag-bp", bundle.Blueprint.BlueprintID)
	}
}

func TestAssembleDiagnosticBundle_NilBlueprintAndError(t *testing.T) {
	tmpDir, err := os.MkdirTemp("", "diag-test-nil-*")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(tmpDir)

	bundle := AssembleDiagnosticBundle(
		context.Background(),
		tmpDir,
		"G1_INSPECTION",
		"inspection",
		nil,
		"scan",
		0,
		1,
		"",
		"",
		nil,
	)

	if bundle.GateIdentifier != "G1_INSPECTION" {
		t.Errorf("gate = %s, want G1_INSPECTION", bundle.GateIdentifier)
	}
	if bundle.ErrorClassification != "" {
		t.Errorf("expected empty error classification, got %s", bundle.ErrorClassification)
	}
}
