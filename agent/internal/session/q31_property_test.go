// SPDX-License-Identifier: Apache-2.0

// Q-31 — queue-and-revalidate (design §10.3, §17.1 D-41, Appendix B Q-31; leaf 8.12).
//
// Property, universally quantified over offline/reconnect sequences and journal contents:
//
//	no Record persisted by Journal.Append carries an envelope, an approval_id, a MutationAuthority,
//	a device token, an envelope key or a secret value; Journal.Drain applies nothing — every
//	KindIntent record produces a NEW chokepoint transit rather than a replay of a stored
//	authorisation; a revoked device wipes rather than drains; a stale bundle leaves intents queued;
//	and redelivery after an acknowledged batch is a no-op.
//
// # What makes the first clause provable at all
//
// D-41's design is that the TYPE cannot represent an authorisation: `RecordKind` has no kind for an
// envelope, an approval response, an authority, a device token or a secret value. So the property is
// not "we remembered not to write one" — it is "the vocabulary admits no such record", and the
// assertion is over the vocabulary plus what `Append` accepts. That is why the negative control
// mutates the vocabulary (D-87): breaking the fact the property rests on is the only control that
// can fail for the right reason.
//
// # Why the drain half is asserted here and not against a live backend
//
// "Every intent produces a new transit with a fresh approval_id, digest, nonce and seq" is a
// statement about what the BACKEND does when an `approval.request` arrives, and the backend's half
// is Q-03's and Q-04's. What the agent owes is narrower and is what this file proves: the drain
// sends intents as `approval.request` and never as anything that could be applied, it applies
// nothing itself, and it holds intents when the bundle is stale. `session/serve_test.go` carries the
// wire-level half — the frame really is `approval.request` — and `methodForRecord` refuses to invent
// a tenth method.
package session

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"pgregory.net/rapid"
)

// q31Forbidden is every shape that would make a journal record an authorisation.
//
// Values, not just member names: a payload that carried a token under an innocent key would pass a
// name-only check. The tokens here are the synthetic, self-labelling ones the store suite uses, so
// the needles are exactly what a real leak of THIS test's fixtures would look like.
var q31Forbidden = []string{
	"test-only-not-a-real-secret",
	"approval_id",
	"envelope",
	"signature",
	"device_token",
	"envelope_key",
	"authority",
	"bundle_digest",
}

// TestProperty_Q31_TheVocabularyCannotRepresentAnAuthorisation is the clause D-41 rests on.
func TestProperty_Q31_TheVocabularyCannotRepresentAnAuthorisation(t *testing.T) {
	// Not generated: the vocabulary is finite and the assertion is over all of it. Generating a
	// subset would make the clause weaker for no benefit — this is a "for all kinds" over a set of
	// six, so it is enumerated.
	for kind := range validKinds {
		lower := strings.ToLower(string(kind))
		for _, needle := range []string{"envelope", "approval", "authority", "token", "key", "secret value"} {
			if strings.Contains(lower, needle) {
				t.Errorf("RecordKind %q names %q; D-41's guarantee is that no kind can represent an "+
					"authorisation, and a kind whose NAME does is the first step to one that does", kind, needle)
			}
		}
	}
	if len(validKinds) != 6 {
		t.Errorf("the kind vocabulary has %d members; D-41 fixes six. A new one needs its own line "+
			"in this test and its own paragraph in the journal.", len(validKinds))
	}
	for _, required := range []RecordKind{
		KindScanBatch, KindCommandResult, KindCommandProgress, KindAgentStatus, KindSecretFindings, KindIntent,
	} {
		if !validKinds[required] {
			t.Errorf("kind %q is no longer valid; the drain's delivery map assumes it", required)
		}
	}
}

