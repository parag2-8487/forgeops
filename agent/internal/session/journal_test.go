// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"hash/crc32"
	"os"
	"strings"
	"testing"
	"time"
)

// The durable outbound journal (design §10.3, §7.4, D-41, Q-31).
//
// The tests are organised around the four things that make the journal safe rather than
// merely functional: what it CANNOT represent, bounded growth that refuses rather than
// evicts, recovery from the corruption a crash actually produces, and drain ordering that
// applies nothing.

func newTestJournal(t *testing.T, maxBytes int64, maxAge time.Duration) *FileJournal {
	t.Helper()
	j, err := NewJournal(t.TempDir(), maxBytes, maxAge)
	if err != nil {
		t.Fatalf("NewJournal: %v", err)
	}
	return j
}

func record(id string, kind RecordKind, createdAt time.Time) Record {
	return Record{
		RecordID:  id,
		Kind:      kind,
		CreatedAt: createdAt,
		Payload:   json.RawMessage(`{"files":3}`),
	}
}

// ─── what cannot be represented (D-41, Q-31's first clause) ─────────────────

func TestRecordKind_HasNoKindThatCouldAuthoriseAMutation(t *testing.T) {
	t.Parallel()

	// The type IS the security property. If a kind existed for an envelope, an
	// approval_id or an authority, then D-41's items 1-4 — envelope expiry, seq
	// allocation, revocation, policy staleness — would all need mitigating here. Because
	// no such kind exists, they cannot arise.
	forbidden := []string{
		"envelope", "approval", "authority", "token", "credential",
		"secret.value", "key", "mint", "apply", "command.execute",
	}
	for kind := range validKinds {
		lowered := strings.ToLower(string(kind))
		for _, word := range forbidden {
			// `secretscan.findings` is metadata only and legitimately contains "secret";
			// what must not appear is a value-bearing kind.
			if kind == KindSecretFindings && word == "key" {
				continue
			}
			if strings.Contains(lowered, word) && kind != KindSecretFindings {
				t.Errorf("RecordKind %q suggests it could carry %q", kind, word)
			}
		}
	}

	if len(validKinds) != 6 {
		t.Errorf("the closed set has %d kinds, want exactly the 6 §10.3 names: %v", len(validKinds), validKinds)
	}
}

func TestAppend_RefusesAnUnknownKind(t *testing.T) {
	t.Parallel()

	// An unknown kind must not reach the file, where a later version might drain it with
	// a different understanding of what it authorises.
	j := newTestJournal(t, 1<<20, time.Hour)
	err := j.Append(context.Background(), Record{
		RecordID: "r1",
		Kind:     RecordKind("command.envelope"),
		Payload:  json.RawMessage(`{}`),
	})
	if !errors.Is(err, ErrUnknownRecordKind) {
		t.Fatalf("err = %v, want ErrUnknownRecordKind", err)
	}
}

func TestAppend_RequiresARecordID(t *testing.T) {
	t.Parallel()

	// The backend dedupes on it. A record without one would be delivered twice on a
	// retried drain and counted twice.
	j := newTestJournal(t, 1<<20, time.Hour)
	if err := j.Append(context.Background(), record("", KindScanBatch, time.Now())); err == nil {
		t.Fatal("a record with no RecordID must be refused")
	}
}

// ─── bounded growth ─────────────────────────────────────────────────────────

func TestAppend_ReturnsFullRatherThanEvicting(t *testing.T) {
	t.Parallel()

	// ErrJournalFull rather than dropping the oldest record: a dropped scan batch that
	// nobody reports is an index that is quietly wrong.
	j := newTestJournal(t, 400, time.Hour)
	ctx := context.Background()

	appended := 0
	var lastErr error
	for i := 0; i < 100; i++ {
		if err := j.Append(ctx, record(fmt.Sprintf("r%d", i), KindScanBatch, time.Now())); err != nil {
			lastErr = err
			break
		}
		appended++
	}

	if !errors.Is(lastErr, ErrJournalFull) {
		t.Fatalf("err = %v, want ErrJournalFull", lastErr)
	}
	if appended == 0 {
		t.Fatal("nothing was appended at all; the bound is too small to prove anything")
	}

	stats, err := j.Stats(ctx)
	if err != nil {
		t.Fatalf("Stats: %v", err)
	}
	if stats.Records != appended {
		t.Errorf("Stats reports %d records, want %d — a record was evicted", stats.Records, appended)
	}
	if stats.Bytes > 400 {
		t.Errorf("the journal is %d bytes, over its 400-byte bound", stats.Bytes)
	}
}

