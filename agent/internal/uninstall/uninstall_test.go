// SPDX-License-Identifier: Apache-2.0
package uninstall

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"
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

func TestKnownDefaultDirectories(t *testing.T) {
	dirs := KnownDefaultDirectories()
	if len(dirs) == 0 {
		t.Errorf("KnownDefaultDirectories() returned empty list")
	}
}

func TestDiscoverInstallations_WithPath(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()
	binName := TargetBinaryName()
	extraDir := filepath.Join(tmp, "extra_bin")
	createFakeAgentBinary(t, extraDir, binName, true)

	found := DiscoverInstallations(ctx, extraDir)
	var foundExtra bool
	for _, inst := range found {
		if filepath.Dir(inst.Path) == extraDir {
			foundExtra = true
			break
		}
	}
	if !foundExtra {
		t.Errorf("DiscoverInstallations did not discover binary in extraSearchPaths: %s", extraDir)
	}
}

func TestFindRunningProcesses_EmptyAndMock(t *testing.T) {
	ctx := context.Background()
	// Should not panic with empty installations
	procs := FindRunningProcesses(ctx, nil)
	_ = procs

	// Should not panic with dummy installation
	procs = FindRunningProcesses(ctx, []Installation{
		{Path: filepath.Join(t.TempDir(), TargetBinaryName())},
	})
	_ = procs
}

func TestStopProcess_Validation(t *testing.T) {
	ctx := context.Background()
	err := StopProcess(ctx, ProcessInfo{PID: -1})
	if err == nil {
		t.Errorf("StopProcess with negative PID must return error")
	}
}

func TestCleanUserPathWindows(t *testing.T) {
	tmp := t.TempDir()
	_, _ = CleanUserPathWindows(tmp)
}

func TestUninstallAll_EmptyDiscovery(t *testing.T) {
	ctx := context.Background()
	emptyDir := filepath.Join(t.TempDir(), "empty_dir")
	_ = os.MkdirAll(emptyDir, 0o755)

	report, err := UninstallAll(ctx, Options{
		SearchDirs: []string{emptyDir},
	})
	if err != nil {
		t.Fatalf("UninstallAll error on empty dir: %v", err)
	}
	if !report.VerifiedClean {
		t.Errorf("expected VerifiedClean = true on empty dir")
	}
	if len(report.Found) != 0 {
		t.Errorf("expected 0 installations found, got %d", len(report.Found))
	}
}

func TestUninstallAll_WipeCredentials(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()
	binName := TargetBinaryName()
	agentDir := filepath.Join(tmp, "ForgeOps")
	createFakeAgentBinary(t, agentDir, binName, true)

	report, err := UninstallAll(ctx, Options{
		DryRun:          false,
		WipeCredentials: true,
		SearchDirs:      []string{agentDir},
	})
	if err != nil {
		t.Fatalf("UninstallAll error with WipeCredentials: %v", err)
	}
	if report.CredentialsPreserved {
		t.Errorf("report.CredentialsPreserved must be false when WipeCredentials is true")
	}
}

func TestVerifyBinary_EdgeCases(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()

	// 1. Non-existent path
	ok, _, _, _ := VerifyBinary(ctx, filepath.Join(tmp, "does_not_exist"))
	if ok {
		t.Errorf("VerifyBinary on non-existent path returned true")
	}

	// 2. Directory path
	subDir := filepath.Join(tmp, "subdir")
	_ = os.MkdirAll(subDir, 0o755)
	ok, _, _, _ = VerifyBinary(ctx, subDir)
	if ok {
		t.Errorf("VerifyBinary on directory returned true")
	}

	// 3. Empty file
	emptyFile := filepath.Join(tmp, TargetBinaryName())
	_ = os.WriteFile(emptyFile, []byte{}, 0o755)
	ok, _, _, _ = VerifyBinary(ctx, emptyFile)
	if ok {
		t.Errorf("VerifyBinary on empty file returned true")
	}
}

func TestVerifyBinary_WithRealVersionOutput(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()
	binName := TargetBinaryName()
	binPath := filepath.Join(tmp, binName)

	if runtime.GOOS == "windows" {
		src := `package main
import (
	"fmt"
	"os"
)
func main() {
	if len(os.Args) > 1 && os.Args[1] == "version" {
		fmt.Println("forgeops-agent 0.1.3 (commit: abc1234, built: 2026-10-24T00:00:00Z)")
		return
	}
}
`
		srcPath := filepath.Join(tmp, "main.go")
		if err := os.WriteFile(srcPath, []byte(src), 0o644); err != nil {
			t.Fatalf("write main.go: %v", err)
		}
		buildCmd := exec.Command("go", "build", "-o", binPath, srcPath)
		if err := buildCmd.Run(); err != nil {
			t.Fatalf("go build fake agent: %v", err)
		}
	} else {
		script := `#!/bin/sh
if [ "$1" = "version" ]; then
	echo "forgeops-agent 0.1.3 (commit: abc1234, built: 2026-10-24T00:00:00Z)"
fi
`
		if err := os.WriteFile(binPath, []byte(script), 0o755); err != nil {
			t.Fatalf("write fake agent script: %v", err)
		}
	}

	ok, ver, commit, date := VerifyBinary(ctx, binPath)
	if !ok {
		t.Fatalf("VerifyBinary failed for version-answering binary")
	}
	if ver != "0.1.3" {
		t.Errorf("expected version 0.1.3, got %s", ver)
	}
	if commit != "abc1234" {
		t.Errorf("expected commit abc1234, got %s", commit)
	}
	if date != "2026-10-24T00:00:00Z" {
		t.Errorf("expected date 2026-10-24T00:00:00Z, got %s", date)
	}
}

