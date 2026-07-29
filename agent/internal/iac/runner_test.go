// SPDX-License-Identifier: Apache-2.0
package iac

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

// writeFakeScript creates a temporary script file that can be used as a fake
// tofu binary. On Windows it writes a .bat file; on Unix a shell script.
func writeFakeScript(t *testing.T, dir, name, content string) string {
	t.Helper()
	var path string
	if runtime.GOOS == "windows" {
		path = filepath.Join(dir, name+".bat")
		// Windows batch files need @echo off and explicit exit codes.
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatalf("write script: %v", err)
		}
	} else {
		path = filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(content), 0o755); err != nil {
			t.Fatalf("write script: %v", err)
		}
	}
	return path
}

// scriptExit0 returns a script that prints to stdout and stderr then exits 0.
func scriptExit0() string {
	if runtime.GOOS == "windows" {
		return "@echo off\r\necho stdout-line-1\r\necho stdout-line-2\r\necho stderr-line-1 1>&2\r\nexit /b 0\r\n"
	}
	return "#!/bin/sh\necho stdout-line-1\necho stdout-line-2\necho stderr-line-1 >&2\nexit 0\n"
}

// scriptExitNonZero returns a script that exits with a specific code.
func scriptExitNonZero(code int) string {
	if runtime.GOOS == "windows" {
		return "@echo off\r\necho error-output 1>&2\r\nexit /b " + itoa(code) + "\r\n"
	}
	return "#!/bin/sh\necho error-output >&2\nexit " + itoa(code) + "\n"
}

// scriptExitCode2 returns a script that mimics tofu plan with changes (exit 2).
func scriptExitCode2() string {
	if runtime.GOOS == "windows" {
		return "@echo off\r\necho plan-has-changes\r\nexit /b 2\r\n"
	}
	return "#!/bin/sh\necho plan-has-changes\nexit 2\n"
}

// scriptSleepForever returns a script that sleeps indefinitely (for timeout tests).
func scriptSleepForever() string {
	if runtime.GOOS == "windows" {
		// Use a pure-batch infinite loop with a small delay via >nul 2>nul.
		// This does NOT depend on external tools being in PATH.
		return "@echo off\r\n:loop\r\nwaitfor /t 3600 SomethingThatWillNeverHappen >nul 2>nul\r\ngoto loop\r\n"
	}
	return "#!/bin/sh\nsleep 3600\n"
}

// scriptValidateJSON returns a script that outputs JSON like tofu validate -json.
func scriptValidateJSON() string {
	jsonOut := `{"valid":true,"error_count":0,"warning_count":0,"diagnostics":[]}`
	if runtime.GOOS == "windows" {
		return "@echo off\r\necho " + jsonOut + "\r\nexit /b 0\r\n"
	}
	return "#!/bin/sh\necho '" + jsonOut + "'\nexit 0\n"
}

// itoa is a simple int-to-string without importing strconv for test brevity.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	s := ""
	neg := false
	if n < 0 {
		neg = true
		n = -n
	}
	for n > 0 {
		s = string(rune('0'+n%10)) + s
		n /= 10
	}
	if neg {
		s = "-" + s
	}
	return s
}

func newTestRunner(t *testing.T, binaryPath string) *TofuRunner {
	t.Helper()
	cfg := TofuConfig{
		BinaryPath:     binaryPath,
		DefaultTimeout: 30 * time.Second,
		KillGrace:      2 * time.Second,
		MaxLineBytes:   256, // small for testing
	}
	logger := zap.NewNop()
	tracer := telemetry.NoopTracer{}
	return NewTofuRunner(cfg, logger, tracer)
}

func TestErrTofuNotFound(t *testing.T) {
	runner := newTestRunner(t, "nonexistent-binary-that-does-not-exist-xyz123")

	_, err := runner.Validate(context.Background(), t.TempDir())
	if err == nil {
		t.Fatal("expected error for missing binary")
	}
	if !errors.Is(err, ErrTofuNotFound) {
		t.Fatalf("expected ErrTofuNotFound, got: %v", err)
	}
}

