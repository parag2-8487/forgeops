// SPDX-License-Identifier: Apache-2.0
package app

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"pgregory.net/rapid"

	"github.com/parag8487/ForgeOps/agent/internal/config"
)

// TestProperty_P07_ShutdownOrdering is a property-based test verifying that
// the shutdown close-loop logic satisfies:
//   - Exact reverse construction order
//   - Exactly-once close per component
//   - Continues closing after errors
//   - Idempotence (second call returns same result)
//   - Bounded time
func TestProperty_P07_ShutdownOrdering(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		n := rapid.IntRange(1, 10).Draw(rt, "numClosers")

		var order []int
		var mu sync.Mutex
		closers := make([]namedCloser, n)
		failFlags := make([]bool, n)

		for i := 0; i < n; i++ {
			idx := i
			shouldFail := rapid.Bool().Draw(rt, fmt.Sprintf("fail_%d", i))
			failFlags[i] = shouldFail
			closers[i] = namedCloser{
				name: fmt.Sprintf("c%d", i),
				fn: func() error {
					mu.Lock()
					order = append(order, idx)
					mu.Unlock()
					if shouldFail {
						return fmt.Errorf("close %d failed", idx)
					}
					return nil
				},
			}
		}

		// --- Execute the close loop (mirrors App.Close logic) ---
		executeClose := func() error {
			var closeErr error
			for i := len(closers) - 1; i >= 0; i-- {
				c := closers[i]
				if err := c.fn(); err != nil {
					closeErr = errors.Join(closeErr, fmt.Errorf("%s: %w", c.name, err))
				}
			}
			return closeErr
		}

		// First close — ordering and exactly-once clauses below.
		//
		// The bounded-time clause used to live here as `elapsed > 5*time.Second`
		// over these instantaneous closers, which is unfalsifiable: n closers that
		// return immediately finish in microseconds whatever ShutdownTimeout says,
		// and this loop is a COPY of App.Close that has no timeout at all. Design
		// §0.5 records it, and task 2.7 moves the clause to
		// property_shutdown_deadline_test.go, where it drives the real App.Close
		// against a deliberately slow closer.
		firstErr := executeClose()

		// Verify exact reverse order
		if len(order) != n {
			rt.Fatalf("expected %d closes, got %d", n, len(order))
		}
		for i := 0; i < len(order)-1; i++ {
			if order[i] <= order[i+1] {
				rt.Fatalf("not strictly decreasing (not reverse order): %v", order)
			}
		}

		// Verify exactly-once: each index appears exactly once
		seen := make(map[int]int)
		for _, idx := range order {
			seen[idx]++
		}
		for i := 0; i < n; i++ {
			if seen[i] != 1 {
				rt.Fatalf("component %d closed %d times, want 1", i, seen[i])
			}
		}

		// Verify errors don't stop other closers — all n were called
		// (already proven by len(order) == n above)

		// --- Idempotence via sync.Once ---
		// Simulate App.Close() with sync.Once wrapping
		var secondOrder []int
		closers2 := make([]namedCloser, n)
		for i := 0; i < n; i++ {
			idx := i
			shouldFail := failFlags[i]
			closers2[i] = namedCloser{
				name: fmt.Sprintf("c%d", i),
				fn: func() error {
					mu.Lock()
					secondOrder = append(secondOrder, idx)
					mu.Unlock()
					if shouldFail {
						return fmt.Errorf("close %d failed", idx)
					}
					return nil
				},
			}
		}

		var once sync.Once
		var onceErr error
		doClose := func() error {
			once.Do(func() {
				for i := len(closers2) - 1; i >= 0; i-- {
					c := closers2[i]
					if err := c.fn(); err != nil {
						onceErr = errors.Join(onceErr, fmt.Errorf("%s: %w", c.name, err))
					}
				}
			})
			return onceErr
		}

		err1 := doClose()
		err2 := doClose()

		// Second call returns same result
		if fmt.Sprint(err1) != fmt.Sprint(err2) {
			rt.Fatalf("idempotence violated: first=%v second=%v", err1, err2)
		}

		// Second call did not invoke closers again
		if len(secondOrder) != n {
			rt.Fatalf("idempotence: expected %d total calls, got %d", n, len(secondOrder))
		}

		// Verify error is nil only when no failures
		expectedFailCount := 0
		for _, f := range failFlags {
			if f {
				expectedFailCount++
			}
		}
		if expectedFailCount == 0 && firstErr != nil {
			rt.Fatalf("no failures configured but got error: %v", firstErr)
		}
		if expectedFailCount > 0 && firstErr == nil {
			rt.Fatalf("failures configured but got nil error")
		}
	})
}

