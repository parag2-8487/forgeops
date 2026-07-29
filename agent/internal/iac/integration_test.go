// SPDX-License-Identifier: Apache-2.0
//go:build integration

package iac_test

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/iac"
	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

func findTofu() string {
	// Try PATH first
	if p, err := exec.LookPath("tofu"); err == nil {
		return p
	}
	// Try known install location
	known := `C:\tools\tofu\tofu.exe`
	if _, err := os.Stat(known); err == nil {
		return known
	}
	return ""
}

func TestIntegration_InitLockfileReadonly(t *testing.T) {
	tofuBin := findTofu()
	if tofuBin == "" {
		t.Skip("tofu not found, skipping integration test")
	}

	// Copy fixture to a temp dir to avoid polluting the source
	fixtureSrc := filepath.Join("..", "..", "testfixtures", "tofu-null")
	tempDir := t.TempDir()
	copyDir(t, fixtureSrc, tempDir)

	cmd := exec.Command(tofuBin, "init", "-lockfile=readonly")
	cmd.Dir = tempDir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("tofu init -lockfile=readonly failed: %v\n%s", err, out)
	}
}

func TestIntegration_ValidateFixture(t *testing.T) {
	tofuBin := findTofu()
	if tofuBin == "" {
		t.Skip("tofu not found")
	}

	fixtureSrc := filepath.Join("..", "..", "testfixtures", "tofu-null")
	tempDir := t.TempDir()
	copyDir(t, fixtureSrc, tempDir)

	// Init first
	cmd := exec.Command(tofuBin, "init")
	cmd.Dir = tempDir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("init failed: %v\n%s", err, out)
	}

	// Now use the runner
	cfg := iac.TofuConfig{
		BinaryPath:     tofuBin,
		DefaultTimeout: 60 * time.Second,
		KillGrace:      5 * time.Second,
		MaxLineBytes:   65536,
	}
	runner := iac.NewTofuRunner(cfg, zap.NewNop(), telemetry.NoopTracer{})

	result, err := runner.Validate(context.Background(), tempDir)
	if err != nil {
		t.Fatalf("validate failed: %v", err)
	}
	if result.ExitCode != 0 {
		t.Fatalf("expected exit 0, got %d", result.ExitCode)
	}
}

func TestIntegration_PlanFixture(t *testing.T) {
	tofuBin := findTofu()
	if tofuBin == "" {
		t.Skip("tofu not found")
	}

	fixtureSrc := filepath.Join("..", "..", "testfixtures", "tofu-null")
	tempDir := t.TempDir()
	copyDir(t, fixtureSrc, tempDir)

	// Init
	cmd := exec.Command(tofuBin, "init")
	cmd.Dir = tempDir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("init failed: %v\n%s", err, out)
	}

	cfg := iac.TofuConfig{
		BinaryPath:     tofuBin,
		DefaultTimeout: 60 * time.Second,
		KillGrace:      5 * time.Second,
		MaxLineBytes:   65536,
	}
	runner := iac.NewTofuRunner(cfg, zap.NewNop(), telemetry.NoopTracer{})

	result, err := runner.Plan(context.Background(), tempDir, iac.PlanOptions{})
	if err != nil {
		t.Fatalf("plan failed: %v", err)
	}
	// null_resource.test will be created => exit code 2
	if result.ExitCode != 2 {
		t.Fatalf("expected exit 2 (has changes), got %d", result.ExitCode)
	}
	if !result.HasChanges {
		t.Fatal("expected HasChanges=true")
	}
	if len(result.PlanJSON) == 0 {
		t.Fatal("expected non-empty PlanJSON")
	}

	// Verify the JSON structure matches our sample
	var plan struct {
		FormatVersion   string `json:"format_version"`
		ResourceChanges []struct {
			Address string `json:"address"`
			Change  struct {
				Actions []string `json:"actions"`
			} `json:"change"`
		} `json:"resource_changes"`
	}
	if err := json.Unmarshal(result.PlanJSON, &plan); err != nil {
		t.Fatalf("unmarshal plan JSON: %v", err)
	}
	if plan.FormatVersion != "1.2" {
		t.Fatalf("expected format_version 1.2, got %s", plan.FormatVersion)
	}
	if len(plan.ResourceChanges) != 1 {
		t.Fatalf("expected 1 resource change, got %d", len(plan.ResourceChanges))
	}
	if plan.ResourceChanges[0].Address != "null_resource.test" {
		t.Fatalf("expected null_resource.test, got %s", plan.ResourceChanges[0].Address)
	}
	if plan.ResourceChanges[0].Change.Actions[0] != "create" {
		t.Fatalf("expected create action, got %s", plan.ResourceChanges[0].Change.Actions[0])
	}
}

func TestIntegration_PlanSampleValidity(t *testing.T) {
	// Verify the committed plan-sample.json is valid and matches expected shape
	samplePath := filepath.Join("..", "..", "testdata", "plan-sample.json")
	data, err := os.ReadFile(samplePath)
	if err != nil {
		t.Fatalf("read plan-sample.json: %v", err)
	}

	var plan struct {
		FormatVersion    string `json:"format_version"`
		TerraformVersion string `json:"terraform_version"`
		ResourceChanges  []struct {
			Address string `json:"address"`
			Change  struct {
				Actions []string `json:"actions"`
			} `json:"change"`
		} `json:"resource_changes"`
		Errored bool `json:"errored"`
	}
	if err := json.Unmarshal(data, &plan); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if plan.FormatVersion != "1.2" {
		t.Errorf("format_version: got %q, want 1.2", plan.FormatVersion)
	}
	if plan.TerraformVersion != "1.12.5" {
		t.Errorf("terraform_version: got %q, want 1.12.5", plan.TerraformVersion)
	}
	if len(plan.ResourceChanges) != 1 {
		t.Fatalf("expected 1 resource_change, got %d", len(plan.ResourceChanges))
	}
	if plan.ResourceChanges[0].Address != "null_resource.test" {
		t.Errorf("address: got %q", plan.ResourceChanges[0].Address)
	}
	if plan.ResourceChanges[0].Change.Actions[0] != "create" {
		t.Errorf("action: got %q", plan.ResourceChanges[0].Change.Actions[0])
	}
	if plan.Errored {
		t.Error("expected errored=false")
	}
}

// copyDir copies all files from src to dst (non-recursive, one level only for this fixture).
func copyDir(t *testing.T, src, dst string) {
	t.Helper()
	entries, err := os.ReadDir(src)
	if err != nil {
		t.Fatalf("read dir %s: %v", src, err)
	}
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(src, e.Name()))
		if err != nil {
			t.Fatalf("read %s: %v", e.Name(), err)
		}
		if err := os.WriteFile(filepath.Join(dst, e.Name()), data, 0o644); err != nil {
			t.Fatalf("write %s: %v", e.Name(), err)
		}
	}
}
