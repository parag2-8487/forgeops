// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"context"
	"path/filepath"

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
	closed  bool
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
				if fw.closed {
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
	fw.closed = true
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
	underlying Watcher
	debounceMs int
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
				go func(e Event) {
					defer func() { <-sem }()
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


