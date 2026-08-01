// SPDX-License-Identifier: Apache-2.0

// Q-14 — canonicalisation and signature verification (design §7.6, §10.4, Appendix A.2,
// Appendix B Q-14).
//
// Property, universally quantified over envelopes:
//
//	CanonicalBytes is byte-identical in Go and Python for the same logical envelope;
//	signature verification accepts exactly the correctly signed envelope and rejects every
//	single-byte mutation.
//
// # How the cross-runtime half is discharged, stated plainly
//
// "Byte-identical in Go and Python" cannot be asserted from inside one runtime. Two shapes were
// available. Generate here and shell out to the backend virtualenv per batch — rejected, because
// the agent CI job has no Python environment and a test that cannot run there is finding 63's
// shape; and a conditional would be a skip in disguise, which §0.4.4 forbids.
//
// So the cross-runtime clause is discharged over the COMMITTED corpus, which both runtimes read:
// `agent/testdata/envelopes/*.json` carries each envelope with its expected canonical bytes as
// hex, and `corpus_test.go` here plus `backend/tests/unit/test_governance_envelope.py` there
// assert their own implementation against the same committed bytes. A divergence fails both
// suites. This file carries the GENERATED half — determinism, member-order independence, JCS
// shape, exact-acceptance and single-byte rejection — and `backend/tests/property/
// test_q14_envelope_canonicalisation.py` carries the same generated clauses on the Python side,
// over a generator of the same shape.
//
// The residual is real and worth naming: a divergence that only appears for an envelope shape the
// corpus does not contain would be caught by neither. The mitigation is that the two generators
// draw from the same declared shape, and the corpus covers the corners both runtimes disagreed on
// while it was being built (integer bounds, UTF-16 key order, string escaping, empty containers).
//
// # The negative control
//
// `mutations.toml`'s Q-14 row overlays `domain.go` with a version whose prefix is empty — the
// domain-separation prefix removed on one side only. Three clauses here object, and the committed
// corpus objects as well, which is exactly the "one side only" the control describes.
package envelope

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"pgregory.net/rapid"
)

// q14Key is synthetic and self-labelling, per `.kiro/steering/secret-safety.md`.
var q14Key = []byte("test-only-not-a-real-secret-q14-envelope-key")

const q14Digest = "sha256:1414141414141414141414141414141414141414141414141414141414141414"

// q14Now is the fixed clock every generated envelope is positioned against, so freshness is not
// a source of flakiness and `not_after` can be drawn inside the window on purpose.
var q14Now = time.Unix(1899999900, 0).UTC()

// drawEnvelope generates one logically valid envelope.
//
// The shape is the declared one: every member present (canonicalisation is defined over the member
// set, so an absent member and an empty one are different bytes), no floats anywhere, integers
// inside RFC 8785's exact domain. `approval_id` is drawn including the empty string, because D-83
// moved that requirement to the dispatcher and an empty one must canonicalise and verify here.
//
// The identifiers are UUID-shaped even though this side only requires them to be non-empty: the
// Python signer REFUSES anything that is not a UUID, so drawing free strings here would make the
// two properties quantify over two different shapes and the cross-runtime claim weaker than it
// reads. The asymmetry itself is safe — the backend is the only minter, and the permissive side is
// the verifier — and it is stated here so nobody has to rediscover it.
func drawEnvelope(t *rapid.T) Envelope {
	seq := rapid.Int64Range(1, MaxSafeInteger).Draw(t, "seq")
	return Envelope{
		V:          Version,
		CommandID:  drawUUID(t, "command_id"),
		DeviceID:   drawUUID(t, "device_id"),
		Operation:  Operation(rapid.SampledFrom([]string{"changeset.apply", "changeset.revert", "scan.full", "validate.k8s"}).Draw(t, "operation")),
		Args:       drawArgs(t),
		ApprovalID: rapid.SampledFrom([]string{"", "8c1f7b30-52d9-4e6a-b1c4-9a3e0f5d7268"}).Draw(t, "approval_id"),
		PolicyContext: PolicyContext{
			BundleDigest: q14Digest,
			Decision:     rapid.SampledFrom([]string{"allow", "require_approval"}).Draw(t, "decision"),
		},
		Nonce:    fmt.Sprintf("%032x", rapid.Uint64().Draw(t, "nonce")),
		Seq:      seq,
		NotAfter: q14Now.Add(time.Duration(rapid.IntRange(1, 240).Draw(t, "ttl")) * time.Second).Unix(),
	}
}

