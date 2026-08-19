// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"context"
	"fmt"
	"sort"
	"testing"
	"time"

	"pgregory.net/rapid"
)

// Property Q-11 (design Appendix B; tasks.md leaf 11.11).
//
//	∀ raw watcher event sequences: the debounced/coalesced stream produces the same dirty set
//	as the un-coalesced stream.
//
// # WHAT THIS REPLACES
//
// The previous version of this file drew a list of paths, built a `map[string]bool` from them
// INSIDE the test, and asserted:
//
//	if len(seen) == 0 && len(paths) > 0 { rt.Fatalf("...dropped all entries") }
//
// `seen` is populated from `paths` two lines earlier, so that condition cannot hold for any
// input — it is unfalsifiable by construction. The file imported nothing from this package's
// watch pipeline and never constructed a `DebouncedWatcher`, so `WatchDebounced` could have
// returned an empty channel and the test would still have passed. That is why Q-11 had no
// negative control: `scripts/mutation-harness.py` reports a property that survives its own
// mutation as `VACUOUS`, and nothing can be mutated into view of a test that calls no
// production code.
//
// # WHY THE ASSERTION IS ON A SET
//
// Appendix B says "the same dirty SET", and that is the right shape rather than a convenience:
// `WatchDebounced` forwards each event on its own goroutine bounded by a semaphore, so output
// ORDER is deliberately not defined. Asserting on a sequence would make this test fail on a
// correct implementation for scheduling reasons, which is how a real property test gets deleted
// for being flaky.
//
// The dirty set is the set of paths a rescan would have to visit. Losing a path from it means a
// file changed and the index never learned, which is the harm the property exists to exclude.
func TestPropertyQ11_DebouncingPreservesTheDirtySet(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		count := rapid.IntRange(1, 12).Draw(rt, "eventCount")

		raw := make([]Event, 0, count)
		for i := 0; i < count; i++ {
			// A small path alphabet on purpose: repeats and delete-then-create pairs on the
			// SAME path are the interesting inputs, and a wide alphabet almost never draws them.
			path := fmt.Sprintf("file_%d.go", rapid.IntRange(0, 3).Draw(rt, "pathIndex"))
			kind := rapid.SampledFrom([]EventType{Create, Modify, Delete}).Draw(rt, "eventType")
			raw = append(raw, Event{Path: path, Type: kind})
		}

		// The un-coalesced dirty set, computed from the raw sequence.
		expected := dirtySetOf(raw)

		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		debounced := NewDebouncedWatcher(&replayWatcher{events: raw}, 1, 4)
		out, err := debounced.WatchDebounced(ctx, []string{"."})
		if err != nil {
			rt.Fatalf("WatchDebounced returned an error: %v", err)
		}

		observed := map[string]bool{}
		for ev := range out {
			observed[ev.Path] = true
		}

		// The positive control: the replay watcher must actually have delivered something, or
		// "the sets match" is two empty maps agreeing and the property proves nothing.
		if len(expected) == 0 {
			rt.Fatalf("the generated sequence produced an empty expected set, which cannot happen for count >= 1")
		}
		if len(observed) == 0 {
			rt.Fatalf(
				"Q-11 violation: the debounced stream delivered NOTHING for %d raw event(s); "+
					"every dirty path was lost", len(raw),
			)
		}

		if !sameSet(observed, expected) {
			rt.Fatalf(
				"Q-11 violation: debounced dirty set != raw dirty set.\n  raw events: %v\n  expected: %v\n  observed: %v",
				raw, sortedKeys(expected), sortedKeys(observed),
			)
		}
	})
}

// replayWatcher is a `Watcher` that delivers a fixed sequence and then closes, so the property
// quantifies over event sequences without depending on the filesystem or on fsnotify timing.
type replayWatcher struct {
	events []Event
}

func (r *replayWatcher) Watch(ctx context.Context, _ []string) (<-chan Event, error) {
	ch := make(chan Event, len(r.events))
	go func() {
		defer close(ch)
		for _, ev := range r.events {
			select {
			case ch <- ev:
			case <-ctx.Done():
				return
			}
		}
	}()
	return ch, nil
}

func (r *replayWatcher) Close() error { return nil }

func dirtySetOf(events []Event) map[string]bool {
	out := map[string]bool{}
	for _, ev := range events {
		out[ev.Path] = true
	}
	return out
}

func sameSet(a, b map[string]bool) bool {
	if len(a) != len(b) {
		return false
	}
	for key := range a {
		if !b[key] {
			return false
		}
	}
	return true
}

func sortedKeys(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for key := range set {
		out = append(out, key)
	}
	sort.Strings(out)
	return out
}
