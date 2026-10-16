// SPDX-License-Identifier: Apache-2.0

package executor

// `iac.apply` — Phase 2 §2.2's OpenTofu apply, as a governed operation.
//
// WHY THIS IS ONE OPERATION AND NOT `iac.plan` PLUS `iac.apply`. The authority being granted is "change
// this project's infrastructure", and a plan changes nothing. Splitting them would mean an approval for
// the plan and a second approval for the apply, with the state free to move in between — and the whole
// point of applying a SAVED PLAN is that the approved diff and the applied diff are the same computation.
// So one operation plans and then applies the plan it just made, inside one approval, and the plan's own
// summary travels back in the report so a reviewer can see what was applied.
//
// WHY THE APPROVAL IS NOT OPTIONAL. `tofu apply` creates and destroys real infrastructure and bills for
// it. There is no read-only mode of this operation; `validate.tofu` already exists for that.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/iac"
	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

// ErrIacWorkdirOutsideRoot refuses a workdir outside the workspace root.
var ErrIacWorkdirOutsideRoot = errors.New("executor: the module directory is outside the workspace root")

// iacApplyArgs is one apply against one module directory.
//
// NO `lock` FIELD and no `auto_approve` field. Both would be a caller's route to disabling the two
// protections that make this operation safe, and `iac.ApplyOptions` does not express them either.
type iacApplyArgs struct {
	// Relative to the workspace root, or absolute and beneath it. Empty means the root.
	Directory string `json:"directory"`
	// `-var` pairs for the PLAN. They are baked into the plan file and the apply gets no variables of
	// its own, so what is applied cannot differ from what was planned.
	Vars map[string]string `json:"vars"`
	// `-var-file` paths, resolved relative to the module directory by OpenTofu itself.
	VarFiles []string `json:"var_files"`
	// `-target` addresses. Present because a real recovery sometimes needs one; recorded in the report
	// so a targeted apply is visible as such in the audit row rather than looking like a full one.
	Target []string `json:"target"`
}

// IacApplyReport is what an apply reports.
type IacApplyReport struct {
	Directory string `json:"directory"`
	// PlanHadChanges is false when the module was already converged. An apply of a no-change plan is a
	// SUCCESS that changed nothing, and an operator needs those to be distinguishable from an apply
	// that created twelve resources.
	PlanHadChanges bool `json:"plan_had_changes"`
	// Applied is the exit status, not a phrase matched in the output.
	Applied bool `json:"applied"`
	// LockHeld says another operation held the state lock, which is the one failure here that is
	// resolved by waiting rather than by changing something.
	LockHeld bool `json:"lock_held"`
	// StateSerial and ResourceCount are read back from the state AFTER the apply. Two applies that both
	// report success and leave the same serial mean the second did nothing.
	StateSerial   int64 `json:"state_serial"`
	ResourceCount int   `json:"resource_count"`
	Targeted      bool  `json:"targeted"`
	// PlanSummary is the plan's own output, so the applied diff is reviewable in the audit row.
	PlanSummary string `json:"plan_summary"`
	Output      string `json:"output"`
	ObservedAt  string `json:"observed_at"`
}