// drawUUID generates a syntactically valid version-4 UUID string.
func drawUUID(t *rapid.T, label string) string {
	hi := rapid.Uint64().Draw(t, label+"_hi")
	lo := rapid.Uint64().Draw(t, label+"_lo")
	return fmt.Sprintf("%08x-%04x-4%03x-8%03x-%012x",
		uint32(hi>>32), uint16(hi>>16), uint16(hi)&0x0fff, uint16(lo>>48)&0x0fff, lo&0xffffffffffff)
}

// drawArgs generates an args object over the value shapes §7.6 allows: objects, arrays, strings,
// integers and booleans, and explicitly no floats.
func drawArgs(t *rapid.T) json.RawMessage {
	members := map[string]any{}
	count := rapid.IntRange(0, 4).Draw(t, "arg_count")
	for i := 0; i < count; i++ {
		key := rapid.SampledFrom([]string{
			"root", "path", "empty", "flag", "count", "nested", "list",
			// Non-ASCII and surrogate-pair keys, because UTF-16 code-unit ordering is where the
			// two runtimes disagreed while the corpus was being built.
			"é", "日本", "\U0001F600", "\uFFFD",
		}).Draw(t, fmt.Sprintf("key_%d", i))
		switch rapid.IntRange(0, 5).Draw(t, fmt.Sprintf("kind_%d", i)) {
		case 0:
			members[key] = rapid.String().Draw(t, fmt.Sprintf("str_%d", i))
		case 1:
			members[key] = rapid.Int64Range(-MaxSafeInteger, MaxSafeInteger).Draw(t, fmt.Sprintf("int_%d", i))
		case 2:
			members[key] = rapid.Bool().Draw(t, fmt.Sprintf("bool_%d", i))
		case 3:
			members[key] = map[string]any{}
		case 4:
			members[key] = []any{}
		case 5:
			members[key] = nil
		}
	}
	encoded, err := json.Marshal(members)
	if err != nil {
		t.Fatalf("marshalling generated args: %v", err)
	}
	return encoded
}

func q14Verifier(t *rapid.T, deviceID string) *Verifier {
	keys := NewStaticKeySource()
	keys.Set(deviceID, q14Key)
	guard, err := NewMemoryReplayGuard(300*time.Second, 4096)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	verifier, err := NewVerifier(keys, guard, NewStaticBundleDigest(q14Digest),
		WithClock(func() time.Time { return q14Now }))
	if err != nil {
		t.Fatalf("NewVerifier: %v", err)
	}
	return verifier
}

// TestProperty_Q14_CanonicalisationIsDeterministicAndOrderIndependent is the half of "byte
// identical" that can be asserted inside one runtime: the same LOGICAL envelope canonicalises to
// the same bytes however it was written down.
func TestProperty_Q14_CanonicalisationIsDeterministicAndOrderIndependent(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		env := drawEnvelope(t)

		first, err := CanonicalBytes(env)
		if err != nil {
			t.Fatalf("CanonicalBytes: %v", err)
		}
		second, err := CanonicalBytes(env)
		if err != nil {
			t.Fatalf("CanonicalBytes again: %v", err)
		}
		if !bytes.Equal(first, second) {
			t.Fatalf("canonicalisation is not deterministic:\n%s\n%s", first, second)
		}

		// The same logical envelope with its args re-serialised in a different member order.
		// JCS sorts, so the bytes must not move. This is the clause that would catch a
		// canonicaliser that passed the caller's ordering through.
		var members map[string]any
		if err := json.Unmarshal(env.Args, &members); err != nil {
			t.Fatalf("unmarshalling args: %v", err)
		}
		reordered := env
		reordered.Args = reserialiseReversed(t, members)
		third, err := CanonicalBytes(reordered)
		if err != nil {
			t.Fatalf("CanonicalBytes reordered: %v", err)
		}
		if !bytes.Equal(first, third) {
			t.Fatalf("member order changed the canonical bytes:\n%s\n%s", first, third)
		}

		// JCS shape, asserted rather than assumed: no insignificant whitespace, and top-level
		// members in non-decreasing UTF-16 code-unit order.
		if bytes.ContainsAny(first, "\n\t") || bytes.Contains(first, []byte(`", "`)) {
			t.Fatalf("canonical bytes carry insignificant whitespace: %s", first)
		}
		if !topLevelKeysSorted(string(first)) {
			t.Fatalf("canonical top-level members are not sorted: %s", first)
		}
		// `signature` is never part of the signed bytes, whatever the input carried.
		signedShape := env
		signedShape.Signature = "AAAA"
		fourth, err := CanonicalBytes(signedShape)
		if err != nil {
			t.Fatalf("CanonicalBytes with a signature: %v", err)
		}
		if !bytes.Equal(first, fourth) {
			t.Fatal("the signature member entered the canonical bytes")
		}
	})
}

