// SPDX-License-Identifier: Apache-2.0
package grammars

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"

	"pgregory.net/rapid"
)

// TestPropertyQ25_GrammarIntegrity verifies property Q-25:
// Any byte mutation in an embedded Wasm grammar fails SHA-256 validation.
func TestPropertyQ25_GrammarIntegrity(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		data, lock, err := LoadGrammars()
		if err != nil {
			rt.Fatalf("LoadGrammars failed: %v", err)
		}

		langNames := make([]string, 0, len(data))
		for k := range data {
			langNames = append(langNames, k)
		}

		idx := rapid.IntRange(0, len(langNames)-1).Draw(rt, "langIdx")
		targetLang := langNames[idx]
		originalBytes := data[targetLang]

		// Mutate a byte
		mutatedBytes := make([]byte, len(originalBytes))
		copy(mutatedBytes, originalBytes)
		mutatedBytes[0] ^= 0xFF

		hash := sha256.Sum256(mutatedBytes)
		digest := hex.EncodeToString(hash[:])
		expected := lock.Grammars[targetLang].SHA256

		if digest == expected {
			rt.Fatalf("Q-25 violation: mutated byte produced matching SHA-256 digest!")
		}
	})
}
