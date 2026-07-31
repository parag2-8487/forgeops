// SPDX-License-Identifier: Apache-2.0

package envelope

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// The test key is synthetic and self-labelling, per .kiro/steering/secret-safety.md. It
// is not shaped like any real provider credential and says so in its own bytes.
var testKey = []byte("test-only-not-a-real-secret-envelope-key")

const testDigest = "sha256:0000000000000000000000000000000000000000000000000000000000000001"

func sampleEnvelope() Envelope {
	return Envelope{
		V:          Version,
		CommandID:  "cmd-0001",
		DeviceID:   "dev-0001",
		Operation:  Operation("files.apply"),
		Args:       json.RawMessage(`{"root":"/tmp/p","count":2}`),
		ApprovalID: "apr-0001",
		PolicyContext: PolicyContext{
			BundleDigest: testDigest,
			Decision:     "allow",
		},
		Nonce:    "0123456789abcdef0123456789abcdef",
		Seq:      7,
		NotAfter: 1900000000,
	}
}

func signed(t *testing.T, e Envelope) []byte {
	t.Helper()
	signature, err := Sign(DomainPrefix, e, testKey)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	e.Signature = signature
	raw, err := json.Marshal(e)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	return raw
}

func newTestVerifier(t *testing.T, options ...VerifierOption) *Verifier {
	t.Helper()
	keys := NewStaticKeySource()
	keys.Set("dev-0001", testKey)
	guard, err := NewMemoryReplayGuard(300*time.Second, 1024)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	fixedNow := time.Unix(1899999900, 0).UTC()
	base := []VerifierOption{WithClock(func() time.Time { return fixedNow })}
	verifier, err := NewVerifier(keys, guard, NewStaticBundleDigest(testDigest), append(base, options...)...)
	if err != nil {
		t.Fatalf("NewVerifier: %v", err)
	}
	return verifier
}

// ── Canonicalisation ───────────────────────────────────────────────────────────

func TestCanonicalBytes_ExcludesSignatureAndSortsMembers(t *testing.T) {
	e := sampleEnvelope()
	withoutSignature, err := CanonicalBytes(e)
	if err != nil {
		t.Fatalf("CanonicalBytes: %v", err)
	}
	e.Signature = "AAAA"
	withSignature, err := CanonicalBytes(e)
	if err != nil {
		t.Fatalf("CanonicalBytes: %v", err)
	}
	if string(withoutSignature) != string(withSignature) {
		t.Fatalf("signature changed the canonical bytes; it must be removed before canonicalisation\n%s\n%s",
			withoutSignature, withSignature)
	}
	got := string(withoutSignature)
	if strings.Contains(got, "signature") {
		t.Fatalf("canonical bytes carry a signature member: %s", got)
	}
	// JCS sorts members by UTF-16 code unit. "nonce" precedes "not_after" because 'n'
	// (0x6E) precedes 't' (0x74) at the third code unit.
	wantOrder := []string{"approval_id", "args", "command_id", "device_id", "nonce",
		"not_after", "operation", "policy_context", "seq", "v"}
	position := -1
	for _, member := range wantOrder {
		at := strings.Index(got, `"`+member+`"`)
		if at <= position {
			t.Fatalf("member %q is out of JCS order in %s", member, got)
		}
		position = at
	}
}

func TestCanonicalBytes_HasNoInsignificantWhitespace(t *testing.T) {
	canonical, err := CanonicalBytes(sampleEnvelope())
	if err != nil {
		t.Fatalf("CanonicalBytes: %v", err)
	}
	for _, forbidden := range []string{" ", "\n", "\t", "\r"} {
		// A space inside a string value would be legitimate; this envelope has none, so
		// any occurrence is insignificant whitespace.
		if strings.Contains(string(canonical), forbidden) {
			t.Fatalf("canonical bytes contain insignificant whitespace %q: %s", forbidden, canonical)
		}
	}
}

