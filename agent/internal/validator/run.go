// SPDX-License-Identifier: Apache-2.0

// Package validator runs the real external tools that decide whether a generated artifact is
// safe to show a user (FR-27).
//
// WHAT THIS REPLACED, BECAUSE IT MATTERS MORE THAN WHAT IT DOES
// ------------------------------------------------------------
// Every validator in this package used to be substring matching wearing a validator's name. The
// Kubernetes one checked `strings.Contains(content, "apiVersion:")` and then, if a cluster was
// reported available, `return nil` under a comment reading "Server-side dry run evaluation via
// client-go" — no client-go, no dry run, no cluster. The OpenTofu one took a `BinaryPath`, never
// executed it, and passed anything containing the word "resource". The Trivy one grepped for
// `privileged: true` and `0.0.0.0/0` and called everything else secure, with a `FailClosed` field
// it never read.
//
// That is the defect class this repository keeps finding, in its worst form: a security control
// that fabricates a pass. A generated manifest with a genuine schema error, a chart that cannot
// template, or an image with a critical CVE all came back clean, and Phase 1's completion criterion
// "Generated files pass validation pipeline" was ticked over it — while the dispatcher, separately,
// refused all six operations as `unimplemented`. So the criterion was green in two incompatible
// ways at once: the code that would have been asked said "not built", and the code that answered
// said "fine".
//
// Everything here now shells out to a pinned binary and reports what it actually said. A validator
// that cannot find its tool says so and fails; it does not pass by default. `Outcome.Tool` and
// `Outcome.ToolVersion` are on every result for that reason — "it passed" is only meaningful
// alongside what did the passing.
package validator

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// Severity orders findings so a caller can threshold on them without parsing prose.
type Severity string

const (
	SeverityCritical Severity = "CRITICAL"
	SeverityHigh     Severity = "HIGH"
	SeverityMedium   Severity = "MEDIUM"
	SeverityLow      Severity = "LOW"
	SeverityInfo     Severity = "INFO"
)

// : The order used when deciding whether a report's worst finding crosses a threshold.
var severityRank = map[Severity]int{
	SeverityInfo:     0,
	SeverityLow:      1,
	SeverityMedium:   2,
	SeverityHigh:     3,
	SeverityCritical: 4,
}

// Finding is one thing a tool objected to, located where the tool located it.
//
// `Path` and `Line` are whatever the tool reported and may be empty: a `helm lint` message is
// about a chart, not a line. Inventing a location would make a report look more precise than the
// tool that produced it, which is how a reader stops trusting the ones that are precise.
type Finding struct {
	Severity Severity `json:"severity"`
	Rule     string   `json:"rule,omitempty"`
	Message  string   `json:"message"`
	Path     string   `json:"path,omitempty"`
	Line     int      `json:"line,omitempty"`
}

// Outcome is one validator's verdict.
type Outcome struct {
	// Tool and ToolVersion name what produced this. A pass from an unknown version is not
	// evidence, so both are always populated on a completed run.
	Tool        string `json:"tool"`
	ToolVersion string `json:"tool_version"`
	// Mode says which of several possible checks ran, where a validator has more than one path.
	// `validate.k8s` sets it to `server-dry-run` or `client-dry-run`, because those are different
	// assurances and a caller must be able to tell which it got.
	Mode string `json:"mode,omitempty"`
	// Command is the argv actually executed, so a failure can be reproduced by hand.
	Command []string `json:"command"`
	// ExitCode is the tool's own exit status.
	ExitCode int       `json:"exit_code"`
	Passed   bool      `json:"passed"`
	Findings []Finding `json:"findings,omitempty"`
	// Output is the tool's combined stdout and stderr, truncated. The tool's own words are the
	// most useful thing in a failure report, so they travel rather than being summarised away.
	Output   string        `json:"output,omitempty"`
	Duration time.Duration `json:"duration_ns"`
}

