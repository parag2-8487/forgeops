// SPDX-License-Identifier: Apache-2.0
package grammars

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"

	"pgregory.net/rapid"
)

// Property Q-25 (design Appendix B; tasks.md leaf 11.13).
//
//	∀ byte mutations in an embedded Wasm grammar: SHA-256 validation fails.
//
// # WHAT THIS REPLACES
//
// The previous version called `LoadGrammars()`, copied one grammar's bytes, flipped byte 0,
// hashed the copy ITSELF, and asserted the result differed from the locked digest. Every step
// after `LoadGrammars` was the test doing the work: it asserted that SHA-256 is collision
// resistant, which is true of the hash function and says nothing about ForgeOps. The production
// check -- the `digest != meta.SHA256` comparison -- was never invoked with a mismatching input,
// so Appendix B's control ("skip digest verification when the blob loads successfully") could
// delete it and the test would still pass.
//
// `VerifyGrammar` was extracted from `LoadGrammars` in the same commit to make that comparison
// reachable. The embedded `.wasm` files cannot be modified at runtime, which is the whole point
// of `go:embed`, so without a seam there is no way for any test to watch the check reject
// something.
//
// # THE TWO HALVES
//
// The positive half matters as much as the negative one. If `VerifyGrammar` were mutated to
// reject everything, a test that only checked "tampered blobs are refused" would still pass while
// the agent refused to start. So the committed tree is asserted to VERIFY first.
func TestPropertyQ25_AnyByteMutationFailsVerification(t *testing.T) {
	data, lock, err := LoadGrammars()
	if err != nil {
		t.Fatalf("the committed tree must load and verify: %v", err)
	}
	if len(data) == 0 {
		t.Fatal("no grammars were loaded, so every assertion below would be vacuous")
	}

	names := make([]string, 0, len(data))
	for name := range data {
		names = append(names, name)
	}

	// ── the positive half: the real bytes verify ──────────────────────────────
	for _, name := range names {
		if err := VerifyGrammar(name+".wasm", data[name], lock.Grammars[name]); err != nil {
			t.Fatalf("Q-25 violation: the committed grammar %q does not verify: %v", name, err)
		}
	}

	// ── the negative half: any single-byte change is refused ──────────────────
	rapid.Check(t, func(rt *rapid.T) {
		name := rapid.SampledFrom(names).Draw(rt, "grammar")
		original := data[name]
		if len(original) == 0 {
			rt.Skip("empty grammar blob")
		}

		index := rapid.IntRange(0, len(original)-1).Draw(rt, "byteIndex")
		delta := byte(rapid.IntRange(1, 255).Draw(rt, "delta"))

		tampered := make([]byte, len(original))
		copy(tampered, original)
		tampered[index] ^= delta // delta >= 1, so this always changes the byte

		// Sanity: the tampered blob really is different. Without this the assertion below
		// could pass for a mutation that was not applied.
		if hex.EncodeToString(sha256With(tampered)) == hex.EncodeToString(sha256With(original)) {
			rt.Fatalf("the tampered blob hashed identically to the original; the mutation did not apply")
		}

		err := VerifyGrammar(name+".wasm", tampered, lock.Grammars[name])
		if err == nil {
			rt.Fatalf(
				"Q-25 violation: VerifyGrammar ACCEPTED a grammar with byte %d altered by %#x. "+
					"A tampered Wasm grammar would be parsed by the agent as if it were the "+
					"vendored one",
				index, delta,
			)
		}
	})
}

func sha256With(data []byte) []byte {
	sum := sha256.Sum256(data)
	return sum[:]
}
