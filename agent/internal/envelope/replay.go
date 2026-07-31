// SPDX-License-Identifier: Apache-2.0

package envelope

import (
	"container/list"
	"context"
	"errors"
	"sync"
	"time"
)

// MemoryReplayGuard is the agent-side ReplayGuard: a per-device `seq` high-water mark and
// a bounded, age-limited nonce set (§7.6's ordering and uniqueness conditions).
//
// In-process and not persisted, deliberately. §7.6 makes the backend's copy
// Redis-authoritative and the agent's a bounded LRU, and D-41 explains why the agent's
// copy must not survive a restart: a persisted nonce set would be state an offline agent
// could be tricked into treating as authority, and the backend allocates `seq` anyway. A
// restarted agent simply re-learns the high-water mark from the first envelope it
// accepts, and the backend's own SETNX still refuses a replay.
//
// This is a real implementation, not a test double: leaf 8.6 wires it into the session
// Manager unchanged. Group 7 uses it so the six-step order is complete from the first
// commit rather than having three checks stubbed (D-59's cost note).
type MemoryReplayGuard struct {
	mu       sync.Mutex
	maxAge   time.Duration
	capacity int
	now      func() time.Time

	lastSeq map[string]int64
	nonces  map[string]*list.Element
	order   *list.List // front = oldest, so eviction is O(1)
}

type nonceEntry struct {
	key  string
	seen time.Time
}

// NewMemoryReplayGuard builds a guard covering at least maxAge, bounded to capacity
// nonces.
//
// The bound must cover maxAge, because a nonce evicted while its envelope is still fresh
// is a nonce that can be replayed — the uniqueness condition would silently narrow to
// "unique among the last N", which is not what §7.6 asserts. Both arguments are required
// and validated rather than defaulted silently, so an undersized guard is a construction
// error and not a quiet weakening.
func NewMemoryReplayGuard(maxAge time.Duration, capacity int) (*MemoryReplayGuard, error) {
	if maxAge <= 0 {
		return nil, errors.New("envelope: replay guard maxAge must be positive")
	}
	if capacity <= 0 {
		return nil, errors.New("envelope: replay guard capacity must be positive")
	}
	return &MemoryReplayGuard{
		maxAge:   maxAge,
		capacity: capacity,
		now:      time.Now,
		lastSeq:  make(map[string]int64),
		nonces:   make(map[string]*list.Element),
		order:    list.New(),
	}, nil
}

// SetClock replaces the clock. Present for tests that need to age entries without
// sleeping; it changes no decision the guard makes.
func (g *MemoryReplayGuard) SetClock(now func() time.Time) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.now = now
}

// SeenNonce reports whether the nonce was already accepted for deviceID, recording it
// when it was not.
//
// One method rather than Contains + Add, so the check and the record cannot be separated
// by a concurrent second envelope carrying the same nonce. Q-17's pairing-code control is
// the same shape: "read then delete" is not the same as an atomic consume.
func (g *MemoryReplayGuard) SeenNonce(_ context.Context, deviceID, nonce string) (bool, error) {
	if deviceID == "" || nonce == "" {
		return false, errors.New("envelope: deviceID and nonce are both required")
	}
	g.mu.Lock()
	defer g.mu.Unlock()

	now := g.now()
	g.evictExpiredLocked(now)

	key := deviceID + "\x00" + nonce
	if _, exists := g.nonces[key]; exists {
		return true, nil
	}
	element := g.order.PushBack(nonceEntry{key: key, seen: now})
	g.nonces[key] = element

	// Capacity eviction happens only after the age eviction above, so a full guard drops
	// its OLDEST entry rather than refusing the new one. Refusing would turn a busy
	// session into a denial of service against itself.
	for g.order.Len() > g.capacity {
		g.dropOldestLocked()
	}
	return false, nil
}