// TestProperty_Q31_AppendRefusesEveryKindOutsideTheVocabulary is the enforcement half.
//
// The vocabulary being clean is worth nothing if `Append` accepts a kind that is not in it, so this
// generates kind names — including the exact ones D-41 forbids — and requires each to be refused.
func TestProperty_Q31_AppendRefusesEveryKindOutsideTheVocabulary(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		journal, done := newQ31Journal(t)
		defer done()
		name := rapid.SampledFrom([]string{
			"envelope", "command.envelope", "approval.response", "authority",
			"device.token", "envelope.key", "secret", "", "scan.batch.v2",
		}).Draw(t, "kind")

		err := journal.Append(context.Background(), Record{
			RecordID:  "r1",
			Kind:      RecordKind(name),
			CreatedAt: time.Unix(1899999900, 0),
			Payload:   json.RawMessage(`{}`),
		})
		if err == nil {
			t.Fatalf("Append accepted kind %q, which is outside the vocabulary", name)
		}
		stats, err := journal.Stats(context.Background())
		if err != nil {
			t.Fatalf("Stats: %v", err)
		}
		if stats.Records != 0 {
			t.Fatalf("a refused Append persisted %d record(s)", stats.Records)
		}
	})
}

// TestProperty_Q31_NothingPersistedCarriesAnAuthorisation reads the FILE, not the API.
//
// The bytes on disk are what a stolen laptop gives an attacker, so the assertion is over the file's
// contents rather than over what `Stats` reports. Generated over sequences of appends so the check
// runs against a journal built the way an offline period builds one.
func TestProperty_Q31_NothingPersistedCarriesAnAuthorisation(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		journal, done := newQ31Journal(t)
		defer done()
		count := rapid.IntRange(1, 8).Draw(t, "records")

		for i := 0; i < count; i++ {
			record := drawRecord(t, i)
			if err := journal.Append(context.Background(), record); err != nil {
				t.Fatalf("Append: %v", err)
			}
		}

		raw, err := os.ReadFile(journal.Path())
		if err != nil {
			t.Fatalf("reading the journal: %v", err)
		}
		content := string(raw)
		for _, needle := range q31Forbidden {
			if strings.Contains(strings.ToLower(content), strings.ToLower(needle)) {
				t.Fatalf("the journal file contains %q; nothing that authorises a mutation may be "+
					"written to disk (D-41)", needle)
			}
		}
	})
}

// TestProperty_Q31_TheControlShowsThePayloadReallyReachesTheFile is what stops the clause above
// passing for a journal that wrote nothing at all.
func TestProperty_Q31_TheControlShowsThePayloadReallyReachesTheFile(t *testing.T) {
	journal, done := newQ31Journal(t)
	defer done()
	marker := "a-benign-payload-marker-0f5d"
	if err := journal.Append(context.Background(), Record{
		RecordID:  "r1",
		Kind:      KindAgentStatus,
		CreatedAt: time.Unix(1899999900, 0),
		Payload:   json.RawMessage(fmt.Sprintf(`{"state":%q}`, marker)),
	}); err != nil {
		t.Fatalf("Append: %v", err)
	}
	raw, err := os.ReadFile(journal.Path())
	if err != nil {
		t.Fatalf("reading the journal: %v", err)
	}
	if !strings.Contains(string(raw), marker) {
		t.Fatal("the payload did not reach the file, so the absence assertions above prove nothing")
	}
}