func TestCanonicalBytes_RejectsAFloatAndNamesThePath(t *testing.T) {
	e := sampleEnvelope()
	e.Args = json.RawMessage(`{"outer":{"score":1.5}}`)
	_, err := CanonicalBytes(e)
	if err == nil {
		t.Fatal("a float in args must be refused (design §7.6)")
	}
	if !strings.Contains(err.Error(), "args.outer.score") {
		t.Fatalf("the error must name the path to the float, got: %v", err)
	}
}

func TestCanonicalBytes_RejectsExponentNotation(t *testing.T) {
	e := sampleEnvelope()
	e.Args = json.RawMessage(`{"n":1e3}`)
	if _, err := CanonicalBytes(e); err == nil {
		t.Fatal("1e3 is a float in JSON terms and must be refused even though it is integral")
	}
}

func TestCanonicalBytes_AbsentArgsIsAnEmptyObject(t *testing.T) {
	a := sampleEnvelope()
	a.Args = nil
	b := sampleEnvelope()
	b.Args = json.RawMessage(`{}`)
	first, err := CanonicalBytes(a)
	if err != nil {
		t.Fatalf("CanonicalBytes(nil args): %v", err)
	}
	second, err := CanonicalBytes(b)
	if err != nil {
		t.Fatalf("CanonicalBytes(empty args): %v", err)
	}
	if string(first) != string(second) {
		t.Fatalf("absent and empty args must canonicalise identically:\n%s\n%s", first, second)
	}
}

// TestJCS_SortsByUTF16CodeUnitNotByCodePoint is the one canonicalisation subtlety that
// silently produces cross-runtime divergence, so it is asserted directly.
//
// RFC 8785 §3.2.3 sorts object members by UTF-16 code unit. Go's byte order equals
// code-point order, which agrees inside the BMP and disagrees above it: U+1F600 encodes
// as the surrogate pair D83D DE00, so it sorts BELOW U+E000 in UTF-16 and ABOVE it in
// UTF-8.
func TestJCS_SortsByUTF16CodeUnitNotByCodePoint(t *testing.T) {
	body := map[string]any{
		"\U0001F600": json.Number("1"), // U+1F600, surrogate pair D83D DE00
		"\uE000":     json.Number("2"), // private use, single code unit E000
	}
	out, err := jcs(body)
	if err != nil {
		t.Fatalf("jcs: %v", err)
	}
	emoji := strings.Index(string(out), "\U0001F600")
	private := strings.Index(string(out), "\uE000")
	if emoji == -1 || private == -1 {
		t.Fatalf("both keys must appear: %s", out)
	}
	if emoji > private {
		t.Fatalf("U+1F600 must sort before U+E000 in UTF-16 code-unit order; got %s", out)
	}
}

func TestJCS_MinimalEscapingLeavesNonAsciiAlone(t *testing.T) {
	out, err := jcs(map[string]any{"k": "café \u2028 <&>"})
	if err != nil {
		t.Fatalf("jcs: %v", err)
	}
	got := string(out)
	// encoding/json would emit \u2028 and \u003c; RFC 8785 does not.
	for _, forbidden := range []string{`\u2028`, `\u003c`, `\u0026`} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("JCS must not escape %s: %s", forbidden, got)
		}
	}
	if !strings.Contains(got, "café") {
		t.Fatalf("non-ASCII must be written literally: %s", got)
	}
}

func TestSigningInput_CarriesTheDomainPrefixAndSeparator(t *testing.T) {
	input, err := SigningInput(DomainPrefix, sampleEnvelope())
	if err != nil {
		t.Fatalf("SigningInput: %v", err)
	}
	if !strings.HasPrefix(string(input), DomainPrefix+"\x00") {
		t.Fatalf("signing input must begin with the prefix and a NUL: %q", input[:40])
	}
}

func TestSigningInput_ApprovalPrefixProducesDifferentBytes(t *testing.T) {
	e := sampleEnvelope()
	command, err := SigningInput(DomainPrefix, e)
	if err != nil {
		t.Fatalf("SigningInput: %v", err)
	}
	approval, err := SigningInput(ApprovalDomainPrefix, e)
	if err != nil {
		t.Fatalf("SigningInput: %v", err)
	}
	if string(command) == string(approval) {
		t.Fatal("domain separation is what stops a command signature being replayed as an approval")
	}
}

