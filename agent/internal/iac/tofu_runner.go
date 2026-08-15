// SPDX-License-Identifier: Apache-2.0
package iac

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

// TofuRunner implements Runner using the OpenTofu CLI binary.
type TofuRunner struct {
	cfg    TofuConfig
	logger *zap.Logger
	tracer telemetry.Tracer
	sink   LineSink
}

// NewTofuRunner constructs a TofuRunner.
func NewTofuRunner(cfg TofuConfig, logger *zap.Logger, tracer telemetry.Tracer) *TofuRunner {
	// Apply defaults for zero-value fields.
	if cfg.BinaryPath == "" {
		cfg.BinaryPath = "tofu"
	}
	if cfg.DefaultTimeout == 0 {
		cfg.DefaultTimeout = 5 * time.Minute
	}
	if cfg.KillGrace == 0 {
		cfg.KillGrace = 10 * time.Second
	}
	if cfg.MaxLineBytes == 0 {
		cfg.MaxLineBytes = 64 * 1024
	}

	r := &TofuRunner{
		cfg:    cfg,
		logger: logger,
		tracer: tracer,
	}
	// Default sink wires to logger.
	r.sink = func(stream string, line string) {
		logger.Debug("tofu output", zap.String("stream", stream), zap.String("line", line))
	}
	return r
}

// SetSink overrides the line sink (useful for testing or streaming to the platform).
func (r *TofuRunner) SetSink(sink LineSink) {
	r.sink = sink
}

// Validate runs `tofu validate -json` in the given workdir.
func (r *TofuRunner) Validate(ctx context.Context, workdir string) (*ValidateResult, error) {
	ctx, span := r.tracer.StartSpan(ctx, "iac.Validate")
	defer span.End()

	start := time.Now()

	binPath, err := r.resolvedBinary()
	if err != nil {
		return nil, err
	}

	ctx, cancel := r.ensureDeadline(ctx)
	defer cancel()

	args := []string{"validate", "-json"}
	stdout, stderr, exitCode, err := r.run(ctx, binPath, args, workdir)
	if err != nil {
		return nil, err
	}

	result := &ValidateResult{
		ExitCode: exitCode,
		Stdout:   stdout,
		Stderr:   stderr,
		Duration: time.Since(start),
	}

	// Parse JSON diagnostics from stdout.
	if len(stdout) > 0 {
		combined := strings.Join(stdout, "\n")
		if json.Valid([]byte(combined)) {
			result.Diagnostics = json.RawMessage(combined)
		}
	}

	return result, nil
}

// Plan runs `tofu plan -detailed-exitcode -out=tfplan` followed by `tofu show -json tfplan`.
func (r *TofuRunner) Plan(ctx context.Context, workdir string, opts PlanOptions) (*PlanResult, error) {
	ctx, span := r.tracer.StartSpan(ctx, "iac.Plan")
	defer span.End()

	start := time.Now()

	binPath, err := r.resolvedBinary()
	if err != nil {
		return nil, err
	}

	ctx, cancel := r.ensureDeadline(ctx)
	defer cancel()

	// Build plan arguments.
	planFile := filepath.Join(workdir, "tfplan")
	args := []string{"plan", "-detailed-exitcode", fmt.Sprintf("-out=%s", planFile)}

	if !opts.Lock {
		args = append(args, "-lock=false")
	}
	for _, vf := range opts.VarFiles {
		args = append(args, fmt.Sprintf("-var-file=%s", vf))
	}
	for k, v := range opts.Vars {
		args = append(args, fmt.Sprintf("-var=%s=%s", k, v))
	}
	for _, t := range opts.Target {
		args = append(args, fmt.Sprintf("-target=%s", t))
	}

	stdout, stderr, exitCode, err := r.run(ctx, binPath, args, workdir)
	if err != nil {
		return nil, err
	}

	result := &PlanResult{
		ExitCode:   exitCode,
		HasChanges: exitCode == 2, // -detailed-exitcode: 2 means changes present
		Stdout:     stdout,
		Stderr:     stderr,
		Duration:   time.Since(start),
	}

	// If plan succeeded (exit 0 or 2), run show -json to get structured output.
	if exitCode == 0 || exitCode == 2 {
		showArgs := []string{"show", "-json", planFile}
		showStdout, _, showExit, showErr := r.run(ctx, binPath, showArgs, workdir)
		if showErr == nil && showExit == 0 && len(showStdout) > 0 {
			combined := strings.Join(showStdout, "\n")
			if json.Valid([]byte(combined)) {
				result.PlanJSON = json.RawMessage(combined)
			}
		}
	}

	return result, nil
}