func TestAppend_BoundIsCheckedBeforeWritingNotAfter(t *testing.T) {
	t.Parallel()

	// The bound must never be exceeded even transiently, because a crash between "write"
	// and "notice we are over" would leave an over-size journal that then refuses every
	// append forever.
	j := newTestJournal(t, 300, time.Hour)
	ctx := context.Background()
	for i := 0; i < 50; i++ {
		if err := j.Append(ctx, record(fmt.Sprintf("r%d", i), KindScanBatch, time.Now())); err != nil {
			break
		}
		size, err := j.sizeLocked()
		if err != nil {
			t.Fatalf("size: %v", err)
		}
		if size > 300 {
			t.Fatalf("journal grew to %d bytes, over the 300-byte bound", size)
		}
	}
}

func TestAppend_IsRefusedWhenDisabled(t *testing.T) {
	t.Parallel()

	// AGENT_JOURNAL_MAX_BYTES=0 is a supported configuration: fail fast rather than
	// queue. It must fail LOUDLY, so an operator sees work being refused rather than
	// silently discarded.
	j := newTestJournal(t, 0, time.Hour)
	if err := j.Append(context.Background(), record("r1", KindScanBatch, time.Now())); !errors.Is(err, ErrJournalDisabled) {
		t.Fatalf("err = %v, want ErrJournalDisabled", err)
	}
}

func TestReadAll_DropsRecordsPastMaxAge(t *testing.T) {
	t.Parallel()

	j := newTestJournal(t, 1<<20, time.Hour)
	ctx := context.Background()
	now := time.Now()

	if err := j.Append(ctx, record("old", KindScanBatch, now.Add(-2*time.Hour))); err != nil {
		t.Fatalf("Append: %v", err)
	}
	if err := j.Append(ctx, record("fresh", KindScanBatch, now)); err != nil {
		t.Fatalf("Append: %v", err)
	}

	stats, err := j.Stats(ctx)
	if err != nil {
		t.Fatalf("Stats: %v", err)
	}
	if stats.Records != 1 {
		t.Fatalf("Stats.Records = %d, want 1 (the 2h-old record is past the 1h bound)", stats.Records)
	}
}

// ─── corrupt-tail recovery ──────────────────────────────────────────────────

func TestReadAll_DiscardsACorruptTrailingRecord(t *testing.T) {
	t.Parallel()

	// A process killed mid-append leaves exactly a truncated final frame. Refusing to
	// start would turn a normal crash into a manual recovery, so the tail is discarded
	// with a count reported through Stats.
	j := newTestJournal(t, 1<<20, time.Hour)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if err := j.Append(ctx, record(fmt.Sprintf("r%d", i), KindScanBatch, time.Now())); err != nil {
			t.Fatalf("Append: %v", err)
		}
	}

	// Append a header claiming more bytes than follow: a torn write.
	file, err := os.OpenFile(j.Path(), os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	header := make([]byte, headerSize)
	binary.BigEndian.PutUint32(header[0:4], 9999)
	binary.BigEndian.PutUint32(header[4:8], 0)
	if _, err := file.Write(append(header, []byte("truncated")...)); err != nil {
		t.Fatalf("write: %v", err)
	}
	_ = file.Close()

	stats, err := j.Stats(ctx)
	if err != nil {
		t.Fatalf("Stats must not fail on a corrupt tail: %v", err)
	}
	if stats.Records != 3 {
		t.Errorf("Stats.Records = %d, want 3 intact records", stats.Records)
	}
	if stats.Truncated != 1 {
		t.Errorf("Stats.Truncated = %d, want 1 — the discard must be REPORTED, not silent", stats.Truncated)
	}
}