// ── The Verified capability ────────────────────────────────────────────────────

func TestVerify_AcceptsACorrectlySignedEnvelope(t *testing.T) {
	verifier := newTestVerifier(t)
	verified, err := verifier.Verify(context.Background(), signed(t, sampleEnvelope()))
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if verified.Operation() != Operation("files.apply") {
		t.Fatalf("Operation() = %q", verified.Operation())
	}
	if verified.ApprovalID() != "apr-0001" {
		t.Fatalf("ApprovalID() = %q", verified.ApprovalID())
	}
	if verified.Seq() != 7 {
		t.Fatalf("Seq() = %d", verified.Seq())
	}
	if verified.Digest() == "" {
		t.Fatal("Digest() must name the bytes that verified")
	}
	if verified.PolicyContext().BundleDigest != testDigest {
		t.Fatalf("PolicyContext() = %+v", verified.PolicyContext())
	}
}

// TestVerify_RejectsEverySingleByteMutationOfTheMAC is Q-14's clause stated as an example
// test, so the behaviour is guarded before the property leaf lands.
//
// It mutates the DECODED MAC and re-encodes, not the encoded characters. That distinction
// matters and was found by writing this test: a 32-byte MAC encodes to 43 base64
// characters carrying 258 bits, so the last character has four bits that decode to
// nothing. Mutating the encoded form at the final position can leave the decoded bytes
// unchanged. `DecodeSignature` now refuses non-canonical trailing bits, and the last
// subtest below asserts that; this one asserts what the clause is actually about.
func TestVerify_RejectsEverySingleByteMutationOfTheMAC(t *testing.T) {
	e := sampleEnvelope()
	signature, err := Sign(DomainPrefix, e, testKey)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	mac, err := DecodeSignature(signature)
	if err != nil {
		t.Fatalf("DecodeSignature: %v", err)
	}
	if len(mac) != 32 {
		t.Fatalf("HMAC-SHA256 is 32 bytes, got %d", len(mac))
	}
	for index := range mac {
		mutated := make([]byte, len(mac))
		copy(mutated, mac)
		mutated[index] ^= 0x01
		e.Signature = EncodeSignature(mutated)
		raw, err := json.Marshal(e)
		if err != nil {
			t.Fatalf("Marshal: %v", err)
		}
		verifier := newTestVerifier(t)
		if _, err := verifier.Verify(context.Background(), raw); err == nil {
			t.Fatalf("flipping bit 0 of MAC byte %d was accepted", index)
		}
	}
}

// TestDecodeSignature_RefusesNonCanonicalTrailingBits closes the four-spellings hole above.
func TestDecodeSignature_RefusesNonCanonicalTrailingBits(t *testing.T) {
	signature, err := Sign(DomainPrefix, sampleEnvelope(), testKey)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if _, err := DecodeSignature(signature); err != nil {
		t.Fatalf("the canonical form must decode: %v", err)
	}
	alphabet := "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
	last := len(signature) - 1
	rejected := 0
	for i := 0; i < len(alphabet); i++ {
		candidate := signature[:last] + string(alphabet[i])
		if candidate == signature {
			continue
		}
		if _, err := DecodeSignature(candidate); err != nil {
			rejected++
		}
	}
	if rejected == 0 {
		t.Fatal("no alternative final character was rejected; a MAC would have several spellings")
	}
	// Every alternative final character either decodes to different bytes (and so fails
	// verification) or is refused here. Both outcomes are correct; what must not happen is
	// silently decoding to the SAME bytes, which is what the round-trip check prevents.
	for i := 0; i < len(alphabet); i++ {
		candidate := signature[:last] + string(alphabet[i])
		if candidate == signature {
			continue
		}
		decoded, err := DecodeSignature(candidate)
		if err != nil {
			continue
		}
		original, err := DecodeSignature(signature)
		if err != nil {
			t.Fatalf("DecodeSignature: %v", err)
		}
		if string(decoded) == string(original) {
			t.Fatalf("final character %q decodes to the same MAC as %q", candidate[last], signature[last])
		}
	}
}

