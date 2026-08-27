// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// scriptedWatcher is a Watcher whose events the test supplies, so the coalescing window can be
// exercised without depending on how quickly the operating system reports a file change.
//
// The existing watcher tests drive a real FSNotifyWatcher against a temp directory, which is the right
// shape for asserting that fsnotify is wired correctly. It is the wrong shape for asserting WHEN a
// batch is emitted: the assertion "a burst becomes one batch" needs the burst to be a burst, and on a
// loaded machine two writes 1 ms apart can be reported 300 ms apart. That would make the test measure
// the filesystem rather than the debouncer.
type scriptedWatcher struct {
	events chan Event
}

func newScriptedWatcher() *scriptedWatcher {
	return &scriptedWatcher{events: make(chan Event, 64)}
}

func (s *scriptedWatcher) Watch(_ context.Context, _ []string) (<-chan Event, error) {
	return s.events, nil
}

func (s *scriptedWatcher) Close() error { return nil }

// send delivers one event as the underlying watcher would.
func (s *scriptedWatcher) send(path string, kind EventType) {
	s.events <- Event{Path: path, Type: kind}
}

func TestWatchCoalesced_ABurstOnOnePathIsOneBatchWithOneEntry(t *testing.T) {
	// The behaviour that makes this worth having: one save in an editor is several filesystem events
	// -- a temp file, a rename over the target, an mtime touch -- and re-indexing per event would
	// submit three reports for one change.
	src := newScriptedWatcher()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	batches, err := NewDebouncedWatcher(src, 80, 1).WatchCoalesced(ctx, []string{"ignored"})
	if err != nil {
		t.Fatalf("watch coalesced: %v", err)
	}

	for i := 0; i < 5; i++ {
		src.send("/w/app.ts", Modify)
	}

	select {
	case batch := <-batches:
		if len(batch) != 1 {
			t.Fatalf("want one coalesced entry, got %d: %+v", len(batch), batch)
		}
		if batch[0].Path != "/w/app.ts" {
			t.Errorf("unexpected path: %s", batch[0].Path)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no batch arrived")
	}
}

func TestWatchCoalesced_ManyPathsInOneBurstArriveTogetherInFirstTouchOrder(t *testing.T) {
	src := newScriptedWatcher()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	batches, err := NewDebouncedWatcher(src, 80, 1).WatchCoalesced(ctx, []string{"ignored"})
	if err != nil {
		t.Fatalf("watch coalesced: %v", err)
	}

	// `b` is touched twice, and must still appear once, in the position of its FIRST sighting -- a map
	// alone would make the order random, which turns the diagnostic log line into a puzzle.
	src.send("/w/a.ts", Modify)
	src.send("/w/b.ts", Modify)
	src.send("/w/a.ts", Modify)
	src.send("/w/c.ts", Create)
	src.send("/w/b.ts", Modify)

	select {
	case batch := <-batches:
		got := make([]string, 0, len(batch))
		for _, ev := range batch {
			got = append(got, ev.Path)
		}
		want := []string{"/w/a.ts", "/w/b.ts", "/w/c.ts"}
		if len(got) != len(want) {
			t.Fatalf("want %v, got %v", want, got)
		}
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("want %v, got %v", want, got)
			}
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no batch arrived")
	}
}

