// SPDX-License-Identifier: Apache-2.0

// Every rejection path, and what it is and is not allowed to change (§10.4, Appendix A.2,
// property Q-15).
//
// A.2's postcondition is the subject: "no mutation is performed on any failure path". The two
// checks that mutate are ordering (a seq compare-and-set) and uniqueness (a nonce record), so
// the assertion is that a rejection reaching neither of them leaves the replay state exactly as
// it was. Written as a table over every reachable rejection rather than as one case per bug
// somebody remembered, because the failure mode this guards against is a NEW check being added
// after the mutating pair and quietly turning its own refusal into a state change.
package envelope

import (
	"context"
	"encoding/json"
	"testing"
	"time"
)

// rejectionCase is one refusal, and the mutation it must not perform.
type rejectionCase struct {
	name string
	// raw builds the frame. It receives the fixed clock so a freshness case can position
	// `not_after` relative to it rather than to wall-clock time.
	raw  func(t *testing.T, now time.Time) []byte
	code string
}

func rejectionCases() []rejectionCase {
	return []rejectionCase{
		{
			name: "not JSON at all",
			raw:  func(*testing.T, time.Time) []byte { return []byte("{not json") },
			code: "envelope-malformed",
		},
		{
			name: "an unknown member",
			raw: func(t *testing.T, _ time.Time) []byte {
				return withMember(t, sampleEnvelope(), "extra", "surprise")
			},
			code: "envelope-malformed",
		},
		{
			name: "an unsupported version",
			raw: func(t *testing.T, _ time.Time) []byte {
				e := sampleEnvelope()
				e.V = "2"
				return signed(t, e)
			},
			code: "envelope-malformed",
		},
		{
			name: "an empty command_id",
			raw: func(t *testing.T, _ time.Time) []byte {
				return withMember(t, sampleEnvelope(), "command_id", "")
			},
			code: "envelope-malformed",
		},
		{
			name: "a zero seq",
			raw: func(t *testing.T, _ time.Time) []byte {
				e := sampleEnvelope()
				e.Seq = 0
				return signed(t, e)
			},
			code: "envelope-malformed",
		},
		{
			name: "expired past the skew window",
			raw: func(t *testing.T, now time.Time) []byte {
				e := sampleEnvelope()
				e.NotAfter = now.Add(-2 * time.Minute).Unix()
				return signed(t, e)
			},
			code: "envelope-expired",
		},
		{
			name: "not_after further ahead than the max age",
			raw: func(t *testing.T, now time.Time) []byte {
				e := sampleEnvelope()
				e.NotAfter = now.Add(2 * time.Hour).Unix()
				return signed(t, e)
			},
			code: "envelope-expired",
		},
		{
			name: "a forged signature",
			raw: func(t *testing.T, _ time.Time) []byte {
				e := sampleEnvelope()
				e.Seq = 999999
				e.Signature = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
				return mustMarshal(t, e)
			},
			code: "envelope-signature-invalid",
		},
		{
			name: "a body mutated under a valid signature",
			raw: func(t *testing.T, _ time.Time) []byte {
				e := sampleEnvelope()
				raw := signed(t, e)
				var decoded Envelope
				if err := json.Unmarshal(raw, &decoded); err != nil {
					t.Fatalf("Unmarshal: %v", err)
				}
				decoded.Args = json.RawMessage(`{"root":"/etc"}`)
				return mustMarshal(t, decoded)
			},
			code: "envelope-signature-invalid",
		},
		{
			name: "an unknown device, so no key",
			raw: func(t *testing.T, _ time.Time) []byte {
				e := sampleEnvelope()
				e.DeviceID = "dev-not-registered"
				return signed(t, e)
			},
			code: "envelope-signature-invalid",
		},
		{
			// D-84's case, and the reason the order changed. Policy binding is not a mutating
			// check, so placed after ordering and uniqueness it consumed the envelope's seq
			// and burned its nonce on the way out — a rejection that changes state.
			name: "a stale policy bundle digest",
			raw: func(t *testing.T, _ time.Time) []byte {
				e := sampleEnvelope()
				e.PolicyContext.BundleDigest = "sha256:a-digest-this-agent-does-not-hold"
				return signed(t, e)
			},
			code: "policy-bundle-stale",
		},
	}
}