// resolvedBinary checks whether the configured binary exists on the system.
func (r *TofuRunner) resolvedBinary() (string, error) {
	path, err := exec.LookPath(r.cfg.BinaryPath)
	if err != nil {
		return "", fmt.Errorf("%w: %s", ErrTofuNotFound, r.cfg.BinaryPath)
	}
	return path, nil
}

// ensureDeadline applies DefaultTimeout if ctx does not already have a deadline.
func (r *TofuRunner) ensureDeadline(ctx context.Context) (context.Context, context.CancelFunc) {
	if _, ok := ctx.Deadline(); ok {
		return context.WithCancel(ctx)
	}
	return context.WithTimeout(ctx, r.cfg.DefaultTimeout)
}

// run executes a command, streams stdout/stderr through the sink, and returns
// captured lines and the exit code. Both stream goroutines complete before
// the result is returned.
func (r *TofuRunner) run(ctx context.Context, binPath string, args []string, workdir string) (stdout []string, stderr []string, exitCode int, err error) {
	cmd := exec.CommandContext(ctx, binPath, args...)
	cmd.Dir = workdir
	cmd.Env = buildEnv(r.cfg, workdir)

	// Set platform-specific process group attributes.
	setProcessGroup(cmd)

	// The runner owns its pipes rather than using cmd.StdoutPipe/StderrPipe.
	//
	// os/exec closes the pipes it hands out inside cmd.Wait, and the
	// StdoutPipe contract states it is "incorrect to call Wait before all
	// reads from the pipe have completed". Waiting first raced the scanners
	// and intermittently truncated stdout, surfacing as a nil
	// ValidateResult.Diagnostics (CI run 30468655307, runner_test.go:145
	// "expected diagnostics JSON"). Draining first instead risks hanging
	// forever, because a killed process tree can leave a grandchild holding
	// the write end open so the scanners never see EOF.
	//
	// Owning the pipes removes the dilemma: cmd.Wait never touches them, so
	// the process can be reaped first and the drain can then be bounded and
	// forced to completion by closing the read ends ourselves.
	stdoutR, stdoutW, err := os.Pipe()
	if err != nil {
		return nil, nil, -1, fmt.Errorf("stdout pipe: %w", err)
	}
	defer func() { _ = stdoutR.Close() }()

	stderrR, stderrW, err := os.Pipe()
	if err != nil {
		_ = stdoutW.Close()
		return nil, nil, -1, fmt.Errorf("stderr pipe: %w", err)
	}
	defer func() { _ = stderrR.Close() }()

	cmd.Stdout = stdoutW
	cmd.Stderr = stderrW

	if err := cmd.Start(); err != nil {
		_ = stdoutW.Close()
		_ = stderrW.Close()
		return nil, nil, -1, fmt.Errorf("start command: %w", err)
	}

	// Immediately after Start, and nowhere later. On Windows this assigns the process
	// to the Job Object created by setProcessGroup, and every descendant it creates
	// from this point is contained automatically (D-37). A descendant created in the
	// window between Start and here would escape, so the two calls stay adjacent. No-op
	// on Unix, where Setpgid already took effect at exec.
	attachProcessGroup(cmd)

	// The child holds its own descriptors now. Dropping the parent's copies is
	// what lets the read ends reach EOF when the child (and any descendant
	// that inherited them) exits.
	_ = stdoutW.Close()
	_ = stderrW.Close()

	// Terminate the whole process group as soon as the context is done, WHILE the
	// child is still live. Signalling after cmd.Wait targets a reaped pgid, and
	// the grace-then-SIGKILL escalation cannot observe a process it no longer
	// owns. `reaped` stops the watchdog on the normal path.
	reaped := make(chan struct{})
	var termOnce sync.Once
	terminate := func() { termOnce.Do(func() { terminateGroup(cmd, r.cfg.KillGrace) }) }
	go func() {
		select {
		case <-ctx.Done():
			terminate()
		case <-reaped:
		}
	}()

	var mu sync.Mutex
	var wg sync.WaitGroup
	wg.Add(2)

	// Stream stdout.
	go func() {
		defer wg.Done()
		lines := r.scanPipe(stdoutR, "stdout")
		mu.Lock()
		stdout = lines
		mu.Unlock()
	}()

	// Stream stderr.
	go func() {
		defer wg.Done()
		lines := r.scanPipe(stderrR, "stderr")
		mu.Lock()
		stderr = lines
		mu.Unlock()
	}()

	drained := make(chan struct{})
	go func() {
		wg.Wait()
		close(drained)
	}()

	// Reap the process. This is safe with the scanners still running: the
	// child cannot block on a full pipe because they are draining it.
	waitErr := cmd.Wait()
	close(reaped)

	// The scanners normally finish the instant the child's descriptors close.
	// If a surviving descendant still holds a write end, close the read ends
	// to unblock them rather than waiting forever.
	select {
	case <-drained:
	case <-time.After(r.drainGrace()):
		_ = stdoutR.Close()
		_ = stderrR.Close()
		<-drained
	}

	// Both scanner goroutines have returned, so stdout and stderr are complete
	// and safe to read without the mutex.

	if waitErr != nil {
		// Check context cancellation first — on Windows, CommandContext kills
		// the process which produces an ExitError, but the root cause is the
		// context being done.
		if ctx.Err() != nil {
			// Idempotent: the watchdog above has normally already run. This covers
			// the race where the child exited just as the deadline expired but a
			// descendant still holds the group.
			terminate()
			return stdout, stderr, -1, ctx.Err()
		}

		var exitErr *exec.ExitError
		if errors.As(waitErr, &exitErr) {
			exitCode = exitErr.ExitCode()
		} else {
			return stdout, stderr, -1, waitErr
		}
	}

	return stdout, stderr, exitCode, nil
}

