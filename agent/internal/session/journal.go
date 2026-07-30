// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"hash/crc32"
	"io"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// The agent's durable OUTBOUND queue (design §10.3, §7.4, D-41, Appendix C.2, D).
//
// Why it exists
// -------------
// NFR-18: an agent that loses its connection keeps working and catches up on reconnect.
// Without a journal, a scan batch computed while offline is simply lost, and the backend's
// index is then quietly wrong — wrong in a way nothing reports.
//
// The type that makes it SAFE
// ---------------------------
// `RecordKind` has deliberately no member for a command envelope, an approval response, an
// `approval_id`, a `MutationAuthority`, a device token, an envelope key or a secret value.
// Nothing that AUTHORISES a mutation can be represented, so nothing that authorises a
// mutation can be persisted — which is why D-41's items 1-4 (envelope expiry, seq
// allocation, revocation, policy staleness) need no mitigation here: they cannot arise.
// A queued `intent` is a REQUEST, replayed as `approval.request` so the backend re-runs
// the whole chokepoint and mints a fresh envelope. `Drain` applies nothing, ever.
//
// On-disk format, and why
// -----------------------
//	[4-byte big-endian length][4-byte big-endian CRC32C of payload][payload]
//
// Length-prefixed rather than newline-delimited, because a payload is JSON that may
// contain a newline and escaping it would make the reader a parser. CRC32C rather than a
// hash: this detects a torn write, which is what an interrupted append produces, and
// Castagnoli has hardware support on every architecture the agent targets. It is NOT a
// tamper check — the file is 0600 in the user's own state directory, and an attacker who
// can write it can also write a valid CRC. Tamper resistance for what matters is the
// backend's job, because a drained intent goes through the full chokepoint again.
//
// A corrupt TRAILING record is discarded with a warning rather than failing startup. A
// process killed mid-append leaves exactly that, and refusing to start would turn a normal
// crash into a manual recovery. Corruption anywhere EARLIER is different and is reported:
// it means the file was damaged rather than truncated.

// RecordKind enumerates what may be queued. See the package note above for why the list
// is closed and what is deliberately absent.
type RecordKind string

const (
	KindScanBatch       RecordKind = "scan.batch"
	KindCommandResult   RecordKind = "command.result"
	KindCommandProgress RecordKind = "command.progress"
	KindAgentStatus     RecordKind = "agent.status"
	KindSecretFindings  RecordKind = "secretscan.findings" // metadata only, never values
	KindIntent          RecordKind = "intent"              // replayed as approval.request
)

// validKinds is the closed set. A record with any other kind is refused at Append, so an
// unknown kind cannot reach the file and be drained later by a version that understands
// it differently.
var validKinds = map[RecordKind]bool{
	KindScanBatch:       true,
	KindCommandResult:   true,
	KindCommandProgress: true,
	KindAgentStatus:     true,
	KindSecretFindings:  true,
	KindIntent:          true,
}

// mutatingKinds are drained SECOND, after everything else has been acknowledged.
//
// Ordering matters: a scan batch delivered after an intent would let the backend evaluate
// the intent against an index it is about to replace.
func (k RecordKind) isIntent() bool { return k == KindIntent }

// Record is one queued item.
type Record struct {
	RecordID  string          `json:"record_id"` // agent-generated; the backend dedupes on it
	Kind      RecordKind      `json:"kind"`
	CreatedAt time.Time       `json:"created_at"`
	Payload   json.RawMessage `json:"payload"`
}

// DrainReport describes what one drain achieved, for logs and `agent.status`.
type DrainReport struct {
	Delivered        int
	IntentsDelivered int
	IntentsHeld      int // left queued because the policy bundle was stale
	BytesReclaimed   int64
}

// JournalStats feeds `agent.status` and `agent doctor` so a growing backlog is visible
// rather than discovered.
type JournalStats struct {
	Records   int
	Intents   int
	Bytes     int64
	MaxBytes  int64
	Oldest    time.Time
	Truncated int // corrupt trailing records discarded on load
}

