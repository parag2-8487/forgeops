// SPDX-License-Identifier: Apache-2.0
package grammars

import (
	"testing"
)

func TestLoadGrammars(t *testing.T) {
	data, lock, err := LoadGrammars()
	if err != nil {
		t.Fatalf("LoadGrammars failed: %v", err)
	}

	if len(data) != 12 {
		t.Errorf("expected 12 grammars, got %d", len(data))
	}

	if len(lock.Grammars) != 12 {
		t.Errorf("expected 12 entries in lockfile, got %d", len(lock.Grammars))
	}
}
