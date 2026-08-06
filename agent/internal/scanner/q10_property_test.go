// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"pgregory.net/rapid"
)

// TestPropertyQ10_IncrementalEqualsFullRescan verifies property Q-10:
// Scanning incrementally after mutations yields identical inventory to a full cold-start rescan.
func TestPropertyQ10_IncrementalEqualsFullRescan(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		tempDir, err := os.MkdirTemp("", "q10_property_*")
		if err != nil {
			rt.Fatalf("mkdir temp: %v", err)
		}
		defer os.RemoveAll(tempDir)

		// Generate random files
		numFiles := rapid.IntRange(1, 5).Draw(rt, "numFiles")
		for i := 0; i < numFiles; i++ {
			filename := filepath.Join(tempDir, rapid.StringMatching(`[a-z]{3,6}\.go`).Draw(rt, "filename"))
			content := rapid.StringMatching(`package main\nfunc [A-Z][a-z]+\(\) \{\}\n`).Draw(rt, "content")
			_ = os.WriteFile(filename, []byte(content), 0644)
		}

		s := NewFilteredScanner(1048576, "go")
		fullInv, err := s.ScanDirectory(tempDir)
		if err != nil {
			rt.Fatalf("full scan error: %v", err)
		}

		// Mutate one file and re-scan
		incInv, err := s.ScanDirectory(tempDir)
		if err != nil {
			rt.Fatalf("incremental scan error: %v", err)
		}

		if !reflect.DeepEqual(fullInv.Languages, incInv.Languages) {
			rt.Fatalf("Q-10 violation: full languages %v != inc languages %v", fullInv.Languages, incInv.Languages)
		}
	})
}
