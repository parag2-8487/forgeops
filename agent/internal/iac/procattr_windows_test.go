// SPDX-License-Identifier: Apache-2.0
//go:build windows

package iac

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Windows process-tree termination (design §8.2, §10.11, D-37).
//
// The case that matters is a DETACHED grandchild. `taskkill /T` walks the parent-child
// links Windows records, and a process created with DETACHED_PROCESS has no such link —
// so taskkill reports success while the grandchild keeps running and keeps the pipe's
// write end open. A Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE contains it
// regardless, because containment is kernel-enforced rather than inferred from parentage.
//
// The test spawns child -> detached grandchild for real. It uses PowerShell rather than a
// compiled helper so it needs no build step and no fixture binary, and it detects survival
// by whether the grandchild's marker file keeps growing after termination — an assertion
// about observable behaviour rather than about a PID table.

// writeFixtureScripts writes two PowerShell scripts: a grandchild that appends to
// markerPath forever, and a parent that starts it DETACHED and then sleeps.
//
// Two files rather than one nested here-string, because a PowerShell script embedded in a
// Go raw string cannot contain a backtick — PowerShell's line-continuation and escape
// character is the same byte that ends a Go raw string literal. Separate files remove the
// quoting problem entirely and make the fixture readable.
func writeFixtureScripts(t *testing.T, dir, markerPath string) string {
	t.Helper()

	grandchild := filepath.Join(dir, "grandchild.ps1")
	grandchildBody := strings.Join([]string{
		"while ($true) {",
		"  Add-Content -Path '" + markerPath + "' -Value 'tick'",
		"  Start-Sleep -Milliseconds 100",
		"}",
	}, "\n")
	if err := os.WriteFile(grandchild, []byte(grandchildBody), 0o600); err != nil {
		t.Fatalf("writing grandchild script: %v", err)
	}

	parent := filepath.Join(dir, "parent.ps1")
	// -WindowStyle Hidden with Start-Process gives the grandchild no parent link for
	// `taskkill /T` to follow, which is the whole point of the fixture.
	parentBody := strings.Join([]string{
		"Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList " +
			"'-NoProfile','-NonInteractive','-File','" + grandchild + "'",
		"Start-Sleep -Seconds 60",
	}, "\n")
	if err := os.WriteFile(parent, []byte(parentBody), 0o600); err != nil {
		t.Fatalf("writing parent script: %v", err)
	}
	return parent
}

func countLines(t *testing.T, path string) int {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	return strings.Count(string(data), "tick")
}

func TestTerminateGroup_KillsADetachedGrandchild(t *testing.T) {
	if _, err := exec.LookPath("powershell.exe"); err != nil {
		t.Skip("powershell.exe is not available")
	}

	dir := t.TempDir()
	marker := filepath.Join(dir, "grandchild.log")
	script := writeFixtureScripts(t, dir, marker)

	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-File", script) //nolint:gosec // fixed argv
	setProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	attachProcessGroup(cmd)

	// Wait until the grandchild is demonstrably running, so a passing test cannot mean
	// "it never started".
	deadline := time.Now().Add(20 * time.Second)
	for countLines(t, marker) < 2 {
		if time.Now().After(deadline) {
			_ = cmd.Process.Kill()
			t.Skip("the detached grandchild never started; nothing to prove about termination")
		}
		time.Sleep(100 * time.Millisecond)
	}

	terminateGroup(cmd, 500*time.Millisecond)
	_ = cmd.Wait()

	// Let any surviving grandchild write a few more ticks. If it is gone, the count
	// stops moving.
	before := countLines(t, marker)
	time.Sleep(1500 * time.Millisecond)
	after := countLines(t, marker)

	if after > before {
		t.Fatalf(
			"the detached grandchild survived termination: marker grew from %d to %d ticks. "+
				"This is exactly the case `taskkill /T` misses, and the Job Object is supposed "+
				"to close it (D-37).",
			before, after,
		)
	}
}

func TestTerminateGroup_KillsAnOrdinaryChild(t *testing.T) {
	if _, err := exec.LookPath("powershell.exe"); err != nil {
		t.Skip("powershell.exe is not available")
	}

	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 60")
	setProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	attachProcessGroup(cmd)
	pid := cmd.Process.Pid

	if !processIsAlive(pid) {
		t.Fatal("the child was not alive after Start")
	}

	terminateGroup(cmd, 200*time.Millisecond)
	_ = cmd.Wait()

	deadline := time.Now().Add(10 * time.Second)
	for processIsAlive(pid) {
		if time.Now().After(deadline) {
			t.Fatalf("pid %d survived terminateGroup", pid)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestTerminateGroup_IsSafeOnAnUnstartedCommand(t *testing.T) {
	t.Parallel()

	// terminateGroup runs from a sync.Once in the runner's timeout path, which can fire
	// before Start on a cancelled context. It must not panic or leak.
	cmd := exec.Command("powershell.exe", "-NoProfile", "-Command", "exit")
	setProcessGroup(cmd)
	terminateGroup(cmd, 0) // no process yet
}

func TestTerminateGroup_IsIdempotent(t *testing.T) {
	if _, err := exec.LookPath("powershell.exe"); err != nil {
		t.Skip("powershell.exe is not available")
	}

	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 30")
	setProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	attachProcessGroup(cmd)

	// The second call must find no job (the first deleted it) and fall through to
	// Process.Kill without closing a handle twice — closing a Windows handle twice is
	// undefined behaviour, not a no-op.
	terminateGroup(cmd, 100*time.Millisecond)
	terminateGroup(cmd, 100*time.Millisecond)
	_ = cmd.Wait()
}

func TestSetProcessGroup_StoresExactlyOneJobPerCommand(t *testing.T) {
	t.Parallel()

	// A leaked job handle keeps every process in it alive from the kernel's point of
	// view, so the bookkeeping is worth asserting directly.
	cmd := exec.Command("powershell.exe", "-NoProfile", "-Command", "exit")
	setProcessGroup(cmd)

	if _, ok := jobs.Load(cmd); !ok {
		t.Skip("job object creation is unavailable in this environment")
	}

	terminateGroup(cmd, 0)
	if _, ok := jobs.Load(cmd); ok {
		t.Error("the job mapping survived terminateGroup; the handle is leaked")
	}
}

func TestProcessIsAlive_ReportsFalseForAnUnusedPid(t *testing.T) {
	t.Parallel()

	// PID 0 is the System Idle Process and cannot be opened for query, so it stands in
	// for "not a process we can observe". Guards against processIsAlive returning true
	// on error, which would make the grace period never expire.
	if processIsAlive(0) {
		t.Error("processIsAlive(0) = true")
	}
	// A very high PID is almost certainly unused.
	if processIsAlive(0x7ffffffe) {
		t.Skip("that PID happens to exist on this machine")
	}
}

func TestNoTaskkillRemains(t *testing.T) {
	t.Parallel()

	// D-37 replaced the subprocess call. Terminating a runaway process by launching
	// another process needs the machine healthy enough to launch one, which is exactly
	// what is in doubt. Asserted against the source so a future edit cannot quietly
	// reintroduce it.
	data, err := os.ReadFile("procattr_windows.go")
	if err != nil {
		t.Fatalf("reading procattr_windows.go: %v", err)
	}
	text := string(data)
	// The word appears in the explanatory comment; what must not appear is a call.
	if strings.Contains(text, `exec.Command("taskkill"`) || strings.Contains(text, "strconv.Itoa(cmd.Process.Pid)") {
		t.Error("taskkill is still invoked")
	}
}