func TestDiscoverInDirs_DefaultAndCurrentExe(t *testing.T) {
	ctx := context.Background()
	currentExe, err := os.Executable()
	if err != nil || currentExe == "" {
		t.Fatalf("no current executable: %v", err)
	}
	defDirs := KnownDefaultDirectories()
	candidateDirs := []string{defDirs[0], filepath.Dir(currentExe), ""}
	_ = DiscoverInDirs(ctx, candidateDirs)
}

func TestStopProcess_LiveProcess(t *testing.T) {
	ctx := context.Background()
	var cmd *exec.Cmd
	if runtime.GOOS == "windows" {
		cmd = exec.Command("ping", "127.0.0.1", "-n", "10")
	} else {
		cmd = exec.Command("sleep", "10")
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("cannot start dummy process: %v", err)
	}
	defer func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
	}()

	err := StopProcess(ctx, ProcessInfo{PID: cmd.Process.Pid, Name: "dummy"})
	if err != nil {
		t.Errorf("StopProcess failed: %v", err)
	}
}

func TestUninstallAll_ForgeOpsDirectoryCleanup(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()
	binName := TargetBinaryName()

	// 1. Directory that should be removed when empty
	dirEmpty := filepath.Join(tmp, "TestEmpty", "ForgeOps")
	createFakeAgentBinary(t, dirEmpty, binName, true)

	// 2. Directory that should be preserved because it has other files
	dirNonEmpty := filepath.Join(tmp, "TestNonEmpty", "ForgeOps")
	createFakeAgentBinary(t, dirNonEmpty, binName, true)
	if err := os.WriteFile(filepath.Join(dirNonEmpty, "user-config.yaml"), []byte("config"), 0o644); err != nil {
		t.Fatalf("write user-config: %v", err)
	}

	report, err := UninstallAll(ctx, Options{
		SearchDirs: []string{dirEmpty, dirNonEmpty},
	})
	if err != nil {
		t.Fatalf("UninstallAll failed: %v", err)
	}
	if !report.VerifiedClean {
		t.Errorf("expected VerifiedClean = true")
	}

	// dirEmpty should be deleted
	if _, err := os.Stat(dirEmpty); !os.IsNotExist(err) {
		t.Errorf("expected empty ForgeOps dir %s to be deleted", dirEmpty)
	}
	// dirNonEmpty should be preserved
	if _, err := os.Stat(dirNonEmpty); os.IsNotExist(err) {
		t.Errorf("expected non-empty ForgeOps dir %s to be preserved", dirNonEmpty)
	}
}

func TestUninstallAll_PartiallyUninstalledWhenFileLocked(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("platform-only: windows - file handle locking during open is an NTFS-specific behaviour")
	}
	ctx := context.Background()
	tmp := t.TempDir()
	binName := TargetBinaryName()
	agentDir := filepath.Join(tmp, "locked_dir")
	fakePath := createFakeAgentBinary(t, agentDir, binName, true)

	// Keep an exclusive handle to the file so os.Remove fails on Windows
	f, err := os.OpenFile(fakePath, os.O_RDWR, 0)
	if err != nil {
		t.Fatalf("cannot lock file: %v", err)
	}
	defer f.Close()

	report, err := UninstallAll(ctx, Options{
		SearchDirs: []string{agentDir},
	})
	if err != nil {
		t.Fatalf("UninstallAll failed: %v", err)
	}
	if report.VerifiedClean {
		t.Errorf("expected VerifiedClean = false for locked file")
	}
	if len(report.Skipped) == 0 && len(report.LeftBehind) == 0 {
		t.Errorf("expected skipped or left behind items for locked file")
	}
}

func TestUninstallAll_WithRunningProcess(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("platform-only: windows - process inspection and taskkill are Windows-specific here")
	}
	ctx := context.Background()
	tmp := t.TempDir()
	binName := TargetBinaryName()
	agentDir := filepath.Join(tmp, "ForgeOps")
	_ = os.MkdirAll(agentDir, 0o755)
	agentExe := filepath.Join(agentDir, binName)

	// Copy cmd.exe so we can run a dummy forgeops-agent.exe process
	cmdExe := filepath.Join(os.Getenv("SystemRoot"), "System32", "cmd.exe")
	input, err := os.ReadFile(cmdExe)
	if err != nil {
		t.Fatalf("cannot read cmd.exe: %v", err)
	}
	// Append forgeops agent signature to make it authentic
	input = append(input, []byte("\n"+"github.com/parag8487/ForgeOps/agent\nForgeOps local agent\n")...)
	if err := os.WriteFile(agentExe, input, 0o755); err != nil {
		t.Fatalf("write fake agent exe: %v", err)
	}

	// Spawn dummy process running forgeops-agent.exe
	cmd := exec.Command(agentExe, "/c", "ping 127.0.0.1 -n 30 >nul")
	if err := cmd.Start(); err != nil {
		t.Fatalf("start dummy process: %v", err)
	}
	defer func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
	}()

	time.Sleep(300 * time.Millisecond)

	report, err := UninstallAll(ctx, Options{
		DryRun:     false,
		SearchDirs: []string{agentDir},
	})
	if err != nil {
		t.Fatalf("UninstallAll with running process failed: %v", err)
	}
	_ = report
}
