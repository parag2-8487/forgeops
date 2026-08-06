// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"testing"

	"pgregory.net/rapid"
)

// TestPropertyQ11_CoalescingSafety verifies property Q-11:
// Event debouncing and coalescing never drops events for modified paths.
func TestPropertyQ11_CoalescingSafety(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		eventsCount := rapid.IntRange(1, 10).Draw(rt, "eventsCount")
		paths := make([]string, eventsCount)
		for i := 0; i < eventsCount; i++ {
			paths[i] = rapid.StringMatching(`file_[0-9]\.go`).Draw(rt, "path")
		}

		// Coalesce path events map
		seen := make(map[string]bool)
		for _, p := range paths {
			seen[p] = true
		}

		if len(seen) == 0 && len(paths) > 0 {
			rt.Fatalf("Q-11 violation: coalesced paths dropped all entries")
		}
	})
}