// TestProperty_Q31_DrainAppliesNothingAndHoldsIntentsWhenTheBundleIsStale covers the three drain
// clauses over generated journal contents and a generated bundle state.
func TestProperty_Q31_DrainAppliesNothingAndHoldsIntentsWhenTheBundleIsStale(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		journal, done := newQ31Journal(t)
		defer done()
		count := rapid.IntRange(1, 8).Draw(t, "records")
		bundleCurrent := rapid.Bool().Draw(t, "bundle_current")

		intents := 0
		for i := 0; i < count; i++ {
			record := drawRecord(t, i)
			if record.Kind == KindIntent {
				intents++
			}
			if err := journal.Append(context.Background(), record); err != nil {
				t.Fatalf("Append: %v", err)
			}
		}

		delivered := []Record{}
		report, err := journal.Drain(context.Background(), func(_ context.Context, r Record) error {
			// The drain hands the record to a SEND function and to nothing else. There is no
			// apply path to assert the absence of, which is the point: `Drain`'s only collaborator
			// is a callback, so "the drain applies nothing" is a property of its signature.
			delivered = append(delivered, r)
			return nil
		}, bundleCurrent)
		if err != nil {
			t.Fatalf("Drain: %v", err)
		}

		deliveredIntents := 0
		for _, record := range delivered {
			if record.Kind == KindIntent {
				deliveredIntents++
			}
		}

		if bundleCurrent {
			if deliveredIntents != intents {
				t.Fatalf("bundle current: %d of %d intents delivered", deliveredIntents, intents)
			}
			if report.IntentsHeld != 0 {
				t.Fatalf("bundle current: %d intents held", report.IntentsHeld)
			}
		} else {
			if deliveredIntents != 0 {
				t.Fatalf("stale bundle: %d intent(s) delivered; §10.3 holds them", deliveredIntents)
			}
			if report.IntentsHeld != intents {
				t.Fatalf("stale bundle: %d intents held, %d queued", report.IntentsHeld, intents)
			}
			// Held means still there. A drain that reported "held" and truncated anyway would lose
			// the user's offline work, which is the whole thing NFR-18 exists to prevent.
			stats, err := journal.Stats(context.Background())
			if err != nil {
				t.Fatalf("Stats: %v", err)
			}
			if stats.Intents != intents {
				t.Fatalf("stale bundle: %d intents left on disk, %d queued", stats.Intents, intents)
			}
		}

		// Non-intent records always drain, whatever the bundle says: §10.3 gates only step 2.
		if len(delivered)-deliveredIntents != count-intents {
			t.Fatalf("%d of %d non-mutating records delivered", len(delivered)-deliveredIntents, count-intents)
		}

		// Ordering: every non-intent precedes every intent, because an intent replayed before the
		// results it depends on would ask the backend to authorise work whose outcome it has not
		// been told about.
		seenIntent := false
		for _, record := range delivered {
			if record.Kind == KindIntent {
				seenIntent = true
				continue
			}
			if seenIntent {
				t.Fatal("a non-intent record was delivered after an intent; §10.3 fixes the order")
			}
		}
	})
}

// TestProperty_Q31_RedeliveryAfterAnAcknowledgedBatchIsANoOp is the at-least-once clause.
func TestProperty_Q31_RedeliveryAfterAnAcknowledgedBatchIsANoOp(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		journal, done := newQ31Journal(t)
		defer done()
		count := rapid.IntRange(1, 6).Draw(t, "records")
		for i := 0; i < count; i++ {
			if err := journal.Append(context.Background(), drawRecord(t, i)); err != nil {
				t.Fatalf("Append: %v", err)
			}
		}

		first := 0
		if _, err := journal.Drain(context.Background(), func(context.Context, Record) error {
			first++
			return nil
		}, true); err != nil {
			t.Fatalf("first Drain: %v", err)
		}
		if first != count {
			t.Fatalf("the first drain delivered %d of %d", first, count)
		}

		second := 0
		report, err := journal.Drain(context.Background(), func(context.Context, Record) error {
			second++
			return nil
		}, true)
		if err != nil {
			t.Fatalf("second Drain: %v", err)
		}
		if second != 0 || report.Delivered != 0 {
			t.Fatalf("a second drain re-delivered %d record(s); an acknowledged batch is truncated", second)
		}
	})
}

// TestProperty_Q31_AHaltedDrainKeepsEverythingUnacknowledged covers the failure direction, which is
// what makes the truncation above safe.
func TestProperty_Q31_AHaltedDrainKeepsEverythingUnacknowledged(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		journal, done := newQ31Journal(t)
		defer done()
		count := rapid.IntRange(2, 6).Draw(t, "records")
		for i := 0; i < count; i++ {
			if err := journal.Append(context.Background(), Record{
				RecordID:  fmt.Sprintf("r%d", i),
				Kind:      KindAgentStatus,
				CreatedAt: time.Unix(1899999900+int64(i), 0),
				Payload:   json.RawMessage(`{"state":"idle"}`),
			}); err != nil {
				t.Fatalf("Append: %v", err)
			}
		}
		failAt := rapid.IntRange(0, count-1).Draw(t, "fail_at")

		sent := 0
		if _, err := journal.Drain(context.Background(), func(context.Context, Record) error {
			if sent == failAt {
				return fmt.Errorf("the backend went away")
			}
			sent++
			return nil
		}, true); err != nil {
			t.Fatalf("Drain reported an error rather than a partial report: %v", err)
		}

		stats, err := journal.Stats(context.Background())
		if err != nil {
			t.Fatalf("Stats: %v", err)
		}
		if stats.Records != count-failAt {
			t.Fatalf("%d record(s) left after failing at %d of %d; everything unacknowledged must "+
				"survive, because the backend dedupes on RecordID and losing one is not recoverable",
				stats.Records, failAt, count)
		}
	})
}