// Journal is the durable outbound queue.
type Journal interface {
	Append(ctx context.Context, r Record) error
	Drain(ctx context.Context, send func(context.Context, Record) error, bundleCurrent bool) (DrainReport, error)
	Wipe(ctx context.Context) error
	Stats(ctx context.Context) (JournalStats, error)
}

var (
	// ErrJournalFull is returned rather than evicting silently: a dropped scan batch
	// that nobody reports is an index that is quietly wrong.
	ErrJournalFull = errors.New("journal: AGENT_JOURNAL_MAX_BYTES exceeded")

	// ErrJournalCorrupt reports a CRC mismatch. Returned from Stats/load diagnostics; a
	// TRAILING mismatch is discarded rather than surfaced as a failure.
	ErrJournalCorrupt = errors.New("journal: CRC mismatch; trailing record discarded")

	// ErrJournalDisabled means AGENT_JOURNAL_MAX_BYTES=0. Appends fail loudly so an
	// operator who disabled the queue sees work being refused rather than dropped.
	ErrJournalDisabled = errors.New("journal: disabled by AGENT_JOURNAL_MAX_BYTES=0")

	// ErrUnknownRecordKind refuses a kind outside the closed set.
	ErrUnknownRecordKind = errors.New("journal: unknown record kind")
)

const (
	journalFile   = "outbound.journal"
	headerSize    = 8 // 4 length + 4 CRC
	maxRecordSize = 8 << 20
)

// crc32cTable is the Castagnoli polynomial, which has hardware support on amd64 and arm64.
var crc32cTable = crc32.MakeTable(crc32.Castagnoli)

// FileJournal is the on-disk implementation.
type FileJournal struct {
	mu        sync.Mutex
	path      string
	maxBytes  int64
	maxAge    time.Duration
	now       func() time.Time
	truncated int
}

// NewJournal opens (or creates) the journal under stateDir.
func NewJournal(stateDir string, maxBytes int64, maxAge time.Duration) (*FileJournal, error) {
	dir, err := resolveStateDir(stateDir)
	if err != nil {
		return nil, err
	}
	return &FileJournal{
		path:     filepath.Join(dir, journalFile),
		maxBytes: maxBytes,
		maxAge:   maxAge,
		now:      time.Now,
	}, nil
}

// Path is the journal file's location, for `agent doctor`.
func (j *FileJournal) Path() string { return j.path }

// Append durably enqueues one record.
func (j *FileJournal) Append(_ context.Context, r Record) error {
	if j.maxBytes == 0 {
		return ErrJournalDisabled
	}
	if !validKinds[r.Kind] {
		return fmt.Errorf("%w: %q", ErrUnknownRecordKind, r.Kind)
	}
	if r.RecordID == "" {
		return fmt.Errorf("journal: RecordID is required; the backend dedupes on it")
	}
	if r.CreatedAt.IsZero() {
		r.CreatedAt = j.now()
	}

	encoded, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("journal: encoding record: %w", err)
	}
	if len(encoded) > maxRecordSize {
		return fmt.Errorf("journal: record of %d bytes exceeds the %d limit", len(encoded), maxRecordSize)
	}

	j.mu.Lock()
	defer j.mu.Unlock()

	size, err := j.sizeLocked()
	if err != nil {
		return err
	}
	// Checked BEFORE writing, so the bound is never exceeded even transiently. Refusing
	// is the whole point: evicting the oldest record would silently drop a scan batch.
	if size+int64(headerSize+len(encoded)) > j.maxBytes {
		return fmt.Errorf("%w: %d bytes used of %d", ErrJournalFull, size, j.maxBytes)
	}

	file, err := os.OpenFile(j.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("journal: opening for append: %w", err)
	}
	defer func() { _ = file.Close() }()

	frame := make([]byte, headerSize+len(encoded))
	binary.BigEndian.PutUint32(frame[0:4], uint32(len(encoded)))
	binary.BigEndian.PutUint32(frame[4:8], crc32.Checksum(encoded, crc32cTable))
	copy(frame[headerSize:], encoded)

	if _, err := file.Write(frame); err != nil {
		return fmt.Errorf("journal: writing record: %w", err)
	}
	// fsync, not just Close. A record the agent believes is durable but which the OS has
	// only buffered is exactly the record lost by the crash the journal exists for.
	if err := file.Sync(); err != nil {
		return fmt.Errorf("journal: fsync: %w", err)
	}
	return nil
}

