// SPDX-License-Identifier: Apache-2.0
package scanner

// NEGATIVE CONTROL for Q-11. Applied by `scripts/mutation-harness.py` via `go build -overlay`,
// which substitutes this file for `agent/internal/scanner/watcher.go` for the duration of one
// test run. It is never compiled into the agent.
//
// Byte-for-byte the committed `watcher.go` except for `WatchDebounced`'s goroutine, which now
// implements Appendix B's prescribed defect: a delete followed by a create on the same path
// becomes a no-op, so that path leaves the dirty set entirely.
//
// This file is GENERATED, so it cannot drift from the original it replaces.
//
// With this applied, TestPropertyQ11_DebouncingPreservesTheDirtySet must FAIL as soon as a
// generated sequence contains a delete and a create for one path -- which the deliberately small
// path alphabet makes common rather than rare.

import (
	"context"
	"path/filepath"
	"sync/atomic"

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
		defer close(out)

		// THE MUTATION (Q-11 negative control, Appendix B: "coalesce a delete followed by a
		// create into a no-op"). The raw stream is drained first, then any path that saw BOTH a
		// Delete and a Create is treated as a no-op and emitted for neither -- so the path leaves
		// the dirty set altogether and a rescan never visits it.
		var order []string
		sawDelete := map[string]bool{}
		sawCreate := map[string]bool{}
		events := map[string][]Event{}

		for {
			select {
			case <-ctx.Done():
				return
			case ev, ok := <-rawEvents:
				if !ok {
					goto flush
				}
				if _, seen := events[ev.Path]; !seen {
					order = append(order, ev.Path)
				}
				events[ev.Path] = append(events[ev.Path], ev)
				switch ev.Type {
				case Delete:
					sawDelete[ev.Path] = true
				case Create:
					sawCreate[ev.Path] = true
				}
			}
		}

	flush:
		for _, path := range order {
			if sawDelete[path] && sawCreate[path] {
				continue
			}
			for _, ev := range events[path] {
				select {
				case out <- ev:
				case <-ctx.Done():
					return
				}
			}
		}
	}()

	return out, nil
}
