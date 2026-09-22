// SPDX-License-Identifier: Apache-2.0

package iac

// OpenTofu apply with state management — Phase 2 §2.2.
//
// WHY THIS IS A DIFFERENT PROBLEM FROM `kubectl apply`, which is what the box said and is the reason this
// was left until there was somewhere safe to put it:
//
//  1. STATE IS SHARED AND MUTABLE. `kubectl apply` is idempotent against the cluster's own record. `tofu
//     apply` reads and rewrites a state file that another apply may be holding, and two concurrent
//     applies against one state produce resources the state does not know about — which is not a failed
//     operation but a corrupted record of reality that the next plan proposes to "fix" by destroying
//     things.
//
//  2. THE LOCK IS THEREFORE NOT OPTIONAL HERE. `PlanOptions.Lock` exists and a plan may legitimately run
//     unlocked (it writes nothing). `ApplyOptions` HAS NO Lock FIELD AT ALL: an unlocked apply is not a
//     configuration this code will express. A caller who wants one has to change this file, which is the
//     point — the alternative is a boolean somebody sets to make a lock timeout go away.
//
//  3. AN APPLY IS ONLY EVER RUN FROM A SAVED PLAN. `tofu apply` with no plan file re-plans and applies
//     whatever it then finds, so what a human approved and what runs are two different computations
//     separated by however long the approval took. Applying the SAVED PLAN FILE means the approved
//     diff is the applied diff, and OpenTofu itself refuses a plan file that no longer matches the state.
//     That refusal is the safety property, and passing `-auto-approve` with no plan file would discard it.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

var (
	// ErrNoSavedPlan refuses an apply with no plan file. Not a fallback to re-planning: see (3) above.
	ErrNoSavedPlan = errors.New("iac: apply needs the saved plan the approval was granted for")
	// ErrStateLockHeld reports that another operation holds the state lock. A distinct error because it
	// is the one failure here that is expected, transient, and resolved by waiting rather than by
	// changing anything.
	ErrStateLockHeld = errors.New("iac: another operation holds the state lock")
)

// ApplyOptions configures an apply.
//
// NO `Lock` FIELD, deliberately — see (2). NO `Vars` or `VarFiles` either: the variables were resolved
// when the plan was made and are baked into the plan file, so accepting them here would let an apply run
// with values the plan never saw and the approval never covered.
type ApplyOptions struct {
	// PlanFile is the saved plan, absolute or relative to the workdir. Required.
	PlanFile string
	// LockTimeout is how long to wait for the state lock before giving up. Zero means the default
	// below rather than "do not wait": a zero wait turns every concurrent apply into a failure an
	// operator has to retry by hand.
	LockTimeout time.Duration
	// Refresh, when false, passes `-refresh=false`. Default is to refresh, because an apply against a
	// state that has drifted from reality is how a resource gets created twice.
	Refresh *bool
}

// ApplyResult holds the outcome of an apply.
type ApplyResult struct {
	ExitCode int
	// Applied is true only on exit 0. Not derived from parsing the output: OpenTofu's summary line has
	// changed format between versions, and an operation that reported success by matching a string
	// would report success against a version that phrases it differently.
	Applied bool
	// LockHeld distinguishes "somebody else is applying" from every other failure, so a caller can
	// retry rather than escalate.
	LockHeld bool
	// StateSerial is the state's serial number AFTER the apply, read with `tofu show -json`. This is
	// the fact that makes an apply auditable: two applies that both report success and leave the same
	// serial mean the second one did nothing, and without the serial they are indistinguishable.
	StateSerial int64
	// ResourceCount is how many resources the state holds afterwards. Read back, not counted from the
	// plan: the plan is an intention.
	ResourceCount int
	Stdout        []string
	Stderr        []string
	Duration      time.Duration
}

// DefaultLockTimeout is how long an apply waits for the state lock.
//
// Two minutes rather than zero or an hour. Zero makes every overlapping apply a hard failure; an hour
// means an operator watching a dashboard sees nothing happen and no explanation for most of it. Two
// minutes covers a normal apply finishing ahead of this one and fails fast enough to be reported.
const DefaultLockTimeout = 2 * time.Minute

