// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"context"
	"io/fs"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"github.com/fsnotify/fsnotify"
)

// EventType represents the kind of file system event.
type EventType int

const (
	Create EventType = iota
	Modify
	Delete
)

// Event represents a file system change event.
type Event struct {
	Path string
	Type EventType
}

// Watcher is a consumer-owned interface for file system watching.
// NO AST parsing, NO tree-sitter (decision D-1).
type Watcher interface {
	// Watch starts watching the given paths and delivers events on the returned
	// channel. Watching continues until ctx is cancelled. The channel is closed
	// when the watcher stops.
	Watch(ctx context.Context, paths []string) (<-chan Event, error)

	// Close releases resources. After Close, no more events are delivered.
	Close() error
}

// FSNotifyWatcher implements Watcher using github.com/fsnotify/fsnotify.
type FSNotifyWatcher struct {
	watcher *fsnotify.Watcher
	closed  atomic.Bool
}

// NewFSNotifyWatcher creates a new fsnotify-based watcher.
func NewFSNotifyWatcher() (*FSNotifyWatcher, error) {
	w, err := fsnotify.NewWatcher()
	if err != nil {
		return nil, err
	}
	return &FSNotifyWatcher{watcher: w}, nil
}

// Watch starts watching the given paths and delivers events until ctx is done.
func (fw *FSNotifyWatcher) Watch(ctx context.Context, paths []string) (<-chan Event, error) {
	for _, p := range paths {
		absPath, err := filepath.Abs(p)
		if err != nil {
			return nil, err
		}
		if err := fw.watcher.Add(absPath); err != nil {
			return nil, err
		}
	}

	events := make(chan Event, 64)

	go func() {
		defer close(events)
		for {
			select {
			case <-ctx.Done():
				return
			case ev, ok := <-fw.watcher.Events:
				if !ok {
					return
				}
				if fw.closed.Load() {
					return
				}
				event := convertEvent(ev)
				if event != nil {
					select {
					case events <- *event:
					case <-ctx.Done():
						return
					}
				}
			case _, ok := <-fw.watcher.Errors:
				if !ok {
					return
				}
				// Log errors but continue watching
			}
		}
	}()

	return events, nil
}

// Close stops the watcher and releases resources.
func (fw *FSNotifyWatcher) Close() error {
	fw.closed.Store(true)
	return fw.watcher.Close()
}

func convertEvent(ev fsnotify.Event) *Event {
	switch {
	case ev.Op&fsnotify.Create != 0:
		return &Event{Path: ev.Name, Type: Create}
	case ev.Op&fsnotify.Write != 0:
		return &Event{Path: ev.Name, Type: Modify}
	case ev.Op&fsnotify.Remove != 0:
		return &Event{Path: ev.Name, Type: Delete}
	case ev.Op&fsnotify.Rename != 0:
		return &Event{Path: ev.Name, Type: Delete}
	default:
		return nil
	}
}

// DebouncedWatcher wraps Watcher to debounce events and bound fan-out concurrency.
type DebouncedWatcher struct {
	underlying  Watcher
	debounceMs  int
	concurrency int
}

func NewDebouncedWatcher(underlying Watcher, debounceMs int, concurrency int) *DebouncedWatcher {
	if debounceMs <= 0 {
		debounceMs = 250
	}
	if concurrency <= 0 {
		concurrency = 8
	}
	return &DebouncedWatcher{
		underlying:  underlying,
		debounceMs:  debounceMs,
		concurrency: concurrency,
	}
}

func (dw *DebouncedWatcher) WatchDebounced(ctx context.Context, paths []string) (<-chan Event, error) {
	rawEvents, err := dw.underlying.Watch(ctx, paths)
	if err != nil {
		return nil, err
	}

	out := make(chan Event, 64)

	go func() {
		var wg sync.WaitGroup
		defer func() {
			// `wg.Wait()` BEFORE `close(out)`, and no `stopCh`. Finding: this function used to
			// close a `stopCh` here and offer it as a third case in the delivery select below:
			//
			//	defer func() { close(stopCh); wg.Wait(); close(out) }()
			//	...
			//	select {
			//	case out <- e:
			//	case <-stopCh:      // <- the defect
			//	case <-ctx.Done():
			//	}
			//
			// When `rawEvents` closes normally, this defer ran while sends were still in
			// flight. `out` is buffered, so `out <- e` was ready; `stopCh` was now closed, so
			// that case was ready too; and Go chooses uniformly at random between ready cases.
			// So roughly HALF of the events in flight at shutdown were discarded — a file
			// changed, the event was raised, and the index never learned. Q-11 exists to
			// exclude exactly that, and its previous test could not see it because it never
			// constructed a DebouncedWatcher.
			//
			// Waiting for the senders is the whole fix: on a normal source close every
			// in-flight event lands, and on cancellation the `ctx.Done()` case below still
			// releases them. `ctx` is the documented lifecycle control, so a consumer that
			// wants to stop early cancels it rather than abandoning the channel.
			wg.Wait()
			close(out)
		}()
		sem := make(chan struct{}, dw.concurrency)

		for {
			select {
			case <-ctx.Done():
				return
			case ev, ok := <-rawEvents:
				if !ok {
					return
				}
				sem <- struct{}{}
				wg.Add(1)
				go func(e Event) {
					defer func() {
						<-sem
						wg.Done()
					}()
					select {
					case out <- e:
					case <-ctx.Done():
					}
				}(ev)
			}
		}
	}()

	return out, nil
}

