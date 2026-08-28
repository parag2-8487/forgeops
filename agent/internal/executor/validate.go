// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/fileops"
	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

// The six `validate.*` operations, and why they are read-only.
//
// FR-27 is "the local agent validates artifacts before the user sees them". None of these writes
// anything, so none is `mutating` and none requires an approval: an approval gate on a read would
// mean a user has to approve finding out whether the thing they were offered is broken.
//
// Every path arrives as a workspace-relative string and is resolved through
// `fileops.ResolveForRead`, which is the same confinement `changeset.apply` uses. A validator is
// still a program that opens a file the sender named, so `../../etc/shadow` has to be refused here
// exactly as it would be on a write. `Root` is deliberately not an argument — it comes from the
// agent's own configuration, so a signed envelope cannot relocate what gets read.

// validateArgs is the argument object shared by all six validators.
type validateArgs struct {
	// Path is the file or directory to validate, relative to the workspace root.
	Path string `json:"path"`
	// Threshold is the severity at or above which `validate.trivy` fails. Defaults to HIGH.
	Threshold string `json:"threshold,omitempty"`
}

func decodeValidateArgs(v *envelope.Verified) (validateArgs, error) {
	var args validateArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return validateArgs{}, fmt.Errorf("executor: undecodable validate arguments: %w", err)
	}
	if strings.TrimSpace(args.Path) == "" {
		return validateArgs{}, errors.New("executor: a validation needs a path to validate")
	}
	return args, nil
}

// resolveTarget confines the requested path to the workspace and reports whether it is a directory.
func (d *dispatcher) resolveTarget(rel string) (abs string, isDir bool, err error) {
	abs, err = fileops.ResolveForRead(d.root, rel)
	if err != nil {
		return "", false, fmt.Errorf("executor: %w", err)
	}
	info, statErr := os.Stat(abs)
	if statErr != nil {
		return "", false, fmt.Errorf("executor: cannot validate %s: %w", rel, statErr)
	}
	return abs, info.IsDir(), nil
}

// outcomeResult turns a validator Outcome into the Result shape every operation reports through.
//
// A FAILED VALIDATION IS A SUCCESSFUL OPERATION. `Result.Status` is `invalid` rather than an error
// return, because "the artifact is broken" is the answer the caller asked for, and an error return
// is reserved for "the validation could not be performed". The generation pipeline depends on that
// distinction: it loops on `invalid` and gives up on an error.
func outcomeResult(outcome validator.Outcome) (Result, error) {
	// Truncated HERE, at the boundary, and not inside the runner: the validators parse the tool's
	// output, and a JSON document cut to fit a frame is unreadable rather than shorter.
	encoded, err := json.Marshal(outcome.Reportable())
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable validation outcome: %w", err)
	}
	status := "invalid"
	if outcome.Passed {
		status = "valid"
	}
	return Result{Status: status, Output: string(encoded)}, nil
}

// runValidation is the shared body: decode, confine, report progress, delegate, encode.
//
// A METHOD ON `*dispatcher`, AND THAT IS NOT COSMETIC. As a free function taking
// `(ctx, *dispatcher, *envelope.Verified, ProgressSink, ...)` it matched the `handler` shape closely
// enough that `TestNoHandlerIsReachableOutsideTheTable` counted it as a second dispatch surface and
// failed — six times, once per caller. The test was right to: a helper with a handler's signature
// reachable from six places is exactly the "second way in" §10.5 forbids. As a method with a
// different parameter list it cannot be mistaken for one, and it cannot be installed in the table by
// accident either.
func (d *dispatcher) runValidation(
	ctx context.Context,
	v *envelope.Verified,
	sink ProgressSink,
	stage string,
	invoke func(ctx context.Context, runner *validator.Runner, abs string, isDir bool, args validateArgs) (validator.Outcome, error),
) (Result, error) {
	args, err := decodeValidateArgs(v)
	if err != nil {
		return Result{}, err
	}
	abs, isDir, err := d.resolveTarget(args.Path)
	if err != nil {
		return Result{}, err
	}
	sink.Progress(10, stage, "resolving "+args.Path)

	// The runner's working directory is the artifact's own directory, so a tool that resolves a
	// relative include cannot climb out of the workspace by doing so.
	dir := abs
	if !isDir {
		dir = filepath.Dir(abs)
	}
	runner := &validator.Runner{Dir: dir}

	sink.Progress(35, stage, "running "+stage)
	outcome, err := invoke(ctx, runner, abs, isDir, args)
	if err != nil {
		// A tool that is absent or could not run is reported as such, never as a pass. FR-27 is
		// about what the agent verified, and an unverifiable artifact is not a verified one.
		if errors.Is(err, validator.ErrToolMissing) {
			return Result{}, fmt.Errorf("%w: %s cannot validate without its tool: %w",
				ErrUnimplemented, stage, err)
		}
		return Result{}, err
	}
	verdict := "valid"
	if !outcome.Passed {
		verdict = fmt.Sprintf("invalid (%d finding(s))", len(outcome.Findings))
	}
	sink.Progress(100, stage, fmt.Sprintf("%s: %s via %s %s", args.Path, verdict, outcome.Tool, outcome.ToolVersion))
	return outcomeResult(outcome)
}

