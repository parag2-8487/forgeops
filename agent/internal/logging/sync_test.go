// SPDX-License-Identifier: Apache-2.0

package logging

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

// The spurious shutdown error, and the line it was hiding.
//
// Every agent invocation on Windows ended with "shutdown: logger: sync /dev/stderr: The handle is
// invalid." — on successful runs too. It printed ABOVE the real message, so it read as a crash and
// trained the reader to skim past the line that mattered.
//
// The fix must be narrow. Suppressing sync errors on Windows wholesale is the obvious one-line
// change and it would discard a full disk while writing a log.

// syncFunc lets a test drive Sync's error handling without needing a real unsyncable handle,
// which cannot be produced portably.
type stubCore struct {
	zapcore.Core
	syncErr error
}

func (s stubCore) Sync() error { return s.syncErr }

func loggerWithSyncError(err error) *zap.Logger {
	return zap.New(stubCore{Core: zapcore.NewNopCore(), syncErr: err})
}

func TestSync_IgnoresTheErrorsAStandardStreamProducesByDesign(t *testing.T) {
	t.Parallel()

	// Every errno here means "this descriptor does not support flushing", which is a fact about
	// the stream and not a failure to write anything.
	for _, errno := range unsyncableErrnos() {
		t.Run(errno.Error(), func(t *testing.T) {
			t.Parallel()
			// Wrapped the way zap wraps it, path included, so the test exercises the unwrapping
			// as well as the comparison.
			wrapped := fmt.Errorf("sync /dev/stderr: %w", errno)
			if err := Sync(loggerWithSyncError(wrapped)); err != nil {
				t.Errorf("Sync reported %v; a stream that cannot be flushed is not a failure", err)
			}
		})
	}
}

func TestSync_StillReportsARealFailure(t *testing.T) {
	t.Parallel()

	// THE HALF THAT MUST NOT BE LOST. Each of these is something an operator has to know about, and
	// none of them is a platform saying "this stream cannot be flushed".
	//
	// `EBADF` is deliberately NOT here, and the reason is in `posixUnsyncableErrnos`: macOS answers
	// EBADF for `/dev/stderr` when stderr is a pipe, which is how every CI job runs the agent, and
	// this package has no file sink for which EBADF could mean a lost log. That premise is pinned by
	// `TestTheLoggerOnlyEverWritesToAStandardStream`.
	for name, err := range map[string]error{
		"disk full":           syscall.ENOSPC,
		"io error":            syscall.EIO,
		"not an errno at all": errors.New("the sink was closed while writing"),
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			wrapped := fmt.Errorf("sync /var/log/forgeops.log: %w", err)
			if got := Sync(loggerWithSyncError(wrapped)); got == nil {
				t.Errorf("Sync swallowed %v; only an unflushable stream may be ignored", err)
			}
		})
	}
}

// TestTheLoggerOnlyEverWritesToAStandardStream pins the premise that lets EBADF be ignored.
//
// EBADF is treated as "unflushable" only because this package has NO FILE SINK: `New` takes zap's
// default `OutputPaths`, which is `stderr`, and `NewRedacted` calls `zap.Open("stderr")` explicitly.
// If a file sink is ever added, an EBADF could mean the log file was closed underneath the logger —
// a real defect — and it must come back out of `posixUnsyncableErrnos` at the same time.
//
// So this reads the package's own source, which is unusual and is the point: the assumption spans two
// files, and a test that only exercised behaviour could not notice the day they diverged.
func TestTheLoggerOnlyEverWritesToAStandardStream(t *testing.T) {
	t.Parallel()

	source, err := os.ReadFile("logging.go")
	if err != nil {
		t.Fatalf("reading logging.go: %v", err)
	}
	text := string(source)

	for _, forbidden := range []string{"OutputPaths =", "ErrorOutputPaths =", `zap.Open("/`, "lumberjack"} {
		if strings.Contains(text, forbidden) {
			t.Errorf("logging.go now contains %q, which suggests a file sink. If the logger can "+
				"write to a file then EBADF may mean a lost log, and it must be removed from "+
				"posixUnsyncableErrnos", forbidden)
		}
	}
	if !strings.Contains(text, `zap.Open("stderr")`) {
		t.Error("logging.go no longer opens stderr explicitly; re-check which sink Sync forgives " +
			"errors for")
	}
}

func TestSync_MatchesOnErrnoRatherThanOnTheMessage(t *testing.T) {
	t.Parallel()

	// A check that matched "The handle is invalid." would break on any non-English Windows and
	// would silently start reporting the spurious error again.
	lookalike := errors.New("sync /dev/stderr: The handle is invalid.")
	if err := Sync(loggerWithSyncError(lookalike)); err == nil {
		t.Error("an error carrying the Windows text but no errno was ignored, so the check is " +
			"matching on the message")
	}
}

func TestSync_ToleratesANilLogger(t *testing.T) {
	t.Parallel()

	// The shutdown sequence runs closers in reverse construction order, so it can reach the
	// logger closer on a partially built app.
	if err := Sync(nil); err != nil {
		t.Errorf("Sync(nil) = %v, want nil", err)
	}
}

// TestTheAgentPrintsNoSyncErrorOnExit is the end-to-end version: build the real binary and run it.
//
// The unit tests above prove the predicate. This proves the DEFECT is gone, which is a different
// claim: it exercises the real console handle, the real zap sink, and the real shutdown sequence,
// and it is the only test here that would have failed before the fix.
func TestTheAgentPrintsNoSyncErrorOnExit(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the agent binary")
	}

	dir := t.TempDir()
	binary := filepath.Join(dir, "forgeops-agent")
	if runtime.GOOS == "windows" {
		binary += ".exe"
	}

	build := exec.Command("go", "build", "-o", binary, "../../cmd/agent")
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("building the agent: %v\n%s", err, out)
	}

	// `version` is the shortest command that constructs the app and runs the full shutdown
	// sequence, which is where the closer lived.
	run := exec.Command(binary, "version")
	run.Env = append(os.Environ(), "AGENT_STATE_DIR="+dir)
	out, err := run.CombinedOutput()
	if err != nil {
		t.Fatalf("`agent version` failed: %v\n%s", err, out)
	}

	combined := string(out)
	for _, forbidden := range []string{
		"handle is invalid",
		"sync /dev/stderr",
		"shutdown: logger",
	} {
		if strings.Contains(strings.ToLower(combined), strings.ToLower(forbidden)) {
			t.Errorf("a successful `agent version` printed %q:\n%s", forbidden, combined)
		}
	}
	// And it must still have done its job, or the test would pass on a binary that printed
	// nothing at all.
	if !strings.Contains(combined, "forgeops-agent") {
		t.Errorf("`agent version` printed no version line:\n%s", combined)
	}
}