func TestValidateSuccess(t *testing.T) {
	dir := t.TempDir()
	script := writeFakeScript(t, dir, "tofu", scriptValidateJSON())

	runner := newTestRunner(t, script)
	result, err := runner.Validate(context.Background(), dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.ExitCode != 0 {
		t.Fatalf("expected exit code 0, got %d", result.ExitCode)
	}
	if result.Duration <= 0 {
		t.Fatal("expected positive duration")
	}
	if result.Diagnostics == nil {
		t.Fatal("expected diagnostics JSON")
	}
}

func TestExitCodeCapture(t *testing.T) {
	dir := t.TempDir()
	script := writeFakeScript(t, dir, "tofu", scriptExitNonZero(1))

	runner := newTestRunner(t, script)
	result, err := runner.Validate(context.Background(), dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.ExitCode != 1 {
		t.Fatalf("expected exit code 1, got %d", result.ExitCode)
	}
}

func TestPlanExitCode2HasChanges(t *testing.T) {
	dir := t.TempDir()
	script := writeFakeScript(t, dir, "tofu", scriptExitCode2())

	runner := newTestRunner(t, script)
	result, err := runner.Plan(context.Background(), dir, PlanOptions{})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.ExitCode != 2 {
		t.Fatalf("expected exit code 2, got %d", result.ExitCode)
	}
	if !result.HasChanges {
		t.Fatal("expected HasChanges to be true for exit code 2")
	}
}

func TestInterleavedStreams(t *testing.T) {
	dir := t.TempDir()
	script := writeFakeScript(t, dir, "tofu", scriptExit0())

	var sinkLines []string
	// LineSink is invoked from BOTH the stdout and stderr scanner goroutines
	// concurrently, so the shared slice needs a mutex. Without it this is a real
	// data race — the race detector caught it on Linux CI even though a local
	// Windows run happened not to hit the interleaving.
	var sinkMu sync.Mutex
	runner := newTestRunner(t, script)
	runner.SetSink(func(stream string, line string) {
		sinkMu.Lock()
		defer sinkMu.Unlock()
		sinkLines = append(sinkLines, stream+":"+line)
	})

	result, err := runner.Validate(context.Background(), dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(result.Stdout) == 0 {
		t.Fatal("expected stdout lines")
	}
	if len(result.Stderr) == 0 {
		t.Fatal("expected stderr lines")
	}
	// Snapshot under the same lock the sink used, then assert on the copy.
	sinkMu.Lock()
	sinkSnapshot := append([]string(nil), sinkLines...)
	sinkMu.Unlock()

	if len(sinkSnapshot) == 0 {
		t.Fatal("expected sink to receive lines")
	}

	// Verify streams are labeled correctly.
	hasStdout := false
	hasStderr := false
	for _, l := range sinkSnapshot {
		if strings.HasPrefix(l, "stdout:") {
			hasStdout = true
		}
		if strings.HasPrefix(l, "stderr:") {
			hasStderr = true
		}
	}
	if !hasStdout || !hasStderr {
		t.Fatalf("expected both stdout and stderr in sink, got: %v", sinkSnapshot)
	}
}

func TestTimeoutFromContext(t *testing.T) {
	// Use a separate manually-managed dir since killed processes on Windows
	// may hold directory handles briefly, causing t.TempDir() cleanup to fail.
	workdir, err := os.MkdirTemp("", "iac-timeout-work-*")
	if err != nil {
		t.Fatalf("create workdir: %v", err)
	}
	defer func() {
		time.Sleep(100 * time.Millisecond)
		_ = os.RemoveAll(workdir)
	}()

	scriptDir, err := os.MkdirTemp("", "iac-timeout-script-*")
	if err != nil {
		t.Fatalf("create script dir: %v", err)
	}
	defer func() {
		time.Sleep(100 * time.Millisecond)
		_ = os.RemoveAll(scriptDir)
	}()
	script := writeFakeScript(t, scriptDir, "tofu", scriptSleepForever())

	runner := newTestRunner(t, script)

	// Use a very short deadline to trigger timeout.
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()

	_, err = runner.Validate(ctx, workdir)
	if err == nil {
		t.Fatal("expected timeout error")
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected context.DeadlineExceeded, got: %v", err)
	}
}

func TestDefaultTimeoutApplied(t *testing.T) {
	workdir, err := os.MkdirTemp("", "iac-deftimeout-work-*")
	if err != nil {
		t.Fatalf("create workdir: %v", err)
	}
	defer func() {
		time.Sleep(100 * time.Millisecond)
		_ = os.RemoveAll(workdir)
	}()

	scriptDir, err := os.MkdirTemp("", "iac-deftimeout-script-*")
	if err != nil {
		t.Fatalf("create script dir: %v", err)
	}
	defer func() {
		time.Sleep(100 * time.Millisecond)
		_ = os.RemoveAll(scriptDir)
	}()
	script := writeFakeScript(t, scriptDir, "tofu", scriptSleepForever())

	cfg := TofuConfig{
		BinaryPath:     script,
		DefaultTimeout: 500 * time.Millisecond, // very short
		KillGrace:      100 * time.Millisecond,
		MaxLineBytes:   256,
	}
	logger := zap.NewNop()
	tracer := telemetry.NoopTracer{}
	runner := NewTofuRunner(cfg, logger, tracer)

	// No deadline on context — should use DefaultTimeout.
	_, err = runner.Validate(context.Background(), workdir)
	if err == nil {
		t.Fatal("expected timeout error from DefaultTimeout")
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected context.DeadlineExceeded, got: %v", err)
	}
}

func TestOverlongLineTruncation(t *testing.T) {
	dir := t.TempDir()

	// MaxLineBytes in the test runner is 256. Write a line much longer.
	// bufio.Scanner with buffer size 256 will return at most 256 bytes per token.
	// Lines longer than the buffer are returned as partial scans — the scanner
	// splits at the buffer boundary.
	longLine := strings.Repeat("A", 300)
	var scriptContent string
	if runtime.GOOS == "windows" {
		// On Windows, we use a Go helper to produce the long line because
		// batch echo is unreliable for very long lines.
		goHelper := `package main

import "fmt"

func main() {
	fmt.Println("` + longLine + `")
}
`
		helperDir := t.TempDir()
		helperPath := filepath.Join(helperDir, "helper.go")
		if err := os.WriteFile(helperPath, []byte(goHelper), 0o644); err != nil {
			t.Fatalf("write go helper: %v", err)
		}
		// Build the helper.
		scriptContent = "@echo off\r\ngo run " + helperPath + "\r\nexit /b 0\r\n"
	} else {
		scriptContent = "#!/bin/sh\nprintf '%s\\n' '" + longLine + "'\nexit 0\n"
	}

	script := writeFakeScript(t, dir, "tofu", scriptContent)
	runner := newTestRunner(t, script)

	result, err := runner.Validate(context.Background(), dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// The scanner with a 256-byte buffer should either truncate or split the line.
	// In either case, no single captured line should exceed MaxLineBytes.
	for _, line := range result.Stdout {
		if len(line) > runner.cfg.MaxLineBytes {
			t.Fatalf("line exceeds MaxLineBytes (%d): got %d bytes", runner.cfg.MaxLineBytes, len(line))
		}
	}
}

func TestCompletionOrdering(t *testing.T) {
	// This test verifies that stdout and stderr are fully drained before
	// the result is returned (i.e., no data loss from goroutines).
	dir := t.TempDir()
	script := writeFakeScript(t, dir, "tofu", scriptExit0())

	runner := newTestRunner(t, script)
	result, err := runner.Validate(context.Background(), dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// The script outputs 2 stdout lines and 1 stderr line.
	// Verify we got them all (completion ordering ensures drain before Wait).
	if len(result.Stdout) < 2 {
		t.Fatalf("expected at least 2 stdout lines, got %d: %v", len(result.Stdout), result.Stdout)
	}
	if len(result.Stderr) < 1 {
		t.Fatalf("expected at least 1 stderr line, got %d: %v", len(result.Stderr), result.Stderr)
	}
}

func TestPlanWithOptions(t *testing.T) {
	dir := t.TempDir()
	// Use a script that exits 0 to test option passing doesn't break anything.
	script := writeFakeScript(t, dir, "tofu", scriptExit0())

	runner := newTestRunner(t, script)
	opts := PlanOptions{
		VarFiles: []string{"vars.tfvars"},
		Vars:     map[string]string{"region": "us-east-1"},
		Target:   []string{"module.vpc"},
		Lock:     false,
	}

	result, err := runner.Plan(context.Background(), dir, opts)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Script exits 0 so no changes.
	if result.HasChanges {
		t.Fatal("expected no changes for exit code 0")
	}
}

func TestNewTofuRunnerDefaults(t *testing.T) {
	cfg := TofuConfig{} // all zero-values
	runner := NewTofuRunner(cfg, zap.NewNop(), telemetry.NoopTracer{})

	if runner.cfg.BinaryPath != "tofu" {
		t.Fatalf("expected default binary path 'tofu', got %q", runner.cfg.BinaryPath)
	}
	if runner.cfg.DefaultTimeout != 5*time.Minute {
		t.Fatalf("expected default timeout 5m, got %v", runner.cfg.DefaultTimeout)
	}
	if runner.cfg.KillGrace != 10*time.Second {
		t.Fatalf("expected default kill grace 10s, got %v", runner.cfg.KillGrace)
	}
	if runner.cfg.MaxLineBytes != 64*1024 {
		t.Fatalf("expected default MaxLineBytes 64KiB, got %d", runner.cfg.MaxLineBytes)
	}
}