func TestVerify_RejectsAMutatedBodyUnderAValidSignature(t *testing.T) {
	e := sampleEnvelope()
	signature, err := Sign(DomainPrefix, e, testKey)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	e.Signature = signature
	e.Operation = Operation("files.revert") // signed as files.apply
	raw, err := json.Marshal(e)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	verifier := newTestVerifier(t)
	if _, err := verifier.Verify(context.Background(), raw); err == nil {
		t.Fatal("changing the operation after signing must be rejected")
	}
}

// TestVerify_RejectsAnUnknownMember is the check that stops an attacker appending content
// to a signed envelope: the canonical form covers a closed member set, so an unknown
// member would be excluded from the MAC and accepted as authentic.
func TestVerify_RejectsAnUnknownMember(t *testing.T) {
	raw := signed(t, sampleEnvelope())
	var asMap map[string]any
	if err := json.Unmarshal(raw, &asMap); err != nil {
		t.Fatalf("Unmarshal: %v", err)
	}
	asMap["injected"] = "anything at all"
	tampered, err := json.Marshal(asMap)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	verifier := newTestVerifier(t)
	if _, err := verifier.Verify(context.Background(), tampered); err == nil {
		t.Fatal("an unknown member must be refused, or it rides along outside the MAC")
	}
}

func TestVerify_RejectsAnExpiredEnvelope(t *testing.T) {
	e := sampleEnvelope()
	e.NotAfter = 1000
	verifier := newTestVerifier(t)
	_, err := verifier.Verify(context.Background(), signed(t, e))
	if err == nil || Code(err) != "envelope-expired" {
		t.Fatalf("expected envelope-expired, got %v (code %q)", err, Code(err))
	}
}

func TestVerify_RejectsAnEnvelopeTooFarInTheFuture(t *testing.T) {
	e := sampleEnvelope()
	e.NotAfter = 1899999900 + 3600 // an hour out, maxAge is 300s
	verifier := newTestVerifier(t)
	_, err := verifier.Verify(context.Background(), signed(t, e))
	if err == nil || Code(err) != "envelope-expired" {
		t.Fatalf("expected the max-age bound to reject, got %v (code %q)", err, Code(err))
	}
}

func TestVerify_RejectsAReplayedNonce(t *testing.T) {
	verifier := newTestVerifier(t)
	raw := signed(t, sampleEnvelope())
	if _, err := verifier.Verify(context.Background(), raw); err != nil {
		t.Fatalf("first Verify: %v", err)
	}
	// The same envelope again: seq is no longer greater, so ordering rejects first. Use a
	// higher seq with the same nonce to reach the uniqueness check.
	e := sampleEnvelope()
	e.Seq = 8
	_, err := verifier.Verify(context.Background(), signed(t, e))
	if err == nil || Code(err) != "envelope-replayed" {
		t.Fatalf("expected envelope-replayed for a reused nonce, got %v (code %q)", err, Code(err))
	}
}

func TestVerify_RejectsANonIncreasingSeq(t *testing.T) {
	verifier := newTestVerifier(t)
	first := sampleEnvelope()
	first.Seq = 9
	if _, err := verifier.Verify(context.Background(), signed(t, first)); err != nil {
		t.Fatalf("first Verify: %v", err)
	}
	second := sampleEnvelope()
	second.Seq = 9
	second.Nonce = "ffffffffffffffffffffffffffffffff"
	_, err := verifier.Verify(context.Background(), signed(t, second))
	if err == nil || Code(err) != "envelope-replayed" {
		t.Fatalf("expected envelope-replayed for an equal seq, got %v (code %q)", err, Code(err))
	}
}

