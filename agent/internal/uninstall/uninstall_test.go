// SPDX-License-Identifier: Apache-2.0
package uninstall

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func createFakeAgentBinary(t *testing.T, dir, filename string, isForgeOps bool) string {
	t.Helper()
	err := os.MkdirAll(dir, 0o755)
	if err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	path := filepath.Join(dir, filename)
	content := []byte("dummy binary without signatures")
	if isForgeOps {
		content = []byte("#!/bin/sh\ngithub.com/parag8487/ForgeOps/agent dummy binary content\nForgeOps local agent\n")
	}
	if err := os.WriteFile(path, content, 0o755); err != nil {
		t.Fatalf("write file: %v", err)
	}
	return path
}

func TestVerifyBinary_OnlyAuthenticForgeOps(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()

	binName := TargetBinaryName()

	// 1. Legitimate fake binary with ForgeOps signature
	legit := createFakeAgentBinary(t, filepath.Join(tmp, "legit"), binName, true)
	ok, _, _, _ := VerifyBinary(ctx, legit)
	if !ok {
		t.Errorf("VerifyBinary(%s) = false, want true", legit)
	}

	// 2. Unrelated binary with same name but without signature
	unrelated := createFakeAgentBinary(t, filepath.Join(tmp, "unrelated"), binName, false)
	okUnrelated, _, _, _ := VerifyBinary(ctx, unrelated)
	if okUnrelated {
		t.Errorf("VerifyBinary(%s) = true, want false for non-ForgeOps binary", unrelated)
	}

	// 3. Different binary name (e.g. some other agent)
	otherName := createFakeAgentBinary(t, filepath.Join(tmp, "other"), "other-agent.exe", true)
	okOther, _, _, _ := VerifyBinary(ctx, otherName)
	if okOther {
		t.Errorf("VerifyBinary(%s) = true, want false for mismatched binary name", otherName)
	}
}

func TestDiscoverInstallations_FindsMultipleAndIgnoresUnrelated(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()

	binName := TargetBinaryName()

	dir1 := filepath.Join(tmp, "install1")
	dir2 := filepath.Join(tmp, "install2")
	dir3 := filepath.Join(tmp, "unrelated")

	createFakeAgentBinary(t, dir1, binName, true)
	createFakeAgentBinary(t, dir2, binName, true)
	createFakeAgentBinary(t, dir3, "another-agent.exe", true)

	found := DiscoverInDirs(ctx, []string{dir1, dir2, dir3})
	if len(found) != 2 {
		t.Fatalf("DiscoverInstallations found %d, want exactly 2", len(found))
	}

	paths := make(map[string]bool)
	for _, inst := range found {
		paths[inst.Path] = true
		if !inst.Verified {
			t.Errorf("installation %s should be verified", inst.Path)
		}
	}

	if !paths[filepath.Join(dir1, binName)] || !paths[filepath.Join(dir2, binName)] {
		t.Errorf("expected both install1 and install2 to be discovered, got %v", paths)
	}
}

func TestUninstallAll_RemovesVerifiedAndPreservesUnrelated(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()

	binName := TargetBinaryName()

	forgeopsDir := filepath.Join(tmp, "Programs", "ForgeOps")
	unrelatedDir := filepath.Join(tmp, "OtherApp")

	agentPath := createFakeAgentBinary(t, forgeopsDir, binName, true)
	unrelatedPath := createFakeAgentBinary(t, unrelatedDir, "important-service.exe", false)

	// Dry run first
	dryReport, err := UninstallAll(ctx, Options{
		DryRun:     true,
		SearchDirs: []string{forgeopsDir, unrelatedDir},
	})
	if err != nil {
		t.Fatalf("UninstallAll dry run error: %v", err)
	}
	if len(dryReport.Found) != 1 {
		t.Errorf("Dry run found %d installations, want 1", len(dryReport.Found))
	}
	if _, err := os.Stat(agentPath); os.IsNotExist(err) {
		t.Fatal("Dry run must NOT remove the file")
	}

	// Real uninstall
	realReport, err := UninstallAll(ctx, Options{
		DryRun:     false,
		SearchDirs: []string{forgeopsDir, unrelatedDir},
	})
	if err != nil {
		t.Fatalf("UninstallAll error: %v", err)
	}

	if !realReport.VerifiedClean {
		t.Errorf("UninstallAll Report.VerifiedClean = false, want true")
	}
	if len(realReport.Removed) != 1 {
		t.Errorf("UninstallAll removed %d items, want 1", len(realReport.Removed))
	}

	// Verify agent was removed
	if _, err := os.Stat(agentPath); !os.IsNotExist(err) {
		t.Errorf("Agent file %s should have been deleted", agentPath)
	}

	// Verify unrelated app was untouched
	if _, err := os.Stat(unrelatedPath); os.IsNotExist(err) {
		t.Errorf("Unrelated file %s must NEVER be deleted", unrelatedPath)
	}

	// Idempotency: run again on clean state
	repeatReport, err := UninstallAll(ctx, Options{
		DryRun:     false,
		SearchDirs: []string{forgeopsDir, unrelatedDir},
	})
	if err != nil {
		t.Fatalf("Repeated UninstallAll error: %v", err)
	}
	if len(repeatReport.Found) != 0 {
		t.Errorf("Second run found %d installations, want 0", len(repeatReport.Found))
	}
	if !repeatReport.VerifiedClean {
		t.Errorf("Second run must report VerifiedClean = true")
	}
}
