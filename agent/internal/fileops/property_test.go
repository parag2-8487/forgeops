// SPDX-License-Identifier: Apache-2.0
package fileops

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"pgregory.net/rapid"
)

// TestProperty_P08_AtomicChangeSets tests:
// - all-new-with-backups OR exact pre-image restoration
// - root confinement
// - blocklist rejection
// - content idempotence
func TestProperty_P08_AtomicChangeSets(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		ops := New()

		// Generate a small set of valid entries with unique paths
		numEntries := rapid.IntRange(1, 5).Draw(rt, "numEntries")
		entries := make([]WriteEntry, 0, numEntries)
		seen := make(map[string]bool)
		for i := 0; i < numEntries; i++ {
			// Generate a safe relative path (no traversal, no blocklist)
			segments := rapid.IntRange(1, 3).Draw(rt, "segments")
			parts := make([]string, segments)
			for j := 0; j < segments; j++ {
				parts[j] = rapid.StringMatching(`[a-z]{1,8}`).Draw(rt, "part")
			}
			relPath := filepath.Join(parts...) + ".txt"
			if seen[relPath] {
				continue // skip duplicates
			}
			seen[relPath] = true
			content := []byte(rapid.StringMatching(`[a-zA-Z0-9 ]{0,100}`).Draw(rt, "content"))
			entries = append(entries, WriteEntry{
				RelPath: relPath,
				Content: content,
				Mode:    0o644,
			})
		}
		if len(entries) == 0 {
			return // nothing to test in this iteration
		}

		// Optionally create some pre-existing files
		preImages := make(map[string][]byte)
		for _, e := range entries {
			if rapid.Bool().Draw(rt, "preexist") {
				abs := filepath.Join(root, e.RelPath)
				os.MkdirAll(filepath.Dir(abs), 0o755)
				preContent := []byte(rapid.StringMatching(`[a-zA-Z0-9]{0,50}`).Draw(rt, "preContent"))
				os.WriteFile(abs, preContent, 0o644)
				preImages[e.RelPath] = preContent
			}
		}

		report, err := ops.ApplyAtomic(context.Background(), root, entries)

		if err != nil {
			// On failure: every target must equal its pre-image
			for _, e := range entries {
				abs := filepath.Join(root, e.RelPath)
				if pre, existed := preImages[e.RelPath]; existed {
					got, readErr := os.ReadFile(abs)
					if readErr != nil {
						t.Fatalf("pre-existing file %q unreadable after failed apply: %v", e.RelPath, readErr)
					}
					if string(got) != string(pre) {
						t.Fatalf("pre-image not restored for %q: got %q, want %q", e.RelPath, got, pre)
					}
				}
			}
		} else {
			// On success: all targets hold new content and backups exist for pre-existing
			for i, e := range entries {
				abs := report.Written[i]
				got, readErr := os.ReadFile(abs)
				if readErr != nil {
					t.Fatalf("written file %q unreadable: %v", e.RelPath, readErr)
				}
				if string(got) != string(e.Content) {
					t.Fatalf("content mismatch for %q: got %q, want %q", e.RelPath, got, e.Content)
				}

				// Verify path is within root
				resolvedRoot, _ := filepath.EvalSymlinks(root)
				if !strings.HasPrefix(abs, resolvedRoot) {
					t.Fatalf("written path %q outside root %q", abs, resolvedRoot)
				}
			}

			// Verify backups exist for pre-existing files
			preExistCount := 0
			for _, e := range entries {
				if _, existed := preImages[e.RelPath]; existed {
					preExistCount++
				}
			}
			if preExistCount > 0 && len(report.Backups) == 0 {
				t.Fatal("expected backups for pre-existing files")
			}
		}
	})
}

func TestProperty_P08_RootConfinement(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		ops := New()

		// Generate traversal paths
		traversals := []string{
			"../escape.txt",
			"../../etc/passwd",
			"sub/../../../out.txt",
		}
		path := rapid.SampledFrom(traversals).Draw(rt, "path")

		_, err := ops.ApplyAtomic(context.Background(), root, []WriteEntry{
			{RelPath: path, Content: []byte("x"), Mode: 0o644},
		})
		if err == nil {
			t.Fatal("traversal path should be rejected")
		}
	})
}

func TestProperty_P08_BlocklistRejection(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		ops := New()

		blockedPaths := []string{
			".env",
			"sub/.env",
			"key.pem",
			"certs/server.PEM",
		}
		path := rapid.SampledFrom(blockedPaths).Draw(rt, "blocked")

		_, err := ops.ApplyAtomic(context.Background(), root, []WriteEntry{
			{RelPath: path, Content: []byte("x"), Mode: 0o644},
		})
		if err == nil {
			t.Fatalf("blocked path %q should be rejected", path)
		}
	})
}

func TestProperty_P08_ContentIdempotence(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		ops := New()

		content := []byte(rapid.StringMatching(`[a-zA-Z0-9]{1,50}`).Draw(rt, "content"))
		entries := []WriteEntry{
			{RelPath: "file.txt", Content: content, Mode: 0o644},
		}

		// Apply once
		_, err := ops.ApplyAtomic(context.Background(), root, entries)
		if err != nil {
			t.Fatalf("first apply: %v", err)
		}

		// Apply again with same content
		_, err = ops.ApplyAtomic(context.Background(), root, entries)
		if err != nil {
			t.Fatalf("second apply: %v", err)
		}

		// Content must be identical
		got, _ := os.ReadFile(filepath.Join(root, "file.txt"))
		if string(got) != string(content) {
			t.Fatalf("content changed after idempotent apply")
		}
	})
}
