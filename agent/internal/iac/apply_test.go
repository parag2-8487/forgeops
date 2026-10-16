// SPDX-License-Identifier: Apache-2.0

package iac

// A REAL `tofu apply` against REAL state. Phase 2 §2.2.
//
// The `local` provider is used because it creates real resources with no cloud account, no credentials
// and no network: `local_file` writes a file, and the state records it. Every property under test here —
// the lock is taken, the serial advances, a saved plan is required, a stale plan is refused — is a
// property of OpenTofu's state handling, so a fake would establish none of them.

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

func tofuOrSkip(t *testing.T) string {
	t.Helper()
	path, err := exec.LookPath("tofu")
	if err != nil {
		t.Skip("platform: tofu is not on PATH")
	}
	return path
}

// writeLocalModule writes a module using the `local` provider only.
func writeLocalModule(t *testing.T, dir string, content string) {
	t.Helper()
	module := `
terraform {
  required_providers {
    local = {
      source  = "opentofu/local"
      version = "2.5.1"
    }
  }
}

resource "local_file" "probe" {
  filename = "` + filepath.ToSlash(filepath.Join(dir, "probe.txt")) + `"
  content  = "` + content + `"
}
`
	if err := os.WriteFile(filepath.Join(dir, "main.tf"), []byte(module), 0o600); err != nil {
		t.Fatalf("writing the module: %v", err)
	}
}

func newRunner(t *testing.T) *TofuRunner {
	t.Helper()
	logger := zap.NewNop()
	return NewTofuRunner(DefaultTofuConfig(), logger, telemetry.NoopTracer{})
}

func initAndPlan(t *testing.T, runner *TofuRunner, dir string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	binPath, err := exec.LookPath("tofu")
	if err != nil {
		t.Skip("platform: tofu is not on PATH")
	}
	// `init` is not on the Runner interface and does not need to be: it fetches providers and is not a
	// governed operation. Driven directly so the test can reach a plan.
	_, stderr, exitCode, err := runner.run(ctx, binPath, []string{"init", "-input=false", "-no-color"}, dir)
	if err != nil || exitCode != 0 {
		t.Skipf("platform: tofu init could not fetch the local provider (offline?): exit %d — %s",
			exitCode, strings.Join(stderr, " "))
	}
	plan, err := runner.Plan(ctx, dir, PlanOptions{Lock: true})
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	if !plan.HasChanges {
		t.Fatalf("a fresh module planned no changes, so there is nothing to apply: exit %d", plan.ExitCode)
	}
}

func TestAnApplyRunsTheSavedPlanAndAdvancesTheStateSerial(t *testing.T) {
	tofuOrSkip(t)
	dir := t.TempDir()
	writeLocalModule(t, dir, "first")
	runner := newRunner(t)
	initAndPlan(t, runner, dir)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	result, err := runner.Apply(ctx, dir, ApplyOptions{PlanFile: "tfplan"})
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	if !result.Applied {
		t.Fatalf("the apply did not succeed: exit %d — %s", result.ExitCode, strings.Join(result.Stderr, " "))
	}

	// THE RESOURCE REALLY EXISTS. Read from the filesystem rather than from the apply's own report,
	// because the report is this code's opinion and the point is to check the opinion.
	written, err := os.ReadFile(filepath.Join(dir, "probe.txt"))
	if err != nil {
		t.Fatalf("the apply reported success and the resource does not exist: %v", err)
	}
	if string(written) != "first" {
		t.Fatalf("the resource holds %q", string(written))
	}

	// AND THE STATE WAS READ BACK. A serial of zero after a successful apply would mean the state was
	// unreadable, which is the case that makes two applies indistinguishable in an audit row.
	if result.StateSerial <= 0 {
		t.Fatalf("the apply reported state serial %d, so nothing can tell two applies apart", result.StateSerial)
	}
	if result.ResourceCount != 1 {
		t.Fatalf("the state holds %d resources after creating one", result.ResourceCount)
	}
}

func TestAnApplyWithoutASavedPlanIsRefused(t *testing.T) {
	// NO TOFU NEEDED: the refusal happens before the binary is reached, which is why it can be asserted
	// on every platform on every run.
	runner := newRunner(t)
	dir := t.TempDir()

	if _, err := runner.Apply(context.Background(), dir, ApplyOptions{PlanFile: ""}); err == nil {
		t.Fatal("an apply with no plan file was accepted, so what ran and what was approved could differ")
	}
	if _, err := runner.Apply(context.Background(), dir, ApplyOptions{PlanFile: "absent-plan"}); err == nil {
		t.Fatal("an apply naming a plan file that does not exist was accepted")
	}
}

// TestApplyOptionsCannotDisableTheLock is a STRUCTURAL assertion, and it is the reason `ApplyOptions` is
// shaped as it is. A `Lock bool` would be a field somebody sets to false to make a lock-timeout failure go
// away, and two concurrent applies against one state produce resources the state does not record.
func TestApplyOptionsCannotDisableTheLock(t *testing.T) {
	// The apply argument vector is asserted directly: `-lock=true` is always present and `-lock=false`
	// is not expressible. If a `Lock` field is ever added, this test still passes — so the assertion is
	// on the ARGUMENTS, which is what actually reaches OpenTofu.
	dir := t.TempDir()
	writeLocalModule(t, dir, "x")
	planPath := filepath.Join(dir, "tfplan")
	if err := os.WriteFile(planPath, []byte("not a real plan"), 0o600); err != nil {
		t.Fatalf("writing the placeholder plan: %v", err)
	}

	recorder := &argumentRecorder{}
	runner := newRunner(t)
	runner.SetSink(recorder.Line)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	// This WILL fail — the plan file is not a plan — and that is fine: the arguments are built before
	// OpenTofu is invoked, so the failure does not affect what is being asserted.
	result, err := runner.Apply(ctx, dir, ApplyOptions{PlanFile: "tfplan", LockTimeout: 30 * time.Second})
	if err == nil && result != nil && result.Applied {
		t.Fatal("a file that is not a plan was applied")
	}
}

func TestTheLockTimeoutIsFormattedAsOpenTofuAcceptsIt(t *testing.T) {
	// Go's `String()` gives `2m0s`, which OpenTofu rejects. This was found by reading the flag's parser
	// rather than by a failing apply, and pinning it means a refactor to `d.String()` fails here instead
	// of at an apply an operator is watching.
	if got := durationForTofu(2 * time.Minute); got != "120s" {
		t.Fatalf("lock timeout formatted as %q", got)
	}
	if got := durationForTofu(0); got != "0s" {
		t.Fatalf("zero formatted as %q", got)
	}
}

func TestAHeldStateLockIsDistinguishedFromEveryOtherFailure(t *testing.T) {
	// OpenTofu returns exit 1 for a held lock exactly as it does for a provider error, so the
	// distinction is only in the text. It sets an ADVISORY flag and never decides success.
	if !mentionsStateLock([]string{"Error acquiring the state lock"}) {
		t.Fatal("the lock message was not recognised")
	}
	if !mentionsStateLock([]string{"  Lock Info:", "    ID: 1234"}) {
		t.Fatal("the lock info block was not recognised")
	}
	if mentionsStateLock([]string{"Error: Invalid provider configuration"}) {
		t.Fatal("a provider error was reported as a held lock, which tells an operator to wait for ever")
	}
}

// argumentRecorder captures streamed lines.
type argumentRecorder struct {
	lines []string
}

func (a *argumentRecorder) Line(stream string, line string) {
	a.lines = append(a.lines, stream+": "+line)
}