// TestProperty_Q14_VerificationAcceptsExactlyTheCorrectlySignedEnvelope is the exactness clause:
// acceptance in one direction, rejection of every single-byte mutation in the other.
func TestProperty_Q14_VerificationAcceptsExactlyTheCorrectlySignedEnvelope(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		env := drawEnvelope(t)
		signature, err := Sign(DomainPrefix, env, q14Key)
		if err != nil {
			t.Fatalf("Sign: %v", err)
		}
		signed := env
		signed.Signature = signature
		raw, err := json.Marshal(signed)
		if err != nil {
			t.Fatalf("Marshal: %v", err)
		}

		if _, err := q14Verifier(t, env.DeviceID).Verify(context.Background(), raw); err != nil {
			t.Fatalf("a correctly signed envelope was rejected: %v", err)
		}

		// The domain-separation prefix is load-bearing: a signature over the same bytes without
		// it, or under the approval prefix, must not verify. This is the clause Q-14's negative
		// control removes.
		for _, prefix := range []string{"", ApprovalDomainPrefix} {
			otherSignature, err := Sign(prefix, env, q14Key)
			if err != nil {
				t.Fatalf("Sign(%q): %v", prefix, err)
			}
			if otherSignature == signature {
				t.Fatalf("prefix %q produced the same signature as the envelope prefix; the "+
					"domain separation is not in the signing input", prefix)
			}
			other := env
			other.Signature = otherSignature
			otherRaw, err := json.Marshal(other)
			if err != nil {
				t.Fatalf("Marshal: %v", err)
			}
			if _, err := q14Verifier(t, env.DeviceID).Verify(context.Background(), otherRaw); err == nil {
				t.Fatalf("a signature computed under prefix %q verified as an envelope", prefix)
			}
		}

		// Every single-byte mutation of the signature is rejected. The index and the replacement
		// are generated, so this quantifies over the mutation rather than testing one.
		decoded, err := DecodeSignature(signature)
		if err != nil {
			t.Fatalf("DecodeSignature: %v", err)
		}
		index := rapid.IntRange(0, len(decoded)-1).Draw(t, "mac_byte")
		delta := byte(rapid.IntRange(1, 255).Draw(t, "mac_delta"))
		mutated := make([]byte, len(decoded))
		copy(mutated, decoded)
		mutated[index] ^= delta
		flipped := env
		flipped.Signature = EncodeSignature(mutated)
		flippedRaw, err := json.Marshal(flipped)
		if err != nil {
			t.Fatalf("Marshal: %v", err)
		}
		if _, err := q14Verifier(t, env.DeviceID).Verify(context.Background(), flippedRaw); err == nil {
			t.Fatalf("a MAC with byte %d flipped by %d verified", index, delta)
		}
	})
}

// TestProperty_Q14_ASingleByteChangeToTheBodyIsRejected is the other half of "single-byte
// mutation": the signature is left correct for the ORIGINAL body and one byte of the body moves.
func TestProperty_Q14_ASingleByteChangeToTheBodyIsRejected(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		env := drawEnvelope(t)
		signature, err := Sign(DomainPrefix, env, q14Key)
		if err != nil {
			t.Fatalf("Sign: %v", err)
		}

		// Mutate a member the schema will still accept, so the refusal comes from the signature
		// rather than from parsing: a generated character appended to one string member.
		suffix := string(rune(rapid.IntRange('a', 'z').Draw(t, "suffix")))
		mutated := env
		switch rapid.IntRange(0, 3).Draw(t, "member") {
		case 0:
			mutated.CommandID += suffix
		case 1:
			mutated.Operation = Operation(string(env.Operation) + suffix)
		case 2:
			mutated.PolicyContext.Decision += suffix
		case 3:
			mutated.Seq = env.Seq%MaxSafeInteger + 1
		}
		if mutated.CommandID == env.CommandID &&
			mutated.Operation == env.Operation &&
			mutated.PolicyContext == env.PolicyContext &&
			mutated.Seq == env.Seq {
			// The draw happened to be a no-op (a seq of exactly MaxSafeInteger wrapping to
			// itself). Nothing changed, so there is nothing to reject.
			return
		}
		mutated.Signature = signature
		raw, err := json.Marshal(mutated)
		if err != nil {
			t.Fatalf("Marshal: %v", err)
		}
		if _, err := q14Verifier(t, env.DeviceID).Verify(context.Background(), raw); err == nil {
			t.Fatal("a mutated body verified under the original signature")
		}
	})
}