// TestVerify_ABadSignatureDoesNotAdvanceSeq is §10.4's ordering note as an assertion.
//
// If ordering ran before the signature check, an unauthenticated attacker could push a
// device's high-water mark to int64 max and lock the real backend out permanently — a
// denial of service through a check meant to be a defence. Q-15's negative control is
// exactly this inversion.
func TestVerify_ABadSignatureDoesNotAdvanceSeq(t *testing.T) {
	keys := NewStaticKeySource()
	keys.Set("dev-0001", testKey)
	guard, err := NewMemoryReplayGuard(300*time.Second, 1024)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	fixedNow := time.Unix(1899999900, 0).UTC()
	verifier, err := NewVerifier(keys, guard, NewStaticBundleDigest(testDigest),
		WithClock(func() time.Time { return fixedNow }))
	if err != nil {
		t.Fatalf("NewVerifier: %v", err)
	}

	forged := sampleEnvelope()
	forged.Seq = 999999
	forged.Signature = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
	raw, err := json.Marshal(forged)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	if _, err := verifier.Verify(context.Background(), raw); err == nil {
		t.Fatal("a forged signature must be rejected")
	}
	if got := guard.LastSeq("dev-0001"); got != 0 {
		t.Fatalf("a rejected envelope advanced the seq high-water mark to %d; "+
			"the signature check must precede the ordering check (§10.4)", got)
	}
	if got := guard.NonceCount(); got != 0 {
		t.Fatalf("a rejected envelope burned %d nonce(s); the signature check must come first", got)
	}
}

func TestVerify_RejectsAStalePolicyBundleDigest(t *testing.T) {
	keys := NewStaticKeySource()
	keys.Set("dev-0001", testKey)
	guard, err := NewMemoryReplayGuard(300*time.Second, 1024)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	fixedNow := time.Unix(1899999900, 0).UTC()
	verifier, err := NewVerifier(keys, guard, NewStaticBundleDigest("sha256:something-else"),
		WithClock(func() time.Time { return fixedNow }))
	if err != nil {
		t.Fatalf("NewVerifier: %v", err)
	}
	_, err = verifier.Verify(context.Background(), signed(t, sampleEnvelope()))
	if err == nil || Code(err) != "policy-bundle-stale" {
		t.Fatalf("expected policy-bundle-stale, got %v (code %q)", err, Code(err))
	}
}

func TestVerify_RejectsAnUnknownVersion(t *testing.T) {
	e := sampleEnvelope()
	e.V = "2"
	verifier := newTestVerifier(t)
	_, err := verifier.Verify(context.Background(), signed(t, e))
	if err == nil || Code(err) != "envelope-malformed" {
		t.Fatalf("expected envelope-malformed for an unknown version, got %v", err)
	}
}

func TestVerify_RejectsAMissingRequiredMember(t *testing.T) {
	for _, blank := range []string{"command_id", "device_id", "approval_id", "nonce", "signature"} {
		raw := signed(t, sampleEnvelope())
		var asMap map[string]any
		if err := json.Unmarshal(raw, &asMap); err != nil {
			t.Fatalf("Unmarshal: %v", err)
		}
		asMap[blank] = ""
		tampered, err := json.Marshal(asMap)
		if err != nil {
			t.Fatalf("Marshal: %v", err)
		}
		verifier := newTestVerifier(t)
		if _, err := verifier.Verify(context.Background(), tampered); err == nil {
			t.Fatalf("an empty %s was accepted", blank)
		}
	}
}

// ── The Verifier refuses to be incomplete ──────────────────────────────────────

func TestNewVerifier_RefusesAnyNilCollaborator(t *testing.T) {
	keys := NewStaticKeySource()
	guard, err := NewMemoryReplayGuard(time.Second, 1)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	bundle := NewStaticBundleDigest(testDigest)

	if _, err := NewVerifier(nil, guard, bundle); err == nil {
		t.Fatal("a nil KeySource must be refused")
	}
	if _, err := NewVerifier(keys, nil, bundle); err == nil {
		t.Fatal("a nil ReplayGuard must be refused: it would skip §7.6's ordering and uniqueness silently")
	}
	if _, err := NewVerifier(keys, guard, nil); err == nil {
		t.Fatal("a nil BundleDigestSource must be refused: it would skip the Q-07 binding silently")
	}
}