// Worst returns the highest severity present, and false when there are no findings.
func (o Outcome) Worst() (Severity, bool) {
	worst, found := SeverityInfo, false
	for _, f := range o.Findings {
		if !found || severityRank[f.Severity] > severityRank[worst] {
			worst, found = f.Severity, true
		}
	}
	return worst, found
}

// Reportable returns a copy whose Output fits a `command.result` frame.
//
// Called at the point the outcome LEAVES the agent, never before a validator parses the text. The
// two limits exist because a JSON document cut in half is unreadable rather than merely shorter, and
// `validate.trivy` was failing on exactly that.
func (o Outcome) Reportable() Outcome {
	if len(o.Output) <= maxReportBytes {
		return o
	}
	trimmed := o
	trimmed.Output = o.Output[:maxReportBytes] +
		fmt.Sprintf("\n[%d bytes of tool output omitted from the report]", len(o.Output)-maxReportBytes)
	return trimmed
}

// ErrToolMissing is the refusal when a validator's binary is not on PATH.
//
// A NAMED FAILURE RATHER THAN A PASS. The version of this package being replaced would report
// success when it could not check anything, which is the worst available answer: the caller shows
// the artifact to a user on the strength of a check that did not happen. FR-27 is about what the
// agent verified, so an unverifiable artifact must not be reported as verified.
var ErrToolMissing = errors.New("validator: tool not found on PATH")

// : Two different limits, because conflating them corrupted a tool's output.
// :
// : `maxCaptureBytes` is how much is held so it can be PARSED. `maxReportBytes` is how much travels
// : in a `command.result`, which §7.3 sizes for a status.
// :
// : These were one number, and the consequence was a real bug: `validate.trivy` asks for JSON, a scan
// : of a modest directory is already 60 KB, and truncating at 64 KB cut the document mid-array — so
// : the validator reported "trivy output could not be read" for a scan that had completed perfectly.
// : A truncated JSON document is not a smaller answer, it is no answer.
const (
	maxCaptureBytes = 8 * 1024 * 1024
	maxReportBytes  = 64 * 1024
)

// Runner executes external tools. A struct so a test can point it at a script directory, and so
// the lookup is done once per process rather than per call.
type Runner struct {
	// Env is appended to the child's environment. Empty means inherit unchanged.
	Env []string
	// Dir is the working directory for every command. Callers set it to the confined artifact
	// directory, so a tool that resolves relative paths cannot reach outside it.
	Dir string

	// cache is a pointer so `In` can share it. A value here would mean copying a mutex, which vet
	// rejects and which would also be wrong: two copies would guard two caches while looking like
	// they guard one.
	cache *versionCache
}

// versionCache memoises `tool --version` output across the Runners that share it.
type versionCache struct {
	mu       sync.Mutex
	versions map[string]string
}

// In returns a Runner that executes in `dir`, sharing this Runner's version cache.
//
// A tool's version does not depend on the directory it runs in, so sharing the cache is the
// behaviour worth having as well as the one that avoids copying a lock.
func (r *Runner) In(dir string) *Runner {
	return &Runner{Env: r.Env, Dir: dir, cache: r.sharedCache()}
}

func (r *Runner) sharedCache() *versionCache {
	if r.cache == nil {
		r.cache = &versionCache{versions: map[string]string{}}
	}
	return r.cache
}
func (r *Runner) Look(tool string) (string, error) {
	path, err := exec.LookPath(tool)
	if err != nil {
		return "", fmt.Errorf("%w: %s", ErrToolMissing, tool)
	}
	return path, nil
}

// Version caches `tool <args...>` output so every Outcome can name the version without paying for
// a second process per validated file.
func (r *Runner) Version(ctx context.Context, tool string, args ...string) string {
	cache := r.sharedCache()
	cache.mu.Lock()
	if cached, ok := cache.versions[tool]; ok {
		cache.mu.Unlock()
		return cached
	}
	cache.mu.Unlock()

	// A version probe must not be able to hang the operation it is decorating.
	probeCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	out, _ := r.exec(probeCtx, tool, args...)
	version := firstLine(out.Output)
	if version == "" {
		version = "unknown"
	}
	cache.mu.Lock()
	cache.versions[tool] = version
	cache.mu.Unlock()
	return version
}

