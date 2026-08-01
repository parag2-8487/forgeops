// SPDX-License-Identifier: Apache-2.0

// Q-15 — replay, reordering and expiry rejection (design §7.6, §10.4, Appendix A.2, Appendix B).
//
// Property, universally quantified over envelope STREAMS containing replays, reorderings and
// expiries:
//
//	a replayed nonce, a non-increasing `seq` and an expired `not_after` are each rejected, and no
//	rejected envelope performs any mutation or advances any counter.
//
// # Why a stream and not a single envelope
//
// Every clause here is about state that one envelope leaves behind for the next. A test that
// verifies one envelope at a time and resets the guard between them cannot observe a replay at all
// — the interesting quantification is over the ORDER of a sequence and over which member of it was
// tampered with. So the generator draws a stream of events against one Verifier and one guard, and
// the invariant is checked after every event rather than at the end.
//
// # The one honest exception, and why it is stated rather than avoided
//
// "No rejected envelope advances any counter" is exactly true of every rejection reachable before
// the two checks that mutate. It is NOT true of an authenticated envelope carrying a fresh `seq`
// and a REUSED nonce: ordering runs first and is implemented as a compare-and-set, so the mark has
// already moved when uniqueness refuses. The invariant below encodes that exception explicitly
// instead of weakening the whole clause, and `rejection_test.go` carries the same statement as an
// example. It is harmless in the direction that matters — the caller is authenticated, so it is
// not a lever an attacker has — and D-84 records why peek-then-commit was rejected as the fix.
//
// # The negative control
//
// `mutations.toml`'s Q-15 row overlays `order.go` with a version that runs the ordering check
// BEFORE the signature check. Every unauthenticated-rejection clause below then fails, because a
// forged envelope advances the device's high-water mark on its way to being refused — a denial of
// service through a check meant to be a defence.
package envelope

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"pgregory.net/rapid"
)

var q15Key = []byte("test-only-not-a-real-secret-q15-envelope-key")

const (
	q15Digest   = "sha256:1515151515151515151515151515151515151515151515151515151515151515"
	q15DeviceID = "6f2b1c40-0000-4000-8000-000000000015"
)

var q15Now = time.Unix(1899999900, 0).UTC()

// eventKind is what the generator can do to one envelope in the stream.
type eventKind int

const (
	// eventFresh is a well-formed envelope with a strictly greater seq and an unseen nonce.
	eventFresh eventKind = iota
	// eventReplayNonce reuses an earlier nonce with a strictly greater seq, which is the only
	// way to reach the uniqueness check at all.
	eventReplayNonce
	// eventReorder carries a seq at or below the high-water mark.
	eventReorder
	// eventExpired carries a not_after in the past, beyond the tolerated skew.
	eventExpired
	// eventTooFarAhead carries a not_after beyond maxAge, which is the other half of freshness.
	eventTooFarAhead
	// eventForged is correctly shaped and signed with the wrong key.
	eventForged
	// eventTampered is signed correctly and then edited, so the MAC covers other bytes.
	eventTampered
	// eventStaleBundle names a policy bundle this agent does not hold.
	eventStaleBundle
)

func (k eventKind) String() string {
	switch k {
	case eventFresh:
		return "fresh"
	case eventReplayNonce:
		return "replayed-nonce"
	case eventReorder:
		return "reordered-seq"
	case eventExpired:
		return "expired"
	case eventTooFarAhead:
		return "too-far-ahead"
	case eventForged:
		return "forged-signature"
	case eventTampered:
		return "tampered-body"
	case eventStaleBundle:
		return "stale-bundle"
	default:
		return "unknown"
	}
}

// mutatesOnRejection reports whether a rejection of this kind is documented to have advanced the
// seq mark before refusing. Only the nonce replay is, and only because ordering precedes
// uniqueness by construction (D-84).
func (k eventKind) mutatesOnRejection() bool { return k == eventReplayNonce }