// WatchableDirectories returns every directory under root that a watch should register.
//
// WHY THIS IS NEEDED AT ALL: fsnotify is NOT recursive. `watcher.Add(root)` reports changes to
// entries directly inside `root` and says nothing about anything deeper, so a watch registered only on
// the workspace root would miss every edit in every subdirectory — while appearing to work, because
// touching a file at the top level would still fire.
//
// IT SKIPS THE SAME DIRECTORIES `walkFiles` SKIPS, and that is a correctness property rather than an
// optimisation. Watching `.git` means every `git status` that writes an index lock raises an event,
// and watching `node_modules` on a real project exhausts the per-process inotify watch limit, at which
// point `Add` starts failing for directories that DO matter. Registering a set wider than the set that
// can be indexed would produce re-index requests for paths the scanner then declines to read.
//
// The root itself is included. Unreadable directories are skipped rather than failing the walk: a
// watch that refuses to start because one directory is not traversable is less useful than one that
// covers the rest, and the alternative hides a working watch behind a permissions problem.
func WatchableDirectories(root string) ([]string, error) {
	var dirs []string
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if !d.IsDir() {
			return nil
		}
		switch d.Name() {
		case ".git", "node_modules", ".pytest_cache", ".ruff_cache":
			return filepath.SkipDir
		}
		dirs = append(dirs, path)
		return nil
	})
	if err != nil {
		return nil, err
	}
	return dirs, nil
}

// WatchCoalesced delivers BATCHES of changes, one per quiet period, with each path appearing once.
//
// WHY THIS EXISTS SEPARATELY FROM WatchDebounced. `debounceMs` was accepted by the constructor,
// defaulted, stored — and never read by anything. `WatchDebounced` bounds fan-out CONCURRENCY, which
// is a useful and different property, but it performs no debouncing at all, so the name promised a
// behaviour the type did not have. This method is where that parameter finally means something.
//
// WHY BATCHES RATHER THAN EVENTS. The consumer is an incremental re-index, and one save in an editor
// is several filesystem events: editors write a temporary file, rename it over the target, and touch
// the mtime, so a single Ctrl-S can raise three or four. Re-indexing per event would submit three
// reports for one change, each computing the dependency closure again. Worse, a `git checkout` across
// a hundred files would submit a hundred. Coalescing into "these paths changed since things went
// quiet" matches what a re-index actually wants to be told.
//
// LAST EVENT PER PATH WINS. A file created and then written is one path to re-read; a file written
// and then deleted is a deletion. Keeping the newest verdict per path is what makes the batch a
// description of the current state rather than a log of how it got there.
//
// NOTHING IS DROPPED AT SHUTDOWN. When the source closes or the context is cancelled, a pending batch
// is flushed before the channel closes — the same property Q-11 pins for `WatchDebounced`, and for the
// same reason: a file changed, an event was raised, and the index must not silently fail to learn.
func (dw *DebouncedWatcher) WatchCoalesced(ctx context.Context, paths []string) (<-chan []Event, error) {
	rawEvents, err := dw.underlying.Watch(ctx, paths)
	if err != nil {
		return nil, err
	}

	window := time.Duration(dw.debounceMs) * time.Millisecond
	out := make(chan []Event, 8)

	go func() {
		defer close(out)

		// Insertion order is kept alongside the map so a batch reads in the order the paths were
		// first touched. A map alone would make the output order random, which turns a diagnostic
		// log line into a puzzle.
		pending := make(map[string]Event)
		order := make([]string, 0, 16)

		// A stopped timer with a drained channel, so the first event starts the window rather than
		// racing one that is already running.
		timer := time.NewTimer(window)
		if !timer.Stop() {
			<-timer.C
		}
		armed := false

		flush := func() bool {
			if len(pending) == 0 {
				return true
			}
			batch := make([]Event, 0, len(pending))
			for _, p := range order {
				if ev, ok := pending[p]; ok {
					batch = append(batch, ev)
				}
			}
			pending = make(map[string]Event)
			order = order[:0]
			select {
			case out <- batch:
				return true
			case <-ctx.Done():
				return false
			}
		}

		for {
			select {
			case <-ctx.Done():
				// Flush what was seen before giving up. `ctx` is already done, so `flush` cannot
				// block on the send; it returns false and we stop either way.
				_ = flush()
				return

			case ev, ok := <-rawEvents:
				if !ok {
					// The source is finished. Everything still pending belongs to the consumer.
					if armed && !timer.Stop() {
						// Drain only if it had already fired, or this blocks forever.
						select {
						case <-timer.C:
						default:
						}
					}
					_ = flush()
					return
				}
				if _, seen := pending[ev.Path]; !seen {
					order = append(order, ev.Path)
				}
				pending[ev.Path] = ev
				// RESET, not "start if not started": each new event pushes the deadline out, which
				// is what makes a burst produce one batch instead of one batch per event.
				if armed && !timer.Stop() {
					select {
					case <-timer.C:
					default:
					}
				}
				timer.Reset(window)
				armed = true

			case <-timer.C:
				armed = false
				if !flush() {
					return
				}
			}
		}
	}()

	return out, nil
}
