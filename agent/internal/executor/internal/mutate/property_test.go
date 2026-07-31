// SPDX-License-Identifier: Apache-2.0

// P-08 — atomic change sets. Relocated here from `internal/fileops` with the algorithm it
// guards (D-45).
//
// Moving a property is not a neutral act, so the reasoning is written down. P-08's clauses
// are about an algorithm — all-or-nothing, backup before mutate, root confinement,
// blocklist refusal, content idempotence — and that algorithm is now in this package.
// Left behind in `fileops`, P-08 would have been guarding a package that no longer
// contains the code, which is the "check that examines nothing" failure the Phase 1 design
// exists to eliminate. Every clause is the Phase 0 clause; the generator now produces
// `Entry` values with an explicit Action and a pre-image hash, because that is what the
// entry point takes.
package mutate

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

		// Generate a small set of valid entries with unique paths
		numEntries := rapid.IntRange(1, 5).Draw(rt, "numEntries")
		type plan struct {
			entry Entry
			pre   []byte
			had   bool
		}
		plans := make([]plan, 0, numEntries)
		seen := make(map[string]bool)
		for i := 0; i < numEntries; i++ {
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

			// Optionally create a pre-existing file, exactly as the Phase 0 property did.
			var pre []byte
			had := rapid.Bool().Draw(rt, "preexist")
			if had {
				pre = []byte(rapid.StringMatching(`[a-zA-Z0-9]{0,50}`).Draw(rt, "preContent"))
				abs := filepath.Join(root, relPath)
				if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
					rt.Fatalf("MkdirAll: %v", err)
				}
				if err := os.WriteFile(abs, pre, 0o644); err != nil {
					rt.Fatalf("WriteFile: %v", err)
				}
			}

			action := Create
			expected := ""
			if had {
				action = Update
				expected = hashBytes(pre)
			}
			plans = append(plans, plan{
				entry: Entry{RelPath: relPath, Action: action, Content: content,
					ExpectedHash: expected, Mode: 0o644},
				pre: pre,
				had: had,
			})
		}
		if len(plans) == 0 {
			return // nothing to test in this iteration
		}

		entries := make([]Entry, 0, len(plans))
		for _, p := range plans {
			entries = append(entries, p.entry)
		}

		report, err := ApplyVerified(context.Background(), verified(t, 1), root, entries)

		if err != nil {
			// On failure: every target must equal its pre-image
			for _, p := range plans {
				abs := filepath.Join(root, p.entry.RelPath)
				if p.had {
					got, readErr := os.ReadFile(abs)
					if readErr != nil {
						rt.Fatalf("pre-existing file %q unreadable after failed apply: %v", p.entry.RelPath, readErr)
					}
					if string(got) != string(p.pre) {
						rt.Fatalf("pre-image not restored for %q: got %q, want %q", p.entry.RelPath, got, p.pre)
					}
				}
			}
			return
		}

		// On success: all targets hold new content and backups exist for pre-existing
		for i, p := range plans {
			abs := report.Written[i].AbsPath
			got, readErr := os.ReadFile(abs)
			if readErr != nil {
				rt.Fatalf("written file %q unreadable: %v", p.entry.RelPath, readErr)
			}
			if string(got) != string(p.entry.Content) {
				rt.Fatalf("content mismatch for %q: got %q, want %q", p.entry.RelPath, got, p.entry.Content)
			}

			// Verify path is within root
			resolvedRoot, _ := filepath.EvalSymlinks(root)
			if !strings.HasPrefix(abs, resolvedRoot) {
				rt.Fatalf("written path %q outside root %q", abs, resolvedRoot)
			}

			// A backup exists for EVERY pre-existing target, which is stronger than the
			// Phase 0 clause's "at least one backup" and is what Q-01 actually asserts.
			backup := report.Backups.Entries[i].BackupPath
			if p.had {
				if backup == "" {
					rt.Fatalf("no backup recorded for pre-existing target %q", p.entry.RelPath)
				}
				backupData, backupErr := os.ReadFile(backup)
				if backupErr != nil {
					rt.Fatalf("backup for %q unreadable: %v", p.entry.RelPath, backupErr)
				}
				if string(backupData) != string(p.pre) {
					rt.Fatalf("backup for %q holds %q, want the pre-image %q",
						p.entry.RelPath, backupData, p.pre)
				}
			} else if backup != "" {
				rt.Fatalf("a target that did not exist got a backup at %q", backup)
			}
		}
	})
}

func TestProperty_P08_RootConfinement(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()

		traversals := []string{
			"../escape.txt",
			"../../etc/passwd",
			"sub/../../../out.txt",
		}
		path := rapid.SampledFrom(traversals).Draw(rt, "path")

		_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
			{RelPath: path, Action: Create, Content: []byte("x"), Mode: 0o644},
		})
		if err == nil {
			rt.Fatal("traversal path should be rejected")
		}
	})
}

func TestProperty_P08_BlocklistRejection(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()

		blockedPaths := []string{
			".env",
			"sub/.env",
			"key.pem",
			"certs/server.PEM",
			// D-46's exemptions must not widen by accident: a .bak of an example file and
			// a case variant are both still refused for writing.
			".env.example.bak",
			".ENV.EXAMPLE",
		}
		path := rapid.SampledFrom(blockedPaths).Draw(rt, "blocked")

		_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
			{RelPath: path, Action: Create, Content: []byte("x"), Mode: 0o644},
		})
		if err == nil {
			rt.Fatalf("blocked path %q should be rejected", path)
		}
	})
}

func TestProperty_P08_ContentIdempotence(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()

		content := []byte(rapid.StringMatching(`[a-zA-Z0-9]{1,50}`).Draw(rt, "content"))

		// Apply once as a create
		if _, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
			{RelPath: "file.txt", Action: Create, Content: content, Mode: 0o644},
		}); err != nil {
			rt.Fatalf("first apply: %v", err)
		}

		// Apply again with the same content, expecting the first apply's output. That the
		// second apply must NAME the first's hash is the pre-image regime working: an
		// apply that did not have to state what it expected could not tell "unchanged"
		// from "changed underneath me".
		if _, err := ApplyVerified(context.Background(), verified(t, 2), root, []Entry{
			{RelPath: "file.txt", Action: Update, Content: content,
				ExpectedHash: hashBytes(content), Mode: 0o644},
		}); err != nil {
			rt.Fatalf("second apply: %v", err)
		}

		got, _ := os.ReadFile(filepath.Join(root, "file.txt"))
		if string(got) != string(content) {
			rt.Fatalf("content changed after idempotent apply")
		}
	})
}