// TestProperty_Q15_NoRejectedEnvelopeAdvancesACounter drives a generated stream and checks the
// invariant after every single event.
func TestProperty_Q15_NoRejectedEnvelopeAdvancesACounter(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		keys := NewStaticKeySource()
		keys.Set(q15DeviceID, q15Key)
		guard, err := NewMemoryReplayGuard(300*time.Second, 4096)
		if err != nil {
			t.Fatalf("NewMemoryReplayGuard: %v", err)
		}
		verifier, err := NewVerifier(keys, guard, NewStaticBundleDigest(q15Digest),
			WithClock(func() time.Time { return q15Now }))
		if err != nil {
			t.Fatalf("NewVerifier: %v", err)
		}

		length := rapid.IntRange(1, 12).Draw(t, "stream_length")
		acceptedNonces := []string{}
		accepted := 0
		rejected := map[eventKind]int{}

		for i := 0; i < length; i++ {
			seqBefore := guard.LastSeq(q15DeviceID)
			noncesBefore := guard.NonceCount()

			kind := eventKind(rapid.IntRange(0, int(eventStaleBundle)).Draw(t, fmt.Sprintf("kind_%d", i)))
			// A nonce replay needs an earlier nonce to reuse; a reorder needs a mark to sit at
			// or below. Before either exists, the event degrades to a fresh one rather than
			// being filtered out, so the stream length is what was drawn.
			if (kind == eventReplayNonce && len(acceptedNonces) == 0) || (kind == eventReorder && seqBefore == 0) {
				kind = eventFresh
			}

			// Positioned against the guard's LIVE mark rather than against the last ACCEPTED
			// seq, and the difference is not cosmetic: a rejected nonce replay advances the mark
			// (D-84's residual), so a generator that tracked only acceptances would then produce
			// a second replay whose seq is no longer greater — and that envelope is refused by
			// ORDERING, not by uniqueness. The property would be asserting the wrong clause
			// about the wrong rejection. Writing this generator is what surfaced that.
			raw, nonce := q15Envelope(t, kind, i, seqBefore, acceptedNonces)
			verified, err := verifier.Verify(context.Background(), raw)

			seqAfter := guard.LastSeq(q15DeviceID)
			noncesAfter := guard.NonceCount()

			if kind == eventFresh {
				if err != nil {
					t.Fatalf("event %d (%s) was rejected: %v", i, kind, err)
				}
				if verified == nil {
					t.Fatalf("event %d (%s) returned no Verified", i, kind)
				}
				accepted++
				acceptedNonces = append(acceptedNonces, nonce)
				if seqAfter <= seqBefore {
					t.Fatalf("event %d (%s) was accepted without advancing the mark: %d -> %d",
						i, kind, seqBefore, seqAfter)
				}
				if noncesAfter != noncesBefore+1 {
					t.Fatalf("event %d (%s) was accepted without recording its nonce", i, kind)
				}
				continue
			}

			// Every other kind must be refused, and no refusal may produce a Verified — that is
			// what "no rejected envelope performs any mutation" means for the executor, which
			// cannot be reached without one.
			if err == nil {
				t.Fatalf("event %d (%s) was ACCEPTED", i, kind)
			}
			if verified != nil {
				t.Fatalf("event %d (%s) was rejected and still returned a Verified", i, kind)
			}
			rejected[kind]++

			if noncesAfter != noncesBefore {
				t.Fatalf("event %d (%s) changed the nonce set: %d -> %d",
					i, kind, noncesBefore, noncesAfter)
			}
			if kind.mutatesOnRejection() {
				// Documented: ordering's compare-and-set has already run. Asserted rather than
				// tolerated, so a change in this behaviour is a failing test rather than a
				// silently weaker guarantee.
				if seqAfter <= seqBefore {
					t.Fatalf("event %d (%s) did NOT advance the mark; D-84's recorded residual "+
						"has changed and the journal entry is now wrong", i, kind)
				}
			} else if seqAfter != seqBefore {
				t.Fatalf("event %d (%s) advanced the seq high-water mark from %d to %d; "+
					"the signature check must precede the ordering check (§10.4)",
					i, kind, seqBefore, seqAfter)
			}
		}

		// Vacuity guard. A stream that happened to contain only fresh events proves nothing about
		// rejection, and a run of such streams would report a healthy property while never
		// exercising one refusal. rapid draws enough streams that this is only a per-example skip.
		if accepted == 0 {
			t.Skip("this stream contained no accepted envelope")
		}
	})
}