// ── The replay guard's own bounds ──────────────────────────────────────────────

func TestMemoryReplayGuard_IsBoundedAndEvictsOldestFirst(t *testing.T) {
	guard, err := NewMemoryReplayGuard(time.Hour, 3)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	ctx := context.Background()
	for _, nonce := range []string{"a", "b", "c", "d"} {
		seen, err := guard.SeenNonce(ctx, "dev", nonce)
		if err != nil {
			t.Fatalf("SeenNonce(%s): %v", nonce, err)
		}
		if seen {
			t.Fatalf("nonce %s reported as seen on first use", nonce)
		}
	}
	if got := guard.NonceCount(); got != 3 {
		t.Fatalf("the guard is not bounded: holding %d with capacity 3", got)
	}
	// "a" was evicted, so it is accepted again — which is why the capacity must cover
	// maxAge in production.
	seen, err := guard.SeenNonce(ctx, "dev", "d")
	if err != nil {
		t.Fatalf("SeenNonce(d): %v", err)
	}
	if !seen {
		t.Fatal("the most recent nonce must still be remembered")
	}
}

func TestMemoryReplayGuard_ExpiresByAge(t *testing.T) {
	guard, err := NewMemoryReplayGuard(10*time.Second, 1024)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	current := time.Unix(1_700_000_000, 0).UTC()
	guard.SetClock(func() time.Time { return current })
	ctx := context.Background()
	if _, err := guard.SeenNonce(ctx, "dev", "n1"); err != nil {
		t.Fatalf("SeenNonce: %v", err)
	}
	current = current.Add(11 * time.Second)
	if got := guard.NonceCount(); got != 1 {
		t.Fatalf("eviction is lazy; expected 1 before the next call, got %d", got)
	}
	seen, err := guard.SeenNonce(ctx, "dev", "n2")
	if err != nil {
		t.Fatalf("SeenNonce: %v", err)
	}
	if seen {
		t.Fatal("n2 is new")
	}
	if got := guard.NonceCount(); got != 1 {
		t.Fatalf("the aged entry was not evicted: holding %d", got)
	}
}

func TestMemoryReplayGuard_SeqIsPerDevice(t *testing.T) {
	guard, err := NewMemoryReplayGuard(time.Hour, 16)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	ctx := context.Background()
	ok, err := guard.AdvanceSeq(ctx, "a", 5)
	if err != nil || !ok {
		t.Fatalf("AdvanceSeq(a,5) = %v, %v", ok, err)
	}
	// Device b starts fresh; a high mark on a must not lock b out.
	ok, err = guard.AdvanceSeq(ctx, "b", 1)
	if err != nil || !ok {
		t.Fatalf("AdvanceSeq(b,1) = %v, %v", ok, err)
	}
	ok, err = guard.AdvanceSeq(ctx, "a", 5)
	if err != nil {
		t.Fatalf("AdvanceSeq: %v", err)
	}
	if ok {
		t.Fatal("an equal seq must be refused: §7.6 says strictly monotonic")
	}
}

func TestStaticKeySource_CopiesTheKey(t *testing.T) {
	source := NewStaticKeySource()
	mutable := []byte("test-only-not-a-real-secret-aaaa")
	source.Set("dev", mutable)
	mutable[0] = 'X'
	got, err := source.EnvelopeKey(context.Background(), "dev")
	if err != nil {
		t.Fatalf("EnvelopeKey: %v", err)
	}
	if got[0] == 'X' {
		t.Fatal("the key source must copy, or a caller's later write changes every signature")
	}
}

func TestStaticBundleDigest_EmptyIsAnErrorNotAMatch(t *testing.T) {
	if _, err := NewStaticBundleDigest("").BundleDigest(context.Background()); err == nil {
		t.Fatal("no bundle loaded must be an error, never an empty string a caller might compare")
	}
}