func validateCompose(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	return d.runValidation(ctx, v, sink, "validate.compose",
		func(ctx context.Context, r *validator.Runner, abs string, isDir bool, _ validateArgs) (validator.Outcome, error) {
			if isDir {
				return validator.Outcome{}, errors.New("executor: validate.compose needs a compose file, not a directory")
			}
			return r.ValidateCompose(ctx, abs)
		})
}

func validateK8s(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	return d.runValidation(ctx, v, sink, "validate.k8s",
		func(ctx context.Context, r *validator.Runner, abs string, _ bool, _ validateArgs) (validator.Outcome, error) {
			return r.ValidateK8s(ctx, abs)
		})
}

func validateTofu(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	return d.runValidation(ctx, v, sink, "validate.tofu",
		func(ctx context.Context, r *validator.Runner, abs string, isDir bool, _ validateArgs) (validator.Outcome, error) {
			// OpenTofu works on a directory. A single `.tf` file is validated in its own directory,
			// which is what `tofu validate` would do anyway.
			module := abs
			if !isDir {
				module = filepath.Dir(abs)
			}
			return r.ValidateTofu(ctx, module)
		})
}

func validateHelm(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	return d.runValidation(ctx, v, sink, "validate.helm",
		func(ctx context.Context, r *validator.Runner, abs string, isDir bool, _ validateArgs) (validator.Outcome, error) {
			chart := abs
			if !isDir {
				// A `Chart.yaml` names its chart directory. Accepting either is kinder than making
				// the caller know which Helm wants, and both resolve to the same chart.
				chart = filepath.Dir(abs)
			}
			return r.ValidateHelm(ctx, chart)
		})
}

func validateYAML(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	return d.runValidation(ctx, v, sink, "validate.yaml",
		func(ctx context.Context, r *validator.Runner, abs string, isDir bool, args validateArgs) (validator.Outcome, error) {
			if isDir {
				return validator.Outcome{}, errors.New("executor: validate.yaml needs a file, not a directory")
			}
			// The schema is chosen by the workspace-relative path, not the absolute one: a file is
			// a workflow because of where it sits in the repository.
			return r.ValidateYAML(ctx, abs, validator.SchemaFor(args.Path))
		})
}

func validateTrivy(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	return d.runValidation(ctx, v, sink, "validate.trivy",
		func(ctx context.Context, r *validator.Runner, abs string, _ bool, args validateArgs) (validator.Outcome, error) {
			threshold := validator.SeverityHigh
			switch strings.ToUpper(strings.TrimSpace(args.Threshold)) {
			case "":
				// Default. HIGH rather than CRITICAL: a HIGH misconfiguration in generated
				// infrastructure is worth stopping before a user is shown it.
			case "CRITICAL":
				threshold = validator.SeverityCritical
			case "HIGH":
				threshold = validator.SeverityHigh
			case "MEDIUM":
				threshold = validator.SeverityMedium
			case "LOW":
				threshold = validator.SeverityLow
			default:
				return validator.Outcome{}, fmt.Errorf(
					"executor: unknown trivy threshold %q; use CRITICAL, HIGH, MEDIUM or LOW", args.Threshold)
			}
			return r.ValidateTrivy(ctx, abs, threshold)
		})
}
