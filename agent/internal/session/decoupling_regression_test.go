// SPDX-License-Identifier: Apache-2.0
package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/connection"
)

func TestSessionDecoupling_ReconnectFlushesPending(t *testing.T) {
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore failed: %v", err)
	}

	m, err := NewManager("wss://backend.local", Deps{
		Store: store,
	})
	if err != nil {
		t.Fatalf("NewManager failed: %v", err)
	}

	ctx := context.Background()

	// 1. Initially no active session. Notify enqueues into pending.
	m.notify(ctx, "command.progress", map[string]any{"command_id": "cmd-1", "percent": 55})
	m.notify(ctx, "command.result", map[string]any{"command_id": "cmd-1", "status": "applied"})

	m.pendingMu.Lock()
	if len(m.pending) != 2 {
		t.Fatalf("expected 2 pending notifications, got %d", len(m.pending))
	}
	m.pendingMu.Unlock()

	// 2. Session 1 connects.
	t1 := newFakeTransport(&connectResult{SessionID: "s-1", HeartbeatInterval: 30, HeartbeatTimeout: 90})
	live1 := &liveSession{
		manager:   m,
		transport: t1,
	}
	m.setActiveSession(live1)

	// Flush pending into live1
	live1.flushPending(ctx)

	// Pending should now be empty
	m.pendingMu.Lock()
	if len(m.pending) != 0 {
		t.Fatalf("expected 0 pending notifications after flush, got %d", len(m.pending))
	}
	m.pendingMu.Unlock()

	// Transport 1 should have received both frames
	t1.mu.Lock()
	if len(t1.sent) != 2 {
		t.Fatalf("expected 2 sent frames on transport 1, got %d", len(t1.sent))
	}
	if t1.sent[0].Method != "command.progress" || t1.sent[1].Method != "command.result" {
		t.Fatalf("unexpected sent methods: %v, %v", t1.sent[0].Method, t1.sent[1].Method)
	}
	t1.mu.Unlock()

	// 3. Session 1 drops
	m.clearActiveSession(live1)

	// Another notification arrives while disconnected
	m.notify(ctx, "command.result", map[string]any{"command_id": "cmd-2", "status": "applied"})

	// 4. Session 2 connects
	t2 := newFakeTransport(&connectResult{SessionID: "s-2", HeartbeatInterval: 30, HeartbeatTimeout: 90})
	live2 := &liveSession{
		manager:   m,
		transport: t2,
	}
	m.setActiveSession(live2)
	live2.flushPending(ctx)

	t2.mu.Lock()
	if len(t2.sent) != 1 {
		t.Fatalf("expected 1 sent frame on transport 2, got %d", len(t2.sent))
	}
	if t2.sent[0].Method != "command.result" {
		t.Fatalf("unexpected method on transport 2: %v", t2.sent[0].Method)
	}
	t2.mu.Unlock()
}

func TestSessionDecoupling_DroppingPolicyPreservesResults(t *testing.T) {
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore failed: %v", err)
	}

	m, err := NewManager("wss://backend.local", Deps{
		Store: store,
	})
	if err != nil {
		t.Fatalf("NewManager failed: %v", err)
	}

	ctx := context.Background()

	// Add 500 progress frames
	for i := 0; i < 500; i++ {
		m.notify(ctx, "command.progress", map[string]any{"command_id": "cmd-1", "percent": i})
	}

	// Add a command result
	m.notify(ctx, "command.result", map[string]any{"command_id": "cmd-1", "status": "applied"})

	// Add another 10 progress frames which should push out older progress frames, not the result!
	for i := 500; i < 510; i++ {
		m.notify(ctx, "command.progress", map[string]any{"command_id": "cmd-1", "percent": i})
	}

	m.pendingMu.Lock()
	defer m.pendingMu.Unlock()

	if len(m.pending) > 500 {
		t.Fatalf("expected max 500 pending notifications, got %d", len(m.pending))
	}

	hasResult := false
	for _, p := range m.pending {
		if p.method == "command.result" {
			hasResult = true
			break
		}
	}
	if !hasResult {
		t.Fatalf("command.result was erroneously dropped by queue cap")
	}
}

func TestSessionDecoupling_FlushFailureRequeuesRemaining(t *testing.T) {
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore failed: %v", err)
	}

	m, err := NewManager("wss://backend.local", Deps{
		Store: store,
	})
	if err != nil {
		t.Fatalf("NewManager failed: %v", err)
	}

	ctx := context.Background()

	m.notify(ctx, "command.progress", map[string]any{"percent": 10})
	m.notify(ctx, "command.progress", map[string]any{"percent": 20})
	m.notify(ctx, "command.result", map[string]any{"status": "applied"})

	// Transport that fails on second send
	failingTransport := &flakyTransport{failAfter: 1}
	live := &liveSession{
		manager:   m,
		transport: failingTransport,
	}

	live.flushPending(ctx)

	// The first item succeeded; the remaining 2 must be requeued back in order
	m.pendingMu.Lock()
	defer m.pendingMu.Unlock()

	if len(m.pending) != 2 {
		t.Fatalf("expected 2 items re-queued, got %d", len(m.pending))
	}
	if m.pending[0].params["percent"] != 20 {
		t.Fatalf("expected percent 20 at head, got %v", m.pending[0].params["percent"])
	}
	if m.pending[1].method != "command.result" {
		t.Fatalf("expected command.result at second position, got %v", m.pending[1].method)
	}
}