// TestProperty_Q31_WipeLeavesNothingBehind is the revoked-device clause.
//
// The behavioural half — that revocation calls Wipe rather than Drain — is asserted in
// `serve_test.go` against a real `Serve` loop. What is asserted here is that Wipe leaves no bytes,
// because "we called the right method" and "the queued intents are gone" are different claims.
func TestProperty_Q31_WipeLeavesNothingBehind(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		journal, done := newQ31Journal(t)
		defer done()
		count := rapid.IntRange(1, 6).Draw(t, "records")
		for i := 0; i < count; i++ {
			if err := journal.Append(context.Background(), drawRecord(t, i)); err != nil {
				t.Fatalf("Append: %v", err)
			}
		}
		if err := journal.Wipe(context.Background()); err != nil {
			t.Fatalf("Wipe: %v", err)
		}
		if _, err := os.Stat(journal.Path()); !os.IsNotExist(err) {
			t.Fatalf("the journal file survived Wipe: %v", err)
		}
		stats, err := journal.Stats(context.Background())
		if err != nil {
			t.Fatalf("Stats after Wipe: %v", err)
		}
		if stats.Records != 0 || stats.Intents != 0 {
			t.Fatalf("Stats reports %d record(s) after Wipe", stats.Records)
		}
	})
}

// drawRecord generates one journal record, over every kind the vocabulary admits.
//
// The payloads are deliberately innocent: the point of the file-content assertions is that nothing
// in the SYSTEM writes an authorisation, so a generator that planted one would be testing itself.
// What is generated is the kind, the size and the member names, because a leak arrives as an
// innocent-looking key on a path nobody re-reads.
func drawRecord(t *rapid.T, index int) Record {
	kind := rapid.SampledFrom([]RecordKind{
		KindScanBatch, KindCommandResult, KindCommandProgress, KindAgentStatus, KindSecretFindings, KindIntent,
	}).Draw(t, fmt.Sprintf("kind_%d", index))

	payload := map[string]any{
		"seen":   rapid.IntRange(0, 1000).Draw(t, fmt.Sprintf("seen_%d", index)),
		"detail": rapid.StringMatching(`[a-z ]{0,24}`).Draw(t, fmt.Sprintf("detail_%d", index)),
	}
	switch kind {
	case KindCommandResult, KindCommandProgress:
		payload["command_id"] = fmt.Sprintf("cmd-%d", index)
		payload["status"] = "succeeded"
	case KindIntent:
		// An intent describes what the user WANTS, in the vocabulary of a request. It carries no
		// authority, which is the whole of D-41 in one field list.
		payload["reason"] = "an offline edit"
		payload["blast_radius"] = "file"
		payload["operation"] = "changeset.apply"
	case KindSecretFindings:
		payload["findings"] = rapid.IntRange(0, 5).Draw(t, fmt.Sprintf("findings_%d", index))
	}

	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshalling payload: %v", err)
	}
	return Record{
		RecordID:  fmt.Sprintf("record-%d", index),
		Kind:      kind,
		CreatedAt: time.Unix(1899999900+int64(index), 0).UTC(),
		Payload:   encoded,
	}
}

// newQ31Journal builds a FileJournal in a fresh temporary directory and returns its cleanup.
//
// `rapid.T` is not a `*testing.T` and has no `TempDir`, so the directory is made and removed here.
// The interface is narrowed to `Fatalf` so the same helper serves both a rapid property and an
// ordinary test — which is what lets the control below share the setup with the properties it
// controls.
func newQ31Journal(t interface {
	Fatalf(string, ...any)
}) (*FileJournal, func()) {
	dir, err := os.MkdirTemp("", "forgeops-q31-")
	if err != nil {
		t.Fatalf("MkdirTemp: %v", err)
	}
	journal, err := NewJournal(filepath.Join(dir, "state"), 1<<20, time.Hour)
	if err != nil {
		_ = os.RemoveAll(dir)
		t.Fatalf("NewJournal: %v", err)
	}
	return journal, func() { _ = os.RemoveAll(dir) }
}