func TestWatchCoalesced_TheNEWESTVerdictPerPathWins(t *testing.T) {
	// A file written and then deleted is a deletion, not a modification. The batch describes the
	// current state of each path rather than logging how it got there, because the consumer is a
	// re-index: told "modified", it would try to read a file that is gone.
	src := newScriptedWatcher()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	batches, err := NewDebouncedWatcher(src, 80, 1).WatchCoalesced(ctx, []string{"ignored"})
	if err != nil {
		t.Fatalf("watch coalesced: %v", err)
	}

	src.send("/w/gone.ts", Create)
	src.send("/w/gone.ts", Modify)
	src.send("/w/gone.ts", Delete)

	select {
	case batch := <-batches:
		if len(batch) != 1 {
			t.Fatalf("want one entry, got %d", len(batch))
		}
		if batch[0].Type != Delete {
			t.Errorf("want the newest verdict (Delete), got %v", batch[0].Type)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no batch arrived")
	}
}

func TestWatchCoalesced_TwoBurstsSeparatedByQuietAreTwoBatches(t *testing.T) {
	// The other half of the contract: coalescing must not merge unrelated work. If it did, a watch
	// left running during steady editing would keep pushing its deadline out and never re-index.
	src := newScriptedWatcher()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	batches, err := NewDebouncedWatcher(src, 60, 1).WatchCoalesced(ctx, []string{"ignored"})
	if err != nil {
		t.Fatalf("watch coalesced: %v", err)
	}

	src.send("/w/first.ts", Modify)
	first := receiveBatch(t, batches)
	if len(first) != 1 || first[0].Path != "/w/first.ts" {
		t.Fatalf("unexpected first batch: %+v", first)
	}

	src.send("/w/second.ts", Modify)
	second := receiveBatch(t, batches)
	if len(second) != 1 || second[0].Path != "/w/second.ts" {
		t.Fatalf("unexpected second batch: %+v", second)
	}
}

func TestWatchCoalesced_PendingChangesAreFlushedWhenTheSourceCloses(t *testing.T) {
	// The Q-11 property, restated for batches: a file changed, an event was raised, and the index must
	// not silently fail to learn. Closing the source immediately after an event is the shape that
	// loses it if shutdown does not flush -- and this is exactly the class of defect the comment in
	// WatchDebounced records, where half the in-flight events were discarded at random.
	src := newScriptedWatcher()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	batches, err := NewDebouncedWatcher(src, 10_000, 1).WatchCoalesced(ctx, []string{"ignored"})
	if err != nil {
		t.Fatalf("watch coalesced: %v", err)
	}

	// A ten-second window, so nothing can be emitted by the timer within this test. The only way the
	// event arrives is the shutdown flush.
	src.send("/w/late.ts", Modify)
	time.Sleep(50 * time.Millisecond)
	close(src.events)

	select {
	case batch, ok := <-batches:
		if !ok {
			t.Fatal("the channel closed without flushing the pending change")
		}
		if len(batch) != 1 || batch[0].Path != "/w/late.ts" {
			t.Fatalf("unexpected flushed batch: %+v", batch)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("the pending change was never flushed")
	}
}

func TestWatchCoalesced_TheChannelClosesWhenTheSourceDoes(t *testing.T) {
	src := newScriptedWatcher()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	batches, err := NewDebouncedWatcher(src, 50, 1).WatchCoalesced(ctx, []string{"ignored"})
	if err != nil {
		t.Fatalf("watch coalesced: %v", err)
	}
	close(src.events)

	select {
	case _, ok := <-batches:
		if ok {
			t.Fatal("want a closed channel with nothing pending")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("the channel never closed")
	}
}

func TestWatchableDirectories_SkipsExactlyWhatTheScannerSkips(t *testing.T) {
	// Registering a wider set than the scanner will read would produce re-index requests for paths it
	// then declines, and `node_modules` on a real project exhausts the inotify watch limit -- at which
	// point `Add` starts failing for directories that DO matter.
	root := t.TempDir()
	for _, d := range []string{
		"src", filepath.Join("src", "deep"),
		".git", filepath.Join(".git", "objects"),
		"node_modules", filepath.Join("node_modules", "left-pad"),
		".pytest_cache", ".ruff_cache",
	} {
		if err := os.MkdirAll(filepath.Join(root, d), 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", d, err)
		}
	}

	dirs, err := WatchableDirectories(root)
	if err != nil {
		t.Fatalf("watchable directories: %v", err)
	}

	got := make(map[string]bool, len(dirs))
	for _, d := range dirs {
		rel, rerr := filepath.Rel(root, d)
		if rerr != nil {
			t.Fatalf("rel: %v", rerr)
		}
		got[filepath.ToSlash(rel)] = true
	}

	for _, want := range []string{".", "src", "src/deep"} {
		if !got[want] {
			t.Errorf("%s should be watched", want)
		}
	}
	for _, unwanted := range []string{
		".git", ".git/objects", "node_modules", "node_modules/left-pad", ".pytest_cache", ".ruff_cache",
	} {
		if got[unwanted] {
			t.Errorf("%s must not be watched", unwanted)
		}
	}
}

// receiveBatch reads one batch or fails the test.
func receiveBatch(t *testing.T, batches <-chan []Event) []Event {
	t.Helper()
	select {
	case batch, ok := <-batches:
		if !ok {
			t.Fatal("the batch channel closed early")
		}
		return batch
	case <-time.After(3 * time.Second):
		t.Fatal("no batch arrived")
		return nil
	}
}