// AdvanceSeq accepts seq only when it is strictly greater than the stored high-water
// mark, and stores it in the same critical section.
//
// Strictly greater, so a re-sent envelope with an equal seq is refused: §7.6 says
// "strictly monotonic per-device `seq`", and `>=` would admit an exact replay.
func (g *MemoryReplayGuard) AdvanceSeq(_ context.Context, deviceID string, seq int64) (bool, error) {
	if deviceID == "" {
		return false, errors.New("envelope: deviceID is required")
	}
	if seq <= 0 {
		return false, errors.New("envelope: seq must be positive")
	}
	g.mu.Lock()
	defer g.mu.Unlock()

	if last, exists := g.lastSeq[deviceID]; exists && seq <= last {
		return false, nil
	}
	g.lastSeq[deviceID] = seq
	return true, nil
}

// LastSeq reports the stored high-water mark, for `agent.status` and `agent doctor`.
func (g *MemoryReplayGuard) LastSeq(deviceID string) int64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.lastSeq[deviceID]
}

// NonceCount reports how many nonces are held, so a test can assert the bound is real
// rather than assume it.
func (g *MemoryReplayGuard) NonceCount() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.order.Len()
}

func (g *MemoryReplayGuard) evictExpiredLocked(now time.Time) {
	cutoff := now.Add(-g.maxAge)
	for {
		front := g.order.Front()
		if front == nil {
			return
		}
		entry := front.Value.(nonceEntry)
		if entry.seen.After(cutoff) {
			return
		}
		g.order.Remove(front)
		delete(g.nonces, entry.key)
	}
}

func (g *MemoryReplayGuard) dropOldestLocked() {
	front := g.order.Front()
	if front == nil {
		return
	}
	entry := front.Value.(nonceEntry)
	g.order.Remove(front)
	delete(g.nonces, entry.key)
}

// StaticKeySource serves one envelope key per device from memory.
//
// The agent's real key arrives from §10.3's credential Store, which this package must not
// import (D-59's cycle). `session` constructs one of these from the loaded Credentials,
// so the key crosses the boundary as bytes rather than as a dependency.
type StaticKeySource struct {
	mu   sync.RWMutex
	keys map[string][]byte
}

// NewStaticKeySource builds an empty key source.
func NewStaticKeySource() *StaticKeySource {
	return &StaticKeySource{keys: make(map[string][]byte)}
}

// Set installs the key for a device, replacing any previous one.
//
// Copies the slice. Holding the caller's backing array would let a later write by the
// caller change the key this Verifier uses, which is a way to invalidate every signature
// with no diff at the call site.
func (s *StaticKeySource) Set(deviceID string, key []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	copied := make([]byte, len(key))
	copy(copied, key)
	s.keys[deviceID] = copied
}

// EnvelopeKey returns the key for deviceID.
func (s *StaticKeySource) EnvelopeKey(_ context.Context, deviceID string) ([]byte, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	key, ok := s.keys[deviceID]
	if !ok {
		return nil, errors.New("envelope: no key for device " + deviceID)
	}
	copied := make([]byte, len(key))
	copy(copied, key)
	return copied, nil
}

// StaticBundleDigest reports a fixed bundle digest.
//
// The real source is `internal/policy`'s loaded bundle (§10.6), which arrives in leaf
// 9.4. Until then this carries the digest the session was handed at connect. It returns
// an error for the empty digest rather than the empty string, so "no bundle loaded"
// cannot be read as "matches nothing" by a caller that forgot to check.
type StaticBundleDigest struct {
	mu     sync.RWMutex
	digest string
}

// NewStaticBundleDigest builds a source holding digest.
func NewStaticBundleDigest(digest string) *StaticBundleDigest {
	return &StaticBundleDigest{digest: digest}
}

// Set replaces the digest, as a bundle reload does.
func (s *StaticBundleDigest) Set(digest string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.digest = digest
}

// BundleDigest returns the held digest, or an error when none is held.
func (s *StaticBundleDigest) BundleDigest(_ context.Context) (string, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.digest == "" {
		return "", errors.New("envelope: no policy bundle is loaded")
	}
	return s.digest, nil
}