func TestSessionDecoupling_JournalDrainPromotesPayloadKeys(t *testing.T) {
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore failed: %v", err)
	}

	m, err := NewManager("wss://backend.local", Deps{
		Store: store,
	})
	if err != nil {
		t.Fatalf("NewManager failed: %v", err)
	}

	payloadBytes, _ := json.Marshal(map[string]any{
		"command_id": "cmd-xyz-99",
		"status":     "applied",
		"output":     "container running",
	})

	records := []Record{
		{
			RecordID: "rec-1",
			Kind:     KindCommandResult,
			Payload:  payloadBytes,
		},
		{
			RecordID: "rec-2",
			Kind:     KindCommandProgress,
			Payload:  []byte(`not valid json`), // malformed payload fallback test
		},
	}

	fakeJournal := &inMemoryJournal{records: records}
	m.journal = fakeJournal

	t1 := newFakeTransport(&connectResult{SessionID: "s-1", HeartbeatInterval: 30, HeartbeatTimeout: 90})
	live := &liveSession{
		manager:   m,
		transport: t1,
	}

	live.drain(context.Background())

	t1.mu.Lock()
	defer t1.mu.Unlock()

	if len(t1.sent) != 2 {
		t.Fatalf("expected 2 sent requests from journal drain, got %d", len(t1.sent))
	}

	// First record should have promoted command_id and status to top level
	req1 := t1.sent[0]
	if req1.Method != "command.result" {
		t.Fatalf("expected method command.result, got %s", req1.Method)
	}
	var params1 map[string]any
	if err := json.Unmarshal(req1.Params, &params1); err != nil {
		t.Fatalf("failed to unmarshal params: %v", err)
	}
	if params1["command_id"] != "cmd-xyz-99" {
		t.Fatalf("expected top-level command_id cmd-xyz-99, got %v", params1["command_id"])
	}
	if params1["status"] != "applied" {
		t.Fatalf("expected top-level status applied, got %v", params1["status"])
	}

	// Second record with malformed payload should fallback cleanly without panic
	req2 := t1.sent[1]
	if req2.Method != "command.progress" {
		t.Fatalf("expected method command.progress, got %s", req2.Method)
	}
	var params2 map[string]any
	if err := json.Unmarshal(req2.Params, &params2); err != nil {
		t.Fatalf("failed to unmarshal params: %v", err)
	}
	if params2["payload"] == nil {
		t.Fatalf("expected fallback payload key for malformed json")
	}
}

func TestSessionDecoupling_ConcurrentNotify(t *testing.T) {
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore failed: %v", err)
	}

	m, err := NewManager("wss://backend.local", Deps{
		Store: store,
	})
	if err != nil {
		t.Fatalf("NewManager failed: %v", err)
	}

	ctx := context.Background()
	var wg sync.WaitGroup
	workers := 20
	iterations := 50

	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(workerID int) {
			defer wg.Done()
			for i := 0; i < iterations; i++ {
				m.notify(ctx, "command.progress", map[string]any{
					"command_id": fmt.Sprintf("cmd-%d", workerID),
					"percent":    i,
				})
			}
			m.notify(ctx, "command.result", map[string]any{
				"command_id": fmt.Sprintf("cmd-%d", workerID),
				"status":     "applied",
			})
		}(w)
	}

	wg.Wait()

	m.pendingMu.Lock()
	total := len(m.pending)
	m.pendingMu.Unlock()

	if total == 0 {
		t.Fatalf("expected pending items from concurrent notifications")
	}
}

type flakyTransport struct {
	mu        sync.Mutex
	failAfter int
	count     int
}

func (f *flakyTransport) Dial(ctx context.Context, url string, header http.Header) error { return nil }
func (f *flakyTransport) Send(ctx context.Context, payload []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.count++
	if f.count > f.failAfter {
		return errors.New("simulated send failure")
	}
	return nil
}
func (f *flakyTransport) Receive(ctx context.Context) ([]byte, error) { return nil, nil }
func (f *flakyTransport) Ping(ctx context.Context) error             { return nil }
func (f *flakyTransport) Close(code connection.StatusCode, reason string) error {
	return nil
}

type inMemoryJournal struct {
	records []Record
}

func (j *inMemoryJournal) Append(ctx context.Context, r Record) error {
	j.records = append(j.records, r)
	return nil
}

func (j *inMemoryJournal) Drain(ctx context.Context, send func(context.Context, Record) error, bundleCurrent bool) (DrainReport, error) {
	delivered := 0
	for _, r := range j.records {
		if err := send(ctx, r); err != nil {
			return DrainReport{Delivered: delivered}, err
		}
		delivered++
	}
	j.records = nil
	return DrainReport{Delivered: delivered}, nil
}

func (j *inMemoryJournal) Wipe(ctx context.Context) error {
	j.records = nil
	return nil
}

func (j *inMemoryJournal) Stats(ctx context.Context) (JournalStats, error) {
	return JournalStats{Records: len(j.records)}, nil
}