// Run executes a tool and returns its Outcome. A non-zero exit is not an error: it is the answer.
//
// An `error` from this method means the tool could not be run at all — missing binary, or the
// context ended. That distinction is the whole point: "the artifact is invalid" and "we could not
// tell" need opposite responses from the caller, and collapsing them is how a validator starts
// reporting a pass it did not establish.
func (r *Runner) Run(ctx context.Context, tool string, args ...string) (Outcome, error) {
	if _, err := r.Look(tool); err != nil {
		return Outcome{Tool: tool}, err
	}
	outcome, err := r.exec(ctx, tool, args...)
	if err != nil {
		return outcome, err
	}
	return outcome, nil
}

func (r *Runner) exec(ctx context.Context, tool string, args ...string) (Outcome, error) {
	started := time.Now()
	cmd := exec.CommandContext(ctx, tool, args...)
	cmd.Dir = r.Dir
	if len(r.Env) > 0 {
		cmd.Env = append(cmd.Environ(), r.Env...)
	}
	// The child gets its own process group and is signalled as a group, so a `tofu plan` that
	// spawned a provider plugin does not leave the plugin behind when the operation's timeout
	// fires. Set per-platform in run_unix.go / run_windows.go.
	configureProcessGroup(cmd)
	cmd.Cancel = func() error { return terminateGroup(cmd) }
	// A grace window between the signal and the kill, so a tool that cleans up after itself gets
	// the chance. Without this, cancelling a `helm template` can leave a temp chart directory.
	cmd.WaitDelay = 5 * time.Second

	var combined bytes.Buffer
	limited := &limitedWriter{w: &combined, remaining: maxCaptureBytes}
	cmd.Stdout = limited
	cmd.Stderr = limited

	runErr := cmd.Run()
	outcome := Outcome{
		Tool:     tool,
		Command:  append([]string{tool}, args...),
		Output:   strings.TrimRight(combined.String(), "\r\n"),
		Duration: time.Since(started),
	}
	// The note is appended only when the CAPTURE ceiling was hit, and never merely because the output
	// is larger than a report frame. Appending it to text a caller is about to parse as JSON is what
	// broke `validate.trivy`; callers that report rather than parse use `Reportable()`.
	if limited.truncated {
		outcome.Output += fmt.Sprintf("\n[output truncated at %d bytes]", maxCaptureBytes)
	}

	var exitErr *exec.ExitError
	switch {
	case runErr == nil:
		outcome.ExitCode = 0
		outcome.Passed = true
	case errors.As(runErr, &exitErr):
		outcome.ExitCode = exitErr.ExitCode()
		outcome.Passed = false
	default:
		// Could not run, or the context ended. Not a verdict.
		return outcome, fmt.Errorf("validator: %s could not be run: %w", tool, runErr)
	}
	if ctx.Err() != nil {
		return outcome, fmt.Errorf("validator: %s did not finish: %w", tool, ctx.Err())
	}
	return outcome, nil
}

// limitedWriter caps how much tool output is retained, and records that it did.
type limitedWriter struct {
	w         *bytes.Buffer
	remaining int
	truncated bool
}

func (l *limitedWriter) Write(p []byte) (int, error) {
	if l.remaining <= 0 {
		l.truncated = true
		return len(p), nil
	}
	if len(p) > l.remaining {
		l.w.Write(p[:l.remaining]) //nolint:errcheck // bytes.Buffer writes cannot fail
		l.remaining = 0
		l.truncated = true
		return len(p), nil
	}
	l.w.Write(p) //nolint:errcheck // bytes.Buffer writes cannot fail
	l.remaining -= len(p)
	return len(p), nil
}

func firstLine(s string) string {
	if idx := strings.IndexAny(s, "\r\n"); idx >= 0 {
		return strings.TrimSpace(s[:idx])
	}
	return strings.TrimSpace(s)
}