func mustMarshal(t *testing.T, e Envelope) []byte {
	t.Helper()
	raw, err := json.Marshal(e)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	return raw
}

// withMember re-marshals a signed envelope with one member replaced, leaving the signature in
// place. The signature therefore covers different bytes, which is the point for the schema
// cases: they must be refused before the signature check ever runs.
func withMember(t *testing.T, e Envelope, member string, value any) []byte {
	t.Helper()
	var asMap map[string]any
	if err := json.Unmarshal(signed(t, e), &asMap); err != nil {
		t.Fatalf("Unmarshal: %v", err)
	}
	asMap[member] = value
	raw, err := json.Marshal(asMap)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	return raw
}

// newInspectableVerifier returns a Verifier and the guard it uses, so a test can read the
// replay state directly instead of inferring it from a later Verify.
func newInspectableVerifier(t *testing.T, now time.Time) (*Verifier, *MemoryReplayGuard) {
	t.Helper()
	keys := NewStaticKeySource()
	keys.Set("dev-0001", testKey)
	guard, err := NewMemoryReplayGuard(300*time.Second, 1024)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	verifier, err := NewVerifier(keys, guard, NewStaticBundleDigest(testDigest),
		WithClock(func() time.Time { return now }))
	if err != nil {
		t.Fatalf("NewVerifier: %v", err)
	}
	return verifier, guard
}

func TestVerify_NoRejectionBeforeTheMutatingChecksAdvancesAnything(t *testing.T) {
	fixedNow := time.Unix(1899999900, 0).UTC()
	for _, tc := range rejectionCases() {
		t.Run(tc.name, func(t *testing.T) {
			verifier, guard := newInspectableVerifier(t, fixedNow)

			verified, err := verifier.Verify(context.Background(), tc.raw(t, fixedNow))
			if err == nil {
				t.Fatalf("%s was accepted", tc.name)
			}
			if verified != nil {
				t.Error("a rejection returned a non-nil *Verified; the executor could be reached")
			}
			if got := Code(err); got != tc.code {
				t.Errorf("code = %q, want %q (Appendix C.2's vocabulary); err = %v", got, tc.code, err)
			}
			if got := guard.LastSeq("dev-0001"); got != 0 {
				t.Errorf("the seq high-water mark moved to %d; a rejected envelope must advance no counter", got)
			}
			if got := guard.NonceCount(); got != 0 {
				t.Errorf("%d nonce(s) burned by a rejected envelope", got)
			}
		})
	}
}

func TestVerify_TheControlShowsTheSameStateChangesOnAcceptance(t *testing.T) {
	// Without this, every clause above would pass for a Verifier that never touched the
	// replay guard at all — which would also mean §7.6's ordering and uniqueness conditions
	// were not being enforced.
	fixedNow := time.Unix(1899999900, 0).UTC()
	verifier, guard := newInspectableVerifier(t, fixedNow)

	if _, err := verifier.Verify(context.Background(), signed(t, sampleEnvelope())); err != nil {
		t.Fatalf("the sample envelope must verify: %v", err)
	}
	if got := guard.LastSeq("dev-0001"); got != sampleEnvelope().Seq {
		t.Errorf("LastSeq = %d after acceptance, want %d", got, sampleEnvelope().Seq)
	}
	if got := guard.NonceCount(); got != 1 {
		t.Errorf("NonceCount = %d after acceptance, want 1", got)
	}
}