// Apply runs `tofu apply` against a SAVED PLAN, holding the state lock.
func (r *TofuRunner) Apply(ctx context.Context, workdir string, opts ApplyOptions) (*ApplyResult, error) {
	ctx, span := r.tracer.StartSpan(ctx, "iac.Apply")
	defer span.End()

	start := time.Now()

	if strings.TrimSpace(opts.PlanFile) == "" {
		return nil, ErrNoSavedPlan
	}
	planPath := opts.PlanFile
	if !filepath.IsAbs(planPath) {
		planPath = filepath.Join(workdir, planPath)
	}
	if _, err := os.Stat(planPath); err != nil {
		// An absent plan file is refused rather than falling back to a fresh plan. A fresh plan would
		// apply whatever the world looks like NOW, which is not what anybody approved.
		return nil, fmt.Errorf("%w: %s is not readable", ErrNoSavedPlan, planPath)
	}

	binPath, err := r.resolvedBinary()
	if err != nil {
		return nil, err
	}

	ctx, cancel := r.ensureDeadline(ctx)
	defer cancel()

	timeout := opts.LockTimeout
	if timeout <= 0 {
		timeout = DefaultLockTimeout
	}
	// `-input=false` so a missing variable fails rather than blocking on a prompt no operator can see.
	// `-auto-approve` is NOT passed: applying a plan file implies it, and passing both is how a command
	// that was meant to need a plan file ends up working without one after an edit.
	args := []string{
		"apply",
		"-input=false",
		"-lock=true",
		fmt.Sprintf("-lock-timeout=%s", durationForTofu(timeout)),
	}
	if opts.Refresh != nil && !*opts.Refresh {
		args = append(args, "-refresh=false")
	}
	args = append(args, planPath)

	stdout, stderr, exitCode, err := r.run(ctx, binPath, args, workdir)
	if err != nil {
		return nil, err
	}

	result := &ApplyResult{
		ExitCode: exitCode,
		Applied:  exitCode == 0,
		Stdout:   stdout,
		Stderr:   stderr,
		Duration: time.Since(start),
	}
	if exitCode != 0 {
		result.LockHeld = mentionsStateLock(stderr) || mentionsStateLock(stdout)
		return result, nil
	}

	// THE STATE IS READ BACK. Everything above establishes that the command exited zero; this
	// establishes what the state now says, which is the part an audit row should carry. An apply whose
	// state cannot be read afterwards is reported as applied with a zero serial rather than as a
	// failure, because the resources DID change and pretending otherwise would be worse.
	serial, count := r.stateFacts(ctx, binPath, workdir)
	result.StateSerial = serial
	result.ResourceCount = count
	return result, nil
}

// stateFacts reads the serial and resource count from the RAW STATE, via `tofu state pull`.
//
// NOT `tofu show -json`. The first version used it and the serial came back zero every time, because
// `show -json` emits a rendered VIEW of the state -- `format_version`, `values.root_module` -- and has no
// `serial` field at all. The serial is a property of the state document itself, and `state pull` is the
// supported way to read that document without reaching into a backend-specific file path. Using the
// rendered view would have left an audit row unable to tell two applies apart, which is the one thing the
// serial is here for.
func (r *TofuRunner) stateFacts(ctx context.Context, binPath, workdir string) (serial int64, resources int) {
	stdout, _, exitCode, err := r.run(ctx, binPath, []string{"state", "pull"}, workdir)
	if err != nil || exitCode != 0 {
		return 0, 0
	}
	combined := strings.Join(stdout, "\n")
	if !json.Valid([]byte(combined)) {
		return 0, 0
	}
	var state struct {
		Serial    int64             `json:"serial"`
		Resources []json.RawMessage `json:"resources"`
	}
	if err := json.Unmarshal([]byte(combined), &state); err != nil {
		return 0, 0
	}
	// The raw state's `resources` is FLAT: module resources appear in the same list with a `module` key,
	// so no child-module walk is needed and the count cannot miss a nested module -- which the rendered
	// view's `child_modules` nesting made easy to get wrong.
	return state.Serial, len(state.Resources)
}

// mentionsStateLock recognises OpenTofu's lock message.
//
// Matching on the phrase is unavoidable here: OpenTofu returns exit 1 for a held lock exactly as it does
// for a provider error, and the distinction is only in the text. It is used ONLY to set an advisory flag
// that lets a caller retry — never to decide whether the apply succeeded, which comes from the exit code.
func mentionsStateLock(lines []string) bool {
	for _, line := range lines {
		lowered := strings.ToLower(line)
		if strings.Contains(lowered, "error acquiring the state lock") ||
			strings.Contains(lowered, "state blob is already locked") ||
			strings.Contains(lowered, "lock info") {
			return true
		}
	}
	return false
}

// durationForTofu formats a duration the way `-lock-timeout` accepts it.
//
// Go's `String()` emits `2m0s`, which OpenTofu rejects; it wants `2m` or `120s`. Seconds are used because
// one unit cannot be misparsed.
func durationForTofu(d time.Duration) string {
	return fmt.Sprintf("%ds", int64(d.Seconds()))
}

// StateFacts reads the serial and resource count without applying anything.
//
// Exported for the CONVERGED path: a module with no changes is not applied at all, and the report still
// has to say what the state holds -- otherwise "already converged" and "the state is unreadable" look the
// same in an audit row.
func (r *TofuRunner) StateFacts(ctx context.Context, workdir string) (serial int64, resources int) {
	binPath, err := r.resolvedBinary()
	if err != nil {
		return 0, 0
	}
	return r.stateFacts(ctx, binPath, workdir)
}