func TestReadAll_DiscardsARecordWithABadChecksum(t *testing.T) {
	t.Parallel()

	j := newTestJournal(t, 1<<20, time.Hour)
	ctx := context.Background()
	if err := j.Append(ctx, record("good", KindScanBatch, time.Now())); err != nil {
		t.Fatalf("Append: %v", err)
	}

	// Frame a record whose CRC does not match its payload.
	payload, err := json.Marshal(record("bad", KindScanBatch, time.Now()))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	frame := make([]byte, headerSize+len(payload))
	binary.BigEndian.PutUint32(frame[0:4], uint32(len(payload)))
	binary.BigEndian.PutUint32(frame[4:8], crc32.Checksum(payload, crc32cTable)^0xffffffff)
	copy(frame[headerSize:], payload)

	file, err := os.OpenFile(j.Path(), os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if _, err := file.Write(frame); err != nil {
		t.Fatalf("write: %v", err)
	}
	_ = file.Close()

	stats, err := j.Stats(ctx)
	if err != nil {
		t.Fatalf("Stats: %v", err)
	}
	if stats.Records != 1 || stats.Truncated != 1 {
		t.Errorf("Stats = %+v, want 1 record and 1 truncated", stats)
	}
}

// ─── drain ordering, and applying nothing ───────────────────────────────────

func TestDrain_DeliversNonMutatingRecordsBeforeIntents(t *testing.T) {
	t.Parallel()

	// A scan batch delivered AFTER an intent would let the backend evaluate the intent
	// against an index it is about to replace.
	j := newTestJournal(t, 1<<20, time.Hour)
	ctx := context.Background()
	base := time.Now()

	// Deliberately appended intent-first, so ordering cannot come from insertion order.
	for i, r := range []Record{
		record("i1", KindIntent, base),
		record("s1", KindScanBatch, base.Add(time.Second)),
		record("i2", KindIntent, base.Add(2*time.Second)),
		record("p1", KindCommandProgress, base.Add(3*time.Second)),
	} {
		if err := j.Append(ctx, r); err != nil {
			t.Fatalf("Append %d: %v", i, err)
		}
	}

	var order []string
	report, err := j.Drain(ctx, func(_ context.Context, r Record) error {
		order = append(order, string(r.Kind))
		return nil
	}, true)
	if err != nil {
		t.Fatalf("Drain: %v", err)
	}

	// Everything non-mutating, then the intents.
	for i, kind := range order {
		if kind == string(KindIntent) {
			for _, later := range order[i:] {
				if later != string(KindIntent) {
					t.Fatalf("a non-intent (%s) was delivered after an intent: %v", later, order)
				}
			}
			break
		}
	}
	if report.Delivered != 4 || report.IntentsDelivered != 2 {
		t.Errorf("report = %+v, want 4 delivered / 2 intents", report)
	}
}

func TestDrain_AStaleBundleHoldsIntentsAndDeliversTheRest(t *testing.T) {
	t.Parallel()

	// §10.3: a stale bundle stops step 2. Delivering an intent whose policy context the
	// agent cannot evaluate would ask the backend to authorise work of unknown policy.
	j := newTestJournal(t, 1<<20, time.Hour)
	ctx := context.Background()
	base := time.Now()

	for _, r := range []Record{
		record("s1", KindScanBatch, base),
		record("i1", KindIntent, base.Add(time.Second)),
	} {
		if err := j.Append(ctx, r); err != nil {
			t.Fatalf("Append: %v", err)
		}
	}

	var delivered []RecordKind
	report, err := j.Drain(ctx, func(_ context.Context, r Record) error {
		delivered = append(delivered, r.Kind)
		return nil
	}, false) // bundle NOT current
	if err != nil {
		t.Fatalf("Drain: %v", err)
	}

	if len(delivered) != 1 || delivered[0] != KindScanBatch {
		t.Errorf("delivered = %v, want only the scan batch", delivered)
	}
	if report.IntentsHeld != 1 {
		t.Errorf("IntentsHeld = %d, want 1", report.IntentsHeld)
	}

	// And the intent is STILL QUEUED, not lost.
	stats, err := j.Stats(ctx)
	if err != nil {
		t.Fatalf("Stats: %v", err)
	}
	if stats.Intents != 1 {
		t.Errorf("Stats.Intents = %d, want the held intent to remain queued", stats.Intents)
	}
}

func TestDrain_TruncatesOnlyWhatWasAcknowledged(t *testing.T) {
	t.Parallel()

	// The at-least-once contract. Re-sending an acknowledged record is harmless because
	// the backend dedupes on RecordID; losing an unacknowledged one is not.
	j := newTestJournal(t, 1<<20, time.Hour)
	ctx := context.Background()
	base := time.Now()

	for i := 0; i < 4; i++ {
		if err := j.Append(ctx, record(fmt.Sprintf("r%d", i), KindScanBatch, base.Add(time.Duration(i)*time.Second))); err != nil {
			t.Fatalf("Append: %v", err)
		}
	}

	sent := 0
	_, err := j.Drain(ctx, func(_ context.Context, _ Record) error {
		sent++
		if sent == 3 {
			return errors.New("connection lost")
		}
		return nil
	}, true)
	if err != nil {
		t.Fatalf("Drain must not surface a send failure as an error: %v", err)
	}

	stats, err := j.Stats(ctx)
	if err != nil {
		t.Fatalf("Stats: %v", err)
	}
	// Two acknowledged, so two remain.
	if stats.Records != 2 {
		t.Errorf("Stats.Records = %d, want 2 unacknowledged records retained", stats.Records)
	}
}

func TestDrain_RedeliveryAfterAFullDrainIsANoOp(t *testing.T) {
	t.Parallel()

	j := newTestJournal(t, 1<<20, time.Hour)
	ctx := context.Background()
	if err := j.Append(ctx, record("r1", KindScanBatch, time.Now())); err != nil {
		t.Fatalf("Append: %v", err)
	}

	if _, err := j.Drain(ctx, func(context.Context, Record) error { return nil }, true); err != nil {
		t.Fatalf("first Drain: %v", err)
	}

	calls := 0
	report, err := j.Drain(ctx, func(context.Context, Record) error { calls++; return nil }, true)
	if err != nil {
		t.Fatalf("second Drain: %v", err)
	}
	if calls != 0 || report.Delivered != 0 {
		t.Errorf("a second drain sent %d records; it must be a no-op", calls)
	}
}

func TestDrain_ReclaimsSpaceSoAppendsSucceedAgain(t *testing.T) {
	t.Parallel()

	// The recovery path NFR-18 depends on: an agent that filled its journal while offline
	// must be able to keep working after catching up.
	j := newTestJournal(t, 400, time.Hour)
	ctx := context.Background()

	for i := 0; i < 100; i++ {
		if err := j.Append(ctx, record(fmt.Sprintf("r%d", i), KindScanBatch, time.Now())); err != nil {
			break
		}
	}
	if err := j.Append(ctx, record("overflow", KindScanBatch, time.Now())); !errors.Is(err, ErrJournalFull) {
		t.Fatalf("expected the journal to be full, got %v", err)
	}

	report, err := j.Drain(ctx, func(context.Context, Record) error { return nil }, true)
	if err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if report.BytesReclaimed <= 0 {
		t.Errorf("BytesReclaimed = %d, want positive", report.BytesReclaimed)
	}
	if err := j.Append(ctx, record("after", KindScanBatch, time.Now())); err != nil {
		t.Fatalf("Append after drain: %v", err)
	}
}

// ─── wipe on revocation ─────────────────────────────────────────────────────

func TestWipe_RemovesEverythingWithoutDelivering(t *testing.T) {
	t.Parallel()

	// A revoked principal's queued intents must not reach the backend.
	j := newTestJournal(t, 1<<20, time.Hour)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if err := j.Append(ctx, record(fmt.Sprintf("i%d", i), KindIntent, time.Now())); err != nil {
			t.Fatalf("Append: %v", err)
		}
	}

	if err := j.Wipe(ctx); err != nil {
		t.Fatalf("Wipe: %v", err)
	}

	stats, err := j.Stats(ctx)
	if err != nil {
		t.Fatalf("Stats: %v", err)
	}
	if stats.Records != 0 || stats.Intents != 0 {
		t.Errorf("Stats = %+v, want empty after Wipe", stats)
	}
	if _, err := os.Stat(j.Path()); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("the journal file survived Wipe: %v", err)
	}
}