func TestVerify_AnOrderingRejectionBurnsNoNonce(t *testing.T) {
	// The ordering check is allowed to refuse; it is not allowed to consume the nonce of the
	// envelope it refused. If it did, the backend could not resend that envelope with a
	// corrected seq — it would then be refused as a replayed nonce, which is a different and
	// misleading error.
	fixedNow := time.Unix(1899999900, 0).UTC()
	verifier, guard := newInspectableVerifier(t, fixedNow)

	first := sampleEnvelope()
	if _, err := verifier.Verify(context.Background(), signed(t, first)); err != nil {
		t.Fatalf("first Verify: %v", err)
	}
	stale := sampleEnvelope()
	stale.Seq = first.Seq // not strictly greater
	stale.Nonce = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	if _, err := verifier.Verify(context.Background(), signed(t, stale)); Code(err) != "envelope-replayed" {
		t.Fatalf("a non-increasing seq gave %v (code %q)", err, Code(err))
	}
	if got := guard.LastSeq("dev-0001"); got != first.Seq {
		t.Errorf("LastSeq = %d, want it unchanged at %d", got, first.Seq)
	}
	if got := guard.NonceCount(); got != 1 {
		t.Errorf("NonceCount = %d; the refused envelope's nonce was burned", got)
	}
}

// TestVerify_AUniquenessRejectionDoesAdvanceSeq records what is TRUE rather than what would be
// tidy, because the difference matters to whoever reads Q-15's property next.
//
// An authenticated envelope carrying a fresh seq and a REUSED nonce advances the high-water
// mark before the uniqueness check runs, because ordering is implemented as a compare-and-set
// and the set is what makes it atomic under concurrency. Splitting it into peek-then-commit
// would make the postcondition exact and the atomicity a lie, which is the worse trade: §7.6
// makes the backend's copy of this state Redis-authoritative through a Lua CAS for the same
// reason.
//
// It is harmless in the direction that matters. The caller is authenticated — the signature
// check has already passed — so this is not a lever an attacker has. And the backend allocates
// seq monotonically, so no legitimate envelope is lost by the mark having moved.
func TestVerify_AUniquenessRejectionDoesAdvanceSeq(t *testing.T) {
	fixedNow := time.Unix(1899999900, 0).UTC()
	verifier, guard := newInspectableVerifier(t, fixedNow)

	first := sampleEnvelope()
	if _, err := verifier.Verify(context.Background(), signed(t, first)); err != nil {
		t.Fatalf("first Verify: %v", err)
	}
	replay := sampleEnvelope()
	replay.Seq = first.Seq + 1 // fresh seq, reused nonce
	if _, err := verifier.Verify(context.Background(), signed(t, replay)); Code(err) != "envelope-replayed" {
		t.Fatalf("a reused nonce gave %v (code %q)", err, Code(err))
	}
	if got := guard.LastSeq("dev-0001"); got != replay.Seq {
		t.Errorf("LastSeq = %d, want %d — the compare-and-set is documented to have happened", got, replay.Seq)
	}
	if got := guard.NonceCount(); got != 1 {
		t.Errorf("NonceCount = %d; a reused nonce must not be recorded twice", got)
	}
}

// TestVerify_AnEmptyApprovalIdIsAcceptedHere is D-83.
//
// §7.7's read-only operations carry no `approval_id`, so refusing an empty one here refused
// valid envelopes the backend legitimately sends. The requirement is operation-dependent and
// therefore belongs in the dispatcher, which is where `executor` asserts that a MUTATING
// operation with an empty `approval_id` is refused. This case is the "wrong by refusal" half
// disappearing; `executor`'s is the half that replaces it.
func TestVerify_AnEmptyApprovalIdIsAcceptedHere(t *testing.T) {
	fixedNow := time.Unix(1899999900, 0).UTC()
	verifier, _ := newInspectableVerifier(t, fixedNow)

	e := sampleEnvelope()
	e.ApprovalID = ""
	verified, err := verifier.Verify(context.Background(), signed(t, e))
	if err != nil {
		t.Fatalf("an envelope with no approval_id must verify (D-83): %v", err)
	}
	if verified.ApprovalID() != "" {
		t.Errorf("ApprovalID() = %q, want the empty string to survive verification", verified.ApprovalID())
	}
}

func TestVerifier_ClockSkewIsTheDesignsSixtySeconds(t *testing.T) {
	verifier, _ := newInspectableVerifier(t, time.Unix(1899999900, 0).UTC())
	if got := verifier.ClockSkew(); got != 60*time.Second {
		t.Errorf("ClockSkew() = %s, want the ±60 s §7.6 fixes", got)
	}
}