// drainGrace bounds how long run waits for the output scanners after the child
// process has been reaped. A surviving descendant can hold a pipe write end
// open, in which case the scanners never see EOF and must be forced out.
func (r *TofuRunner) drainGrace() time.Duration {
	if r.cfg.KillGrace > 0 {
		return r.cfg.KillGrace
	}
	return 10 * time.Second
}

// scanPipe reads lines from a pipe, capping each line at MaxLineBytes.
func (r *TofuRunner) scanPipe(pipe io.Reader, stream string) []string {
	maxLine := r.cfg.MaxLineBytes
	if maxLine <= 0 {
		maxLine = 1024 * 1024
	}

	reader := bufio.NewReaderSize(pipe, 64*1024)
	var lines []string
	var current strings.Builder

	for {
		chunk, isPrefix, err := reader.ReadLine()
		if len(chunk) > 0 {
			if current.Len() < maxLine {
				remaining := maxLine - current.Len()
				if len(chunk) > remaining {
					current.Write(chunk[:remaining])
				} else {
					current.Write(chunk)
				}
			}
		}
		if err != nil {
			if current.Len() > 0 {
				line := current.String()
				lines = append(lines, line)
				if r.sink != nil {
					r.sink(stream, line)
				}
			}
			break
		}
		if !isPrefix {
			line := current.String()
			current.Reset()
			lines = append(lines, line)
			if r.sink != nil {
				r.sink(stream, line)
			}
		}
	}
	return lines
}