// TestProperty_Q14_TheSigningInputCarriesThePrefixAndSeparator asserts the shape of the signed
// bytes directly, which is the clause a reader can check against §7.6 step 4 without running
// anything.
func TestProperty_Q14_TheSigningInputCarriesThePrefixAndSeparator(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		env := drawEnvelope(t)
		input, err := SigningInput(DomainPrefix, env)
		if err != nil {
			t.Fatalf("SigningInput: %v", err)
		}
		want := DomainPrefix + "\x00"
		if !strings.HasPrefix(string(input), want) {
			t.Fatalf("the signing input does not begin with the prefix and its NUL separator: %q",
				input[:min(len(input), 40)])
		}
		if len(DomainPrefix) == 0 {
			t.Fatal("DomainPrefix is empty; there is no domain separation at all")
		}
		canonical, err := CanonicalBytes(env)
		if err != nil {
			t.Fatalf("CanonicalBytes: %v", err)
		}
		if !bytes.Equal(input[len(want):], canonical) {
			t.Fatal("the signing input's body is not the canonical bytes")
		}
	})
}

// reserialiseReversed writes the members in reverse-sorted order, so the input to the
// canonicaliser is ordered differently from its output.
func reserialiseReversed(t *rapid.T, members map[string]any) json.RawMessage {
	keys := make([]string, 0, len(members))
	for key := range members {
		keys = append(keys, key)
	}
	// A deliberate reverse of Go's map iteration into a fixed descending order.
	for i := 0; i < len(keys); i++ {
		for j := i + 1; j < len(keys); j++ {
			if keys[j] > keys[i] {
				keys[i], keys[j] = keys[j], keys[i]
			}
		}
	}
	var out bytes.Buffer
	out.WriteByte('{')
	for i, key := range keys {
		if i > 0 {
			out.WriteByte(',')
		}
		encodedKey, err := json.Marshal(key)
		if err != nil {
			t.Fatalf("Marshal key: %v", err)
		}
		encodedValue, err := json.Marshal(members[key])
		if err != nil {
			t.Fatalf("Marshal value: %v", err)
		}
		out.Write(encodedKey)
		out.WriteByte(':')
		out.Write(encodedValue)
	}
	out.WriteByte('}')
	return out.Bytes()
}

// topLevelKeysSorted reports whether the top-level member names of a canonical object are in
// non-decreasing UTF-16 code-unit order.
func topLevelKeysSorted(canonical string) bool {
	var members map[string]json.RawMessage
	if err := json.Unmarshal([]byte(canonical), &members); err != nil {
		return false
	}
	names := make([]string, 0, len(members))
	// Read the names in the order they appear in the bytes rather than from the map.
	depth := 0
	inString := false
	escaped := false
	expectKey := true
	current := strings.Builder{}
	for i := 0; i < len(canonical); i++ {
		char := canonical[i]
		switch {
		case escaped:
			escaped = false
			if inString && depth == 1 && expectKey {
				current.WriteByte(char)
			}
		case char == '\\' && inString:
			escaped = true
			if inString && depth == 1 && expectKey {
				current.WriteByte(char)
			}
		case char == '"':
			if inString && depth == 1 && expectKey {
				names = append(names, current.String())
				current.Reset()
				expectKey = false
			}
			inString = !inString
		case inString:
			if depth == 1 && expectKey {
				current.WriteByte(char)
			}
		case char == '{' || char == '[':
			depth++
		case char == '}' || char == ']':
			depth--
		case char == ',' && depth == 1:
			expectKey = true
		}
	}
	for i := 1; i < len(names); i++ {
		if lessUTF16(names[i], names[i-1]) {
			return false
		}
	}
	return len(names) > 0
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