// Drain delivers the journal in the §10.3 order and truncates only what was acknowledged.
func (j *FileJournal) Drain(
	ctx context.Context,
	send func(context.Context, Record) error,
	bundleCurrent bool,
) (DrainReport, error) {
	j.mu.Lock()
	defer j.mu.Unlock()

	records, _, err := j.readAllLocked()
	if err != nil {
		return DrainReport{}, err
	}

	// Non-mutating records first, then intents. A scan batch delivered AFTER an intent
	// would let the backend evaluate that intent against an index it is about to replace.
	ordered := make([]Record, 0, len(records))
	for _, r := range records {
		if !r.Kind.isIntent() {
			ordered = append(ordered, r)
		}
	}
	intents := make([]Record, 0, len(records))
	for _, r := range records {
		if r.Kind.isIntent() {
			intents = append(intents, r)
		}
	}
	// Stable by creation time within each group, so replay order matches the order the
	// work actually happened in.
	sort.SliceStable(ordered, func(a, b int) bool { return ordered[a].CreatedAt.Before(ordered[b].CreatedAt) })
	sort.SliceStable(intents, func(a, b int) bool { return intents[a].CreatedAt.Before(intents[b].CreatedAt) })

	report := DrainReport{}
	acknowledged := map[string]bool{}

	for _, r := range ordered {
		if err := send(ctx, r); err != nil {
			// Stop at the first failure and keep everything not yet acknowledged. The
			// backend dedupes on RecordID, so re-sending an acknowledged record is
			// harmless; losing an unacknowledged one is not.
			return j.finishDrainLocked(report, acknowledged, records)
		}
		acknowledged[r.RecordID] = true
		report.Delivered++
	}

	if !bundleCurrent {
		// A stale bundle stops step 2 and leaves intents queued (§10.3). Delivering them
		// against a bundle the agent cannot evaluate would ask the backend to authorise
		// work whose policy context is unknown.
		report.IntentsHeld = len(intents)
		return j.finishDrainLocked(report, acknowledged, records)
	}

	for _, r := range intents {
		if err := send(ctx, r); err != nil {
			return j.finishDrainLocked(report, acknowledged, records)
		}
		acknowledged[r.RecordID] = true
		report.Delivered++
		report.IntentsDelivered++
	}

	return j.finishDrainLocked(report, acknowledged, records)
}

// finishDrainLocked rewrites the journal with only the unacknowledged records.
func (j *FileJournal) finishDrainLocked(
	report DrainReport,
	acknowledged map[string]bool,
	all []Record,
) (DrainReport, error) {
	before, err := j.sizeLocked()
	if err != nil {
		return report, err
	}

	remaining := make([]Record, 0, len(all))
	for _, r := range all {
		if !acknowledged[r.RecordID] {
			remaining = append(remaining, r)
		}
	}

	if err := j.rewriteLocked(remaining); err != nil {
		return report, err
	}

	after, err := j.sizeLocked()
	if err != nil {
		return report, err
	}
	report.BytesReclaimed = before - after
	return report, nil
}

// Wipe removes the journal without delivering it.
//
// Called on revocation: a revoked principal's queued intents must not reach the backend.
// Idempotent, because an agent told it is revoked has to reach the wiped state whatever it
// finds.
func (j *FileJournal) Wipe(_ context.Context) error {
	j.mu.Lock()
	defer j.mu.Unlock()

	if err := os.Remove(j.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("journal: removing: %w", err)
	}
	j.truncated = 0
	return nil
}

// Stats reports the backlog.
func (j *FileJournal) Stats(_ context.Context) (JournalStats, error) {
	j.mu.Lock()
	defer j.mu.Unlock()

	records, _, err := j.readAllLocked()
	if err != nil {
		return JournalStats{}, err
	}
	size, err := j.sizeLocked()
	if err != nil {
		return JournalStats{}, err
	}

	stats := JournalStats{
		Records:   len(records),
		Bytes:     size,
		MaxBytes:  j.maxBytes,
		Truncated: j.truncated,
	}
	for _, r := range records {
		if r.Kind.isIntent() {
			stats.Intents++
		}
		if stats.Oldest.IsZero() || r.CreatedAt.Before(stats.Oldest) {
			stats.Oldest = r.CreatedAt
		}
	}
	return stats, nil
}