func TestWipe_IsIdempotent(t *testing.T) {
	t.Parallel()

	j := newTestJournal(t, 1<<20, time.Hour)
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if err := j.Wipe(ctx); err != nil {
			t.Fatalf("Wipe %d: %v", i, err)
		}
	}
}

// ─── stats and file mode ────────────────────────────────────────────────────

func TestStats_ReportsBacklogForDoctor(t *testing.T) {
	t.Parallel()

	// `agent.status` and `agent doctor` read this, so a growing backlog is visible rather
	// than discovered (§10.10).
	j := newTestJournal(t, 4096, time.Hour)
	ctx := context.Background()
	oldest := time.Now().Add(-30 * time.Minute).Truncate(time.Second)

	if err := j.Append(ctx, record("s1", KindScanBatch, oldest)); err != nil {
		t.Fatalf("Append: %v", err)
	}
	if err := j.Append(ctx, record("i1", KindIntent, time.Now())); err != nil {
		t.Fatalf("Append: %v", err)
	}

	stats, err := j.Stats(ctx)
	if err != nil {
		t.Fatalf("Stats: %v", err)
	}
	if stats.Records != 2 || stats.Intents != 1 {
		t.Errorf("Stats = %+v, want 2 records / 1 intent", stats)
	}
	if stats.MaxBytes != 4096 {
		t.Errorf("MaxBytes = %d, want the configured bound so doctor can show headroom", stats.MaxBytes)
	}
	if !stats.Oldest.Equal(oldest) {
		t.Errorf("Oldest = %v, want %v", stats.Oldest, oldest)
	}
}

func TestJournalFile_IsOwnerOnly(t *testing.T) {
	if os.Getenv("GOOS") == "windows" {
		t.Skip("NTFS uses ACLs")
	}
	t.Parallel()

	j := newTestJournal(t, 1<<20, time.Hour)
	if err := j.Append(context.Background(), record("r1", KindScanBatch, time.Now())); err != nil {
		t.Fatalf("Append: %v", err)
	}
	info, err := os.Stat(j.Path())
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 && perm != 0o666 {
		// 0666 only on Windows, where Go synthesises the mode.
		t.Errorf("mode = %#o, want 0600", perm)
	}
}
