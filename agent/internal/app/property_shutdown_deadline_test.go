// SPDX-License-Identifier: Apache-2.0

package app

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"pgregory.net/rapid"
)

// P-07's bounded-time clause, made falsifiable (design.md §0.5, §10.4).
//
// What was wrong
// --------------
// The clause lived inside TestProperty_P07_ShutdownOrdering as
//
//	elapsed > 5*time.Second
//
// over closers that return immediately, inside a loop that RE-IMPLEMENTS App.Close
// and has no timeout in it at all. n instantaneous closers finish in microseconds
// whatever ShutdownTimeout is set to, so the assertion could not fail for any
// configuration — and because the loop was a copy, deleting App.Close's context
// entirely would not have disturbed it. Phase 1 gives the agent long-running
// subsystems (session manager, watcher, executor), so the bound has to be real.
//
// What this asserts instead
// -------------------------
// Over generated (timeout, closer-delay) pairs, driving the REAL App.Close:
//
//   - when a closer outlasts the timeout, Close still returns, it returns a
//     context.DeadlineExceeded error, and it returns within a small multiple of the
//     configured timeout rather than waiting for the closer;
//   - when every closer finishes inside the timeout, Close returns no deadline error
//     and the closers all ran.
//
// The negative control is `_ = ctx` in App.Close: it makes the first case hang until
// the Go test timeout instead of returning.
//
// Timing margins are deliberately generous. A property that measures wall-clock time
// on a shared CI runner has to distinguish "the bound is not enforced" from "the
// scheduler was busy", and a flaky property gets deleted, which would lose the clause
// a second time.
const (
	// Small enough that the property is quick, large enough that scheduler noise
	// cannot be mistaken for a missing bound.
	minShutdownTimeout = 20 * time.Millisecond
	maxShutdownTimeout = 80 * time.Millisecond

	// How far past the configured timeout Close may take before the bound is
	// considered unenforced. 20x sounds loose; the failure this catches is
	// unbounded (Close waits for the closer, which sleeps for 50x the timeout or
	// never returns), so a tighter margin would only add flakiness.
	deadlineSlackFactor = 20
)

func TestProperty_P07_ShutdownDeadlineIsEnforced(t *testing.T) {
	t.Parallel()

	rapid.Check(t, func(rt *rapid.T) {
		timeout := time.Duration(rapid.Int64Range(
			int64(minShutdownTimeout), int64(maxShutdownTimeout),
		).Draw(rt, "shutdownTimeout"))

		// A closer that deliberately outlasts the timeout by a wide margin, so the
		// only way Close can return promptly is by honouring its own deadline.
		overrunFactor := rapid.IntRange(5, 50).Draw(rt, "overrunFactor")
		slowFor := timeout * time.Duration(overrunFactor)

		release := make(chan struct{})
		var releaseOnce sync.Once
		// Let the abandoned closer exit rather than leaking a goroutine for the rest
		// of the run; App.Close abandons it by design.
		defer releaseOnce.Do(func() { close(release) })

		var fastRan, slowStarted bool
		var mu sync.Mutex

		app := newTestApp(timeout,
			namedCloser{name: "fast", fn: func() error {
				mu.Lock()
				fastRan = true
				mu.Unlock()
				return nil
			}},
			namedCloser{name: "slow", fn: func() error {
				mu.Lock()
				slowStarted = true
				mu.Unlock()
				select {
				case <-release:
				case <-time.After(slowFor):
				}
				return nil
			}},
		)

		start := time.Now()
		err := app.Close()
		elapsed := time.Since(start)

		if err == nil {
			rt.Fatalf("Close returned nil with a closer that outlasts the %v timeout", timeout)
		}
		if !errors.Is(err, context.DeadlineExceeded) {
			rt.Fatalf("want context.DeadlineExceeded, got %v", err)
		}
		if elapsed > timeout*deadlineSlackFactor {
			rt.Fatalf(
				"Close took %v with ShutdownTimeout=%v; the deadline is not bounding shutdown",
				elapsed, timeout,
			)
		}
		// Reverse order means the slow closer (registered last) runs first, so the
		// fast one never gets its turn. Asserting this keeps the clause honest: if
		// Close silently skipped closers on timeout, `slowStarted` would be false.
		mu.Lock()
		startedSlow := slowStarted
		ranFast := fastRan
		mu.Unlock()
		if !startedSlow {
			rt.Fatalf("the slow closer never started, so the timeout was not what ended Close")
		}
		if ranFast {
			rt.Fatalf("the fast closer ran despite the slow one blocking ahead of it in reverse order")
		}

		releaseOnce.Do(func() { close(release) })
	})
}

func TestProperty_P07_CloseSucceedsWhenClosersFinishInTime(t *testing.T) {
	t.Parallel()

	// The other half of the bound: it must not fire when it should not. Without
	// this, an App.Close that always reported DeadlineExceeded would satisfy the
	// test above.
	rapid.Check(t, func(rt *rapid.T) {
		timeout := time.Duration(rapid.Int64Range(
			int64(200*time.Millisecond), int64(500*time.Millisecond),
		).Draw(rt, "shutdownTimeout"))

		n := rapid.IntRange(1, 5).Draw(rt, "numClosers")
		// Each closer sleeps a little, so the total is genuinely non-zero but
		// comfortably inside the timeout.
		per := rapid.Int64Range(int64(time.Millisecond), int64(5*time.Millisecond)).Draw(rt, "perCloser")

		var mu sync.Mutex
		var ran int
		closers := make([]namedCloser, n)
		for i := range closers {
			closers[i] = namedCloser{name: "c", fn: func() error {
				time.Sleep(time.Duration(per))
				mu.Lock()
				ran++
				mu.Unlock()
				return nil
			}}
		}

		app := newTestApp(timeout, closers...)
		if err := app.Close(); err != nil {
			rt.Fatalf("Close reported %v although every closer finished inside %v", err, timeout)
		}

		mu.Lock()
		total := ran
		mu.Unlock()
		if total != n {
			rt.Fatalf("expected %d closers to run, got %d", n, total)
		}
	})
}
