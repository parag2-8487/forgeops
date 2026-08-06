// SPDX-License-Identifier: Apache-2.0
package grammars

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
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
	Version   string                 `json:"version"`
	Grammars  map[string]GrammarMeta `json:"grammars"`
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

		hash := sha256.Sum256(data)
		digest := hex.EncodeToString(hash[:])
		if digest != meta.SHA256 {
			return nil, lock, fmt.Errorf("SHA-256 mismatch for %s: expected %s, got %s", filename, meta.SHA256, digest)
		}

		result[name] = data
	}

	return result, lock, nil
}
