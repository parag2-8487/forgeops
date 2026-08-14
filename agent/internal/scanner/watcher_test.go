// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestFSNotifyWatcher_EventDelivery(t *testing.T) {
	dir := t.TempDir()

	w, err := NewFSNotifyWatcher()
	if err != nil {
		t.Fatalf("new watcher: %v", err)
	}
	defer w.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	events, err := w.Watch(ctx, []string{dir})
	if err != nil {
		t.Fatalf("watch: %v", err)
	}

	// Give watcher time to start
	time.Sleep(100 * time.Millisecond)

	// Create a file
	testFile := filepath.Join(dir, "test.txt")
	if err := os.WriteFile(testFile, []byte("hello"), 0o644); err != nil {
		t.Fatalf("write file: %v", err)
	}

	// Wait for create event
	var gotCreate bool
	timeout := time.After(3 * time.Second)
	for !gotCreate {
		select {
		case ev, ok := <-events:
			if !ok {
				t.Fatal("events channel closed unexpectedly")
			}
			if ev.Type == Create && filepath.Base(ev.Path) == "test.txt" {
				gotCreate = true
			}
		case <-timeout:
			t.Fatal("timeout waiting for create event")
		}
	}

	// Modify the file
	if err := os.WriteFile(testFile, []byte("modified"), 0o644); err != nil {
		t.Fatalf("write file: %v", err)
	}

	// Wait for modify event
	var gotModify bool
	timeout = time.After(3 * time.Second)
	for !gotModify {
		select {
		case ev, ok := <-events:
			if !ok {
				t.Fatal("events channel closed unexpectedly")
			}
			if ev.Type == Modify && filepath.Base(ev.Path) == "test.txt" {
				gotModify = true
			}
		case <-timeout:
			t.Fatal("timeout waiting for modify event")
		}
	}

	// Delete the file
	if err := os.Remove(testFile); err != nil {
		t.Fatalf("remove file: %v", err)
	}

	// Wait for delete event
	var gotDelete bool
	timeout = time.After(3 * time.Second)
	for !gotDelete {
		select {
		case ev, ok := <-events:
			if !ok {
				t.Fatal("events channel closed unexpectedly")
			}
			if ev.Type == Delete && filepath.Base(ev.Path) == "test.txt" {
				gotDelete = true
			}
		case <-timeout:
			t.Fatal("timeout waiting for delete event")
		}
	}
}

func TestFSNotifyWatcher_Shutdown(t *testing.T) {
	dir := t.TempDir()

	w, err := NewFSNotifyWatcher()
	if err != nil {
		t.Fatalf("new watcher: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())

	events, err := w.Watch(ctx, []string{dir})
	if err != nil {
		t.Fatalf("watch: %v", err)
	}

	// Cancel context should stop event delivery
	cancel()

	// Channel should eventually close
	timeout := time.After(2 * time.Second)
	select {
	case _, ok := <-events:
		if ok {
			// Drain any remaining events
			for range events {
			}
		}
	case <-timeout:
		t.Fatal("timeout waiting for channel close after context cancel")
	}

	w.Close()
}

func TestFSNotifyWatcher_NoEventsAfterClose(t *testing.T) {
	dir := t.TempDir()

	w, err := NewFSNotifyWatcher()
	if err != nil {
		t.Fatalf("new watcher: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	events, err := w.Watch(ctx, []string{dir})
	if err != nil {
		t.Fatalf("watch: %v", err)
	}

	// Close the watcher
	w.Close()

	// Give goroutine time to see close
	time.Sleep(200 * time.Millisecond)

	// Create a file after close — should not generate events
	os.WriteFile(filepath.Join(dir, "after-close.txt"), []byte("nope"), 0o644)

	// Wait briefly and check no event arrives
	time.Sleep(500 * time.Millisecond)

	// Drain the channel
	var gotAfterClose bool
	for {
		select {
		case ev, ok := <-events:
			if !ok {
				goto done
			}
			if filepath.Base(ev.Path) == "after-close.txt" {
				gotAfterClose = true
			}
		default:
			goto done
		}
	}
done:
	if gotAfterClose {
		t.Error("received event after close")
	}
}

func TestDebouncedWatcher_FanOutAndDelivery(t *testing.T) {
	dir := t.TempDir()

	fw, err := NewFSNotifyWatcher()
	if err != nil {
		t.Fatalf("new watcher: %v", err)
	}
	defer fw.Close()

	dw := NewDebouncedWatcher(fw, 100, 4)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	events, err := dw.WatchDebounced(ctx, []string{dir})
	if err != nil {
		t.Fatalf("watch debounced: %v", err)
	}

	time.Sleep(100 * time.Millisecond)

	// Create test file
	testFile := filepath.Join(dir, "debounced.txt")
	os.WriteFile(testFile, []byte("data"), 0o644)

	select {
	case ev := <-events:
		if filepath.Base(ev.Path) != "debounced.txt" {
			t.Errorf("unexpected event path: %s", ev.Path)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("timeout waiting for debounced event")
	}
}