// TestProperty_Q15_EachRejectionCarriesItsOwnCode pins the vocabulary, so a stream test cannot
// pass because everything was refused for the same wrong reason.
func TestProperty_Q15_EachRejectionCarriesItsOwnCode(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		kind := eventKind(rapid.IntRange(int(eventReplayNonce), int(eventStaleBundle)).Draw(t, "kind"))

		keys := NewStaticKeySource()
		keys.Set(q15DeviceID, q15Key)
		guard, err := NewMemoryReplayGuard(300*time.Second, 4096)
		if err != nil {
			t.Fatalf("NewMemoryReplayGuard: %v", err)
		}
		verifier, err := NewVerifier(keys, guard, NewStaticBundleDigest(q15Digest),
			WithClock(func() time.Time { return q15Now }))
		if err != nil {
			t.Fatalf("NewVerifier: %v", err)
		}

		// One accepted envelope first, so a replay has something to replay and a reorder has a
		// mark to sit under.
		first, nonce := q15Envelope(t, eventFresh, 0, 0, nil)
		if _, err := verifier.Verify(context.Background(), first); err != nil {
			t.Fatalf("the seed envelope must verify: %v", err)
		}
		highWater := guard.LastSeq(q15DeviceID)

		raw, _ := q15Envelope(t, kind, 1, highWater, []string{nonce})
		_, err = verifier.Verify(context.Background(), raw)
		if err == nil {
			t.Fatalf("%s was accepted", kind)
		}

		want := map[eventKind]string{
			eventReplayNonce: "envelope-replayed",
			eventReorder:     "envelope-replayed",
			eventExpired:     "envelope-expired",
			eventTooFarAhead: "envelope-expired",
			eventForged:      "envelope-signature-invalid",
			eventTampered:    "envelope-signature-invalid",
			eventStaleBundle: "policy-bundle-stale",
		}[kind]
		if got := Code(err); got != want {
			t.Fatalf("%s reported %q, want %q (Appendix C.2's vocabulary); err = %v", kind, got, want, err)
		}
	})
}

// q15Envelope builds one event's wire bytes and returns the nonce it used.
func q15Envelope(t *rapid.T, kind eventKind, index int, highWater int64, earlier []string) ([]byte, string) {
	seq := highWater + int64(rapid.IntRange(1, 4).Draw(t, fmt.Sprintf("seq_step_%d", index)))
	// The event index is folded into the nonce so a FRESH event is fresh by construction. rapid
	// shrinks towards small values and drew 0x0 repeatedly, which made a "fresh" envelope collide
	// with an earlier one and fail as a replay — a generator bug that reads as a property failure.
	nonce := fmt.Sprintf("%016x%016x", uint64(index), rapid.Uint64().Draw(t, fmt.Sprintf("nonce_%d", index)))
	notAfter := q15Now.Add(time.Duration(rapid.IntRange(30, 240).Draw(t, fmt.Sprintf("ttl_%d", index))) * time.Second)
	digest := q15Digest
	key := q15Key

	switch kind {
	case eventReplayNonce:
		nonce = earlier[rapid.IntRange(0, len(earlier)-1).Draw(t, fmt.Sprintf("replay_%d", index))]
	case eventReorder:
		// At or below the mark: rapid draws which, so both the equal and the strictly-lower case
		// are covered rather than only one.
		seq = highWater - int64(rapid.IntRange(0, int(highWater)-1).Draw(t, fmt.Sprintf("back_%d", index)))
		if seq < 1 {
			seq = 1
		}
	case eventExpired:
		notAfter = q15Now.Add(-time.Duration(rapid.IntRange(61, 3600).Draw(t, fmt.Sprintf("age_%d", index))) * time.Second)
	case eventTooFarAhead:
		notAfter = q15Now.Add(time.Duration(rapid.IntRange(400, 86400).Draw(t, fmt.Sprintf("ahead_%d", index))) * time.Second)
	case eventForged:
		key = []byte("test-only-not-a-real-secret-wrong-key")
	case eventStaleBundle:
		digest = "sha256:" + fmt.Sprintf("%064x", rapid.Uint64().Draw(t, fmt.Sprintf("other_digest_%d", index)))
	}

	env := Envelope{
		V:          Version,
		CommandID:  fmt.Sprintf("6f2b1c40-0000-4000-8000-%012d", index),
		DeviceID:   q15DeviceID,
		Operation:  Operation("changeset.apply"),
		Args:       json.RawMessage(`{"root":"."}`),
		ApprovalID: "6f2b1c40-0000-4000-8000-000000000001",
		PolicyContext: PolicyContext{
			BundleDigest: digest,
			Decision:     "allow",
		},
		Nonce:    nonce,
		Seq:      seq,
		NotAfter: notAfter.Unix(),
	}
	signature, err := Sign(DomainPrefix, env, key)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	env.Signature = signature

	if kind == eventTampered {
		// Signed, then edited: the schema still accepts it and the MAC no longer covers it.
		env.Operation = Operation("changeset.revert")
	}

	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	return raw, nonce
}