// iacApply is the handler.
func iacApply(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args iacApplyArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: iac apply arguments: %v", ErrBadArgs, err)
	}

	workdir, err := resolveModuleDirectory(d.root, args.Directory)
	if err != nil {
		return Result{}, err
	}

	runner := iac.NewTofuRunner(iac.DefaultTofuConfig(), zap.NewNop(), telemetry.NoopTracer{})

	sink.Progress(15, string(OpIacApply), "planning "+filepath.Base(workdir))
	plan, err := runner.Plan(ctx, workdir, iac.PlanOptions{
		// LOCKED EVEN FOR THE PLAN here, because this plan is about to be applied: an unlocked plan can
		// be computed against a state another apply is midway through rewriting, and the plan file would
		// then be stale before it was ever used.
		Lock:     true,
		Vars:     args.Vars,
		VarFiles: args.VarFiles,
		Target:   args.Target,
	})
	if err != nil {
		return Result{}, fmt.Errorf("executor: tofu plan could not be run in %s: %w", workdir, err)
	}
	if plan.ExitCode != 0 && plan.ExitCode != 2 {
		// A plan that FAILED is a result: the module is invalid, and the diagnostics are the answer.
		report := IacApplyReport{
			Directory: relativeOrSelf(d.root, workdir), PlanHadChanges: false, Applied: false,
			PlanSummary: lastLines(plan.Stdout, iacSummaryLines),
			Output:      lastLines(plan.Stderr, iacSummaryLines),
			Targeted:    len(args.Target) > 0,
			ObservedAt:  d.now().UTC().Format(time.RFC3339),
		}
		return iacResult("failed", report)
	}

	if !plan.HasChanges {
		// CONVERGED, AND THE APPLY IS SKIPPED. Applying a no-change plan would succeed and advance
		// nothing, and reporting it as an apply that ran would make an audit row claim work that did not
		// happen. The state is still read back, so the report carries what the state says.
		serial, count := runner.StateFacts(ctx, workdir)
		report := IacApplyReport{
			Directory: relativeOrSelf(d.root, workdir), PlanHadChanges: false, Applied: true,
			StateSerial: serial, ResourceCount: count, Targeted: len(args.Target) > 0,
			PlanSummary: lastLines(plan.Stdout, iacSummaryLines),
			Output:      "the module was already converged, so no apply was run",
			ObservedAt:  d.now().UTC().Format(time.RFC3339),
		}
		sink.Progress(100, string(OpIacApply), "already converged")
		return iacResult("applied", report)
	}

	sink.Progress(55, string(OpIacApply), "applying the plan")
	applied, err := runner.Apply(ctx, workdir, iac.ApplyOptions{PlanFile: "tfplan"})
	if err != nil {
		return Result{}, fmt.Errorf("executor: tofu apply could not be run in %s: %w", workdir, err)
	}

	report := IacApplyReport{
		Directory: relativeOrSelf(d.root, workdir), PlanHadChanges: true,
		Applied: applied.Applied, LockHeld: applied.LockHeld,
		StateSerial: applied.StateSerial, ResourceCount: applied.ResourceCount,
		Targeted:    len(args.Target) > 0,
		PlanSummary: lastLines(plan.Stdout, iacSummaryLines),
		Output:      lastLines(append(applied.Stdout, applied.Stderr...), iacSummaryLines),
		ObservedAt:  d.now().UTC().Format(time.RFC3339),
	}
	status := "applied"
	if !applied.Applied {
		// A FAILED APPLY IS A RESULT, and it is the most important one to report faithfully: a partial
		// apply has created some resources and not others, and an error that discarded the output would
		// leave an operator with no idea which.
		status = "failed"
	}
	sink.Progress(100, string(OpIacApply), "apply "+status)
	return iacResult(status, report)
}

func iacResult(status string, report IacApplyReport) (Result, error) {
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable iac report: %w", err)
	}
	return Result{Status: status, Output: string(encoded)}, nil
}

// resolveModuleDirectory contains the module directory to the workspace root.
func resolveModuleDirectory(root, directory string) (string, error) {
	candidate := root
	if trimmed := strings.TrimSpace(directory); trimmed != "" {
		if filepath.IsAbs(trimmed) {
			candidate = trimmed
		} else {
			candidate = filepath.Join(root, trimmed)
		}
	}
	absRoot := normaliseDeepestExisting(root)
	candidate = normaliseDeepestExisting(candidate)
	relative, err := filepath.Rel(absRoot, candidate)
	if err != nil {
		return "", fmt.Errorf("%w: %q is not comparable to %q", ErrIacWorkdirOutsideRoot, candidate, absRoot)
	}
	if relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("%w: %q is outside %q", ErrIacWorkdirOutsideRoot, candidate, absRoot)
	}
	return candidate, nil
}

func relativeOrSelf(root, path string) string {
	if relative, err := filepath.Rel(root, path); err == nil {
		return filepath.ToSlash(relative)
	}
	return filepath.ToSlash(path)
}

// lastLines keeps the TAIL, because a tofu run's outcome is at the end.
func lastLines(lines []string, limit int) string {
	if len(lines) > limit {
		lines = lines[len(lines)-limit:]
	}
	return strings.Join(lines, "\n")
}

const iacSummaryLines = 120