// ── Tests against the REAL App.Close ────────────────────────────────────────
//
// TestProperty_P07_ShutdownOrdering above re-implements the close loop, so it
// cannot observe a defect in App.Close itself — that is how the discarded
// timeout context survived review. The tests below call the real method.

// newTestApp builds the minimum App needed to exercise Close.
func newTestApp(timeout time.Duration, closers ...namedCloser) *App {
	return &App{
		cfg:     &config.Config{ShutdownTimeout: timeout},
		closers: closers,
	}
}

func TestAppClose_ClosesInReverseOrderExactlyOnce(t *testing.T) {
	var mu sync.Mutex
	var order []string
	record := func(name string) namedCloser {
		return namedCloser{name: name, fn: func() error {
			mu.Lock()
			order = append(order, name)
			mu.Unlock()
			return nil
		}}
	}

	a := newTestApp(5*time.Second, record("first"), record("second"), record("third"))
	if err := a.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	want := []string{"third", "second", "first"}
	if len(order) != len(want) {
		t.Fatalf("closed %v, want %v", order, want)
	}
	for i := range want {
		if order[i] != want[i] {
			t.Fatalf("closed %v, want %v", order, want)
		}
	}

	// Idempotent: a second Close must not re-invoke anything.
	if err := a.Close(); err != nil {
		t.Fatalf("second Close: %v", err)
	}
	if len(order) != len(want) {
		t.Fatalf("second Close re-invoked closers: %v", order)
	}
}

func TestAppClose_ContinuesPastAFailingCloser(t *testing.T) {
	var mu sync.Mutex
	var order []string
	a := newTestApp(5*time.Second,
		namedCloser{name: "bottom", fn: func() error {
			mu.Lock()
			order = append(order, "bottom")
			mu.Unlock()
			return nil
		}},
		namedCloser{name: "boom", fn: func() error { return errors.New("boom") }},
	)

	err := a.Close()
	if err == nil {
		t.Fatal("expected the close error to be reported")
	}
	if len(order) != 1 || order[0] != "bottom" {
		t.Fatalf("a failing closer stopped the rest: %v", order)
	}
}

// TestAppClose_EnforcesShutdownDeadline is the regression guard for P-07's
// "total shutdown <= configured timeout" clause. Reverting App.Close to
// discarding its context (`_ = ctx`) makes this test hang until the Go test
// timeout instead of returning, so the clause can no longer be silently lost.
func TestAppClose_EnforcesShutdownDeadline(t *testing.T) {
	const shutdownTimeout = 150 * time.Millisecond

	release := make(chan struct{})
	defer close(release) // let the abandoned closer exit when the test ends

	a := newTestApp(shutdownTimeout, namedCloser{
		name: "wedged",
		fn: func() error {
			<-release
			return nil
		},
	})

	start := time.Now()
	err := a.Close()
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("expected a deadline error when a closer never returns")
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected context.DeadlineExceeded, got %v", err)
	}
	// Generous upper bound: the point is that Close returns at all, bounded by
	// the configured timeout rather than by the blocked closer.
	if elapsed > 10*shutdownTimeout {
		t.Fatalf("Close took %v, want roughly %v", elapsed, shutdownTimeout)
	}
}