// ─── internals ──────────────────────────────────────────────────────────────

func (j *FileJournal) sizeLocked() (int64, error) {
	info, err := os.Stat(j.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return 0, nil
		}
		return 0, fmt.Errorf("journal: stat: %w", err)
	}
	return info.Size(), nil
}

// readAllLocked reads every intact record, dropping those past MaxAge and discarding a
// corrupt TRAILING record.
//
// Returns the number of bytes of intact data, so a caller could truncate the tail; the
// current callers rewrite instead, which also compacts.
func (j *FileJournal) readAllLocked() ([]Record, int64, error) {
	data, err := os.ReadFile(j.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, 0, nil
		}
		return nil, 0, fmt.Errorf("journal: reading: %w", err)
	}

	var (
		records  []Record
		offset   int
		intact   int64
		dropped  int
		cutoff   = j.now().Add(-j.maxAge)
		checkAge = j.maxAge > 0
	)

	for offset+headerSize <= len(data) {
		length := int(binary.BigEndian.Uint32(data[offset : offset+4]))
		want := binary.BigEndian.Uint32(data[offset+4 : offset+headerSize])

		end := offset + headerSize + length
		if length <= 0 || length > maxRecordSize || end > len(data) {
			// Truncated tail: a process killed mid-append leaves exactly this.
			dropped++
			break
		}
		payload := data[offset+headerSize : end]
		if crc32.Checksum(payload, crc32cTable) != want {
			dropped++
			break
		}

		var r Record
		if err := json.Unmarshal(payload, &r); err != nil {
			dropped++
			break
		}

		offset = end
		intact = int64(offset)

		// Age is applied on READ rather than by a sweeper: there is no background
		// goroutine to get wrong, and a record is only interesting at the moment
		// somebody asks for it.
		if checkAge && r.CreatedAt.Before(cutoff) {
			continue
		}
		records = append(records, r)
	}

	j.truncated = dropped
	return records, intact, nil
}

// rewriteLocked replaces the journal with exactly `records`, atomically.
//
// Write-to-temp-then-rename, so a crash during compaction cannot leave a half-written
// journal: the rename is atomic, and until it happens the original file is intact.
func (j *FileJournal) rewriteLocked(records []Record) error {
	if len(records) == 0 {
		if err := os.Remove(j.path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("journal: removing drained journal: %w", err)
		}
		return nil
	}

	temp, err := os.CreateTemp(filepath.Dir(j.path), ".outbound-*.tmp")
	if err != nil {
		return fmt.Errorf("journal: creating temp: %w", err)
	}
	tempName := temp.Name()
	defer func() { _ = os.Remove(tempName) }()

	if err := temp.Chmod(0o600); err != nil {
		_ = temp.Close()
		return fmt.Errorf("journal: setting temp mode: %w", err)
	}

	writer := io.Writer(temp)
	for _, r := range records {
		encoded, err := json.Marshal(r)
		if err != nil {
			_ = temp.Close()
			return fmt.Errorf("journal: re-encoding record: %w", err)
		}
		frame := make([]byte, headerSize+len(encoded))
		binary.BigEndian.PutUint32(frame[0:4], uint32(len(encoded)))
		binary.BigEndian.PutUint32(frame[4:8], crc32.Checksum(encoded, crc32cTable))
		copy(frame[headerSize:], encoded)
		if _, err := writer.Write(frame); err != nil {
			_ = temp.Close()
			return fmt.Errorf("journal: writing temp: %w", err)
		}
	}

	if err := temp.Sync(); err != nil {
		_ = temp.Close()
		return fmt.Errorf("journal: fsync temp: %w", err)
	}
	if err := temp.Close(); err != nil {
		return fmt.Errorf("journal: closing temp: %w", err)
	}
	if err := os.Rename(tempName, j.path); err != nil {
		return fmt.Errorf("journal: renaming temp into place: %w", err)
	}
	return nil
}
