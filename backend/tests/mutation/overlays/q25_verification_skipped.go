// SPDX-License-Identifier: Apache-2.0
package grammars

// NEGATIVE CONTROL for Q-25. Applied by `scripts/mutation-harness.py` via `go build -overlay`,
// which substitutes this file for `agent/internal/scanner/grammars/grammars.go` for the duration
// of one test run. It is never compiled into the agent.
//
// Byte-for-byte the committed grammars.go except that `VerifyGrammar` returns nil without
// comparing digests. GENERATED, so it cannot drift from its original.
//
// With this applied, TestPropertyQ25_AnyByteMutationFailsVerification must FAIL on its first
// tampered draw: VerifyGrammar accepts the altered blob and the test reports that a tampered Wasm
// grammar would be parsed as if it were the vendored one.

import (
	"embed"
	"encoding/json"
	"fmt"
)

//go:embed *.wasm
var WasmFiles embed.FS

//go:embed grammars.lock.json
var LockJSON []byte

type GrammarMeta struct {
	Name      string `json:"name"`
	Version   string `json:"version"`
	SHA256    string `json:"sha256"`
	Licence   string `json:"licence"`
	SourceURL string `json:"source_url"`
	PURL      string `json:"purl"`
}

type LockFile struct {
	Version  string                 `json:"version"`
	Grammars map[string]GrammarMeta `json:"grammars"`
}

// VerifyGrammar checks one grammar blob against its locked SHA-256 digest.
//
// Extracted from `LoadGrammars` so the verification can be exercised with a TAMPERED blob.
// The embedded `.wasm` files cannot be modified at runtime -- that is the point of `go:embed` --
// so while this check lived inline there was no way for any test to observe it rejecting
// anything, and therefore no way to tell a working check from a deleted one. Q-25's negative
// control in design Appendix B is "skip digest verification when the blob loads successfully",
// which presupposes exactly this seam.
//
// Behaviour is unchanged: same comparison, same error text, same call site.
func VerifyGrammar(filename string, data []byte, meta GrammarMeta) error {
	// THE MUTATION (Q-25 negative control, Appendix B: "skip digest verification when the blob
	// loads successfully"). The comparison is gone. The blob is still read by LoadGrammars, so
	// the stated precondition holds; only the check that the bytes are the vendored ones is
	// missing. A tampered grammar is now accepted and parsed as if it were genuine.
	_ = filename
	_ = data
	_ = meta
	return nil
}

// LoadGrammars parses grammars.lock.json and verifies every embedded .wasm file against its SHA-256 digest.
func LoadGrammars() (map[string][]byte, LockFile, error) {
	var lock LockFile
	if err := json.Unmarshal(LockJSON, &lock); err != nil {
		return nil, lock, fmt.Errorf("unmarshal grammars.lock.json: %w", err)
	}

	result := make(map[string][]byte)
	for name, meta := range lock.Grammars {
		filename := fmt.Sprintf("%s.wasm", name)
		data, err := WasmFiles.ReadFile(filename)
		if err != nil {
			return nil, lock, fmt.Errorf("read embedded grammar %s: %w", filename, err)
		}

		if err := VerifyGrammar(filename, data, meta); err != nil {
			return nil, lock, err
		}

		result[name] = data
	}

	return result, lock, nil
}
