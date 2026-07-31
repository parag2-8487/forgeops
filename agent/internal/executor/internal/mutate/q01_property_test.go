// SPDX-License-Identifier: Apache-2.0

// Q-01 — atomic all-or-nothing application (design §10.5, §7.11(f), Appendix A.9, Appendix B).
//
// Property, universally quantified over change-sets and injected failure points:
//
//	after ApplyVerified either every target holds its new content AND a backup exists for every
//	pre-existing target, or every target byte-equals its pre-image; no path outside root is
//	written; blockedForWrite paths are always refused; .env.example is permitted while .env is
//	not.
//
// # Why this is a property and how it differs from P-08
//
// P-08 (relocated with the algorithm by D-45) quantifies the same all-or-nothing shape over
// generated change-sets, and it is kept. Q-01 adds the half P-08 never had: a generated FAILURE
// POINT. "Either all or nothing" is trivially satisfied by a change-set that always succeeds, so
// the interesting quantification is over *where* the apply breaks — and the only way to observe
// the CATCH branch is to make it run.
//
// # How the failure is injected, and why not with a read-only directory
//
// The existing example test uses a 0555 directory and is skipped on Windows, which is exactly the
// "gate that can never pass locally" shape D-51 rejects. This file injects instead by ORDERING: an
// entry creates a plain file `collide`, and a later entry in the same set targets `collide/child`.
// Both survive pre-validation — neither path exists when it runs — and the second one's MkdirAll
// then fails because its parent is a regular file. That is deterministic on every platform, needs
// no permission games, and is a change-set shape a real caller could actually produce.
//
// The failure INDEX is generated, so the rollback is exercised with 0..n-1 completed writes rather
// than only with one.
//
// Negative control (`mutations.toml` Q-01): the rollback loop is removed from the CATCH branch,
// via a `go build -overlay` of `rollback.go`. The property must then fail.
package mutate

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"pgregory.net/rapid"
)

// planned is one generated entry plus the pre-state the generator knows it had.
type planned struct {
	entry Entry
	pre   []byte
	had   bool
}

// drawPlans generates a set of entries with unique relative paths, half of which have a
// pre-image on disk. Shared by every clause below so they quantify over the same space.
func drawPlans(rt *rapid.T, root string, count int) []planned {
	plans := make([]planned, 0, count)
	seen := make(map[string]bool, count)
	for i := 0; i < count; i++ {
		name := rapid.StringMatching(`[a-z]{1,6}`).Draw(rt, "name")
		relPath := name + ".txt"
		if seen[relPath] {
			continue
		}
		seen[relPath] = true

		content := []byte(rapid.StringMatching(`[a-zA-Z0-9 ]{0,40}`).Draw(rt, "content"))
		had := rapid.Bool().Draw(rt, "preexist")
		var pre []byte
		action := Create
		expected := ""
		if had {
			pre = []byte(rapid.StringMatching(`[a-zA-Z0-9 ]{1,40}`).Draw(rt, "pre"))
			abs := filepath.Join(root, relPath)
			if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
				rt.Fatalf("MkdirAll: %v", err)
			}
			if err := os.WriteFile(abs, pre, 0o644); err != nil {
				rt.Fatalf("WriteFile: %v", err)
			}
			action = Update
			expected = hashBytes(pre)
		}
		plans = append(plans, planned{
			entry: Entry{
				RelPath:      relPath,
				Action:       action,
				Content:      content,
				ExpectedHash: expected,
				Mode:         0o644,
			},
			pre: pre,
			had: had,
		})
	}
	return plans
}

func entriesOf(plans []planned) []Entry {
	entries := make([]Entry, len(plans))
	for i, p := range plans {
		entries[i] = p.entry
	}
	return entries
}

// assertPreImagesIntact is the "nothing happened" half of the disjunction.
func assertPreImagesIntact(rt *rapid.T, root string, plans []planned) {
	for _, p := range plans {
		abs := filepath.Join(root, p.entry.RelPath)
		data, err := os.ReadFile(abs)
		switch {
		case p.had && err != nil:
			rt.Fatalf("%s had a pre-image and is now unreadable: %v", p.entry.RelPath, err)
		case p.had && !bytes.Equal(data, p.pre):
			rt.Fatalf("%s was not restored: have %q, want %q", p.entry.RelPath, data, p.pre)
		case !p.had && err == nil:
			rt.Fatalf("%s did not exist before and exists now", p.entry.RelPath)
		}
	}
}

// TestProperty_Q01_EitherEveryTargetIsNewOrEveryTargetIsItsPreImage is the disjunction itself,
// over a set that SUCCEEDS. The failing half is the next test.
func TestProperty_Q01_EitherEveryTargetIsNewOrEveryTargetIsItsPreImage(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		plans := drawPlans(rt, root, rapid.IntRange(1, 5).Draw(rt, "count"))
		if len(plans) == 0 {
			return
		}

		report, err := ApplyVerified(context.Background(), verified(t, 1), root, entriesOf(plans))
		if err != nil {
			// A refusal is a legitimate outcome, and the disjunction's other branch then has to
			// hold: nothing may have changed.
			assertPreImagesIntact(rt, root, plans)
			return
		}

		for _, p := range plans {
			data, readErr := os.ReadFile(filepath.Join(root, p.entry.RelPath))
			if readErr != nil {
				rt.Fatalf("%s: %v", p.entry.RelPath, readErr)
			}
			if !bytes.Equal(data, p.entry.Content) {
				rt.Fatalf("%s: have %q, want %q", p.entry.RelPath, data, p.entry.Content)
			}
		}

		// "a backup exists for every pre-existing target" — and, just as importantly, NOT for a
		// target that did not exist, because Revert reads that absence as "delete this" (Q-02).
		byPath := make(map[string]BackupEntry, len(report.Backups.Entries))
		for _, b := range report.Backups.Entries {
			byPath[b.RelPath] = b
		}
		if len(byPath) != len(plans) {
			rt.Fatalf("manifest has %d entries for %d targets", len(byPath), len(plans))
		}
		for _, p := range plans {
			b, ok := byPath[p.entry.RelPath]
			if !ok {
				rt.Fatalf("%s is absent from the manifest", p.entry.RelPath)
			}
			if p.had {
				if b.NoPrevious() {
					rt.Fatalf("%s had a pre-image but the manifest says NO_PREVIOUS", p.entry.RelPath)
				}
				backup, backupErr := os.ReadFile(b.BackupPath)
				if backupErr != nil {
					rt.Fatalf("backup for %s is unreadable: %v", p.entry.RelPath, backupErr)
				}
				if !bytes.Equal(backup, p.pre) {
					rt.Fatalf("backup for %s holds %q, want the pre-image %q", p.entry.RelPath, backup, p.pre)
				}
				if b.PreImageHash != hashBytes(p.pre) {
					rt.Fatalf("%s: manifest pre-image hash disagrees with the bytes", p.entry.RelPath)
				}
				continue
			}
			if !b.NoPrevious() {
				rt.Fatalf("%s did not exist but the manifest names a backup %q", p.entry.RelPath, b.BackupPath)
			}
		}
	})
}

// TestProperty_Q01_AnInjectedFailureLeavesEveryTargetAtItsPreImage is the clause P-08 could not
// make: the CATCH branch, exercised at a generated position.
func TestProperty_Q01_AnInjectedFailureLeavesEveryTargetAtItsPreImage(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		plans := drawPlans(rt, root, rapid.IntRange(1, 5).Draw(rt, "count"))
		if len(plans) == 0 {
			return
		}

		// The injection: a plain file, then a child of it. `at` decides how many of the generated
		// entries complete before the failure, so the rollback runs with 0..n completed writes.
		at := rapid.IntRange(0, len(plans)).Draw(rt, "failAfter")
		entries := make([]Entry, 0, len(plans)+2)
		entries = append(entries, entriesOf(plans)[:at]...)
		entries = append(entries,
			Entry{RelPath: "collide", Action: Create, Content: []byte("a regular file"), Mode: 0o644},
			Entry{RelPath: filepath.Join("collide", "child.txt"), Action: Create,
				Content: []byte("cannot be created under a file"), Mode: 0o644},
		)
		entries = append(entries, entriesOf(plans)[at:]...)

		_, err := ApplyVerified(context.Background(), verified(t, 1), root, entries)
		if err == nil {
			rt.Fatalf("the injected failure did not fail; the clause is untested for this example")
		}

		// Every generated target must be back at its pre-image — including the ones written
		// before the failure, which is the whole of the CATCH branch.
		assertPreImagesIntact(rt, root, plans)

		// And the injected file itself must be gone: it had no pre-image, so rollback deletes it.
		if _, statErr := os.Stat(filepath.Join(root, "collide")); statErr == nil {
			rt.Fatalf("the file written immediately before the failure survived the rollback")
		}
	})
}

// TestProperty_Q01_NoPathOutsideRootIsEverWritten quantifies the confinement clause.
//
// The claim is "no path outside root is written", which is NOT the same as "every suspicious path
// is refused" — and the difference is platform-visible. On Windows `\canary.txt` is drive-relative
// rather than absolute, so `filepath.IsAbs` is false and the path is joined onto root, landing
// safely inside it; on Linux the same string is absolute and is refused. The first version of this
// test asserted a refusal and failed on Windows for a candidate that had never escaped.
//
// So the assertion is the disjunction the design actually promises: either the apply is refused, or
// every path it wrote is inside root. Either way the canary outside root is untouched.
func TestProperty_Q01_NoPathOutsideRootIsEverWritten(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		parent := t.TempDir()
		root := filepath.Join(parent, "root")
		if err := os.MkdirAll(root, 0o755); err != nil {
			rt.Fatalf("MkdirAll: %v", err)
		}
		// A canary outside root. If any escape succeeded, this is what it would overwrite.
		canary := filepath.Join(parent, "canary.txt")
		if err := os.WriteFile(canary, []byte("untouched"), 0o644); err != nil {
			rt.Fatalf("WriteFile: %v", err)
		}

		depth := rapid.IntRange(1, 4).Draw(rt, "depth")
		// Built with explicit separators and NEVER through filepath.Join, because Join cleans:
		// `Join("nested", "../canary.txt")` is `"canary.txt"`, a perfectly legitimate in-root
		// path. The first version of this generator did exactly that and the property failed on a
		// candidate that had stopped being an escape — a generator bug that looked like a
		// confinement bug.
		sep := string(filepath.Separator)
		escape := strings.Repeat(".."+sep, depth) + "canary.txt"
		candidate := rapid.SampledFrom([]string{
			escape,
			"nested" + sep + strings.Repeat(".."+sep, depth+1) + "canary.txt",
			sep + "canary.txt",
			filepath.Join(parent, "canary.txt"),
			"." + sep + escape,
		}).Draw(rt, "relPath")

		report, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
			{RelPath: candidate, Action: Create, Content: []byte("escaped"), Mode: 0o644},
		})
		if err == nil {
			// Not refused, so every written path must be inside root.
			resolvedRoot, absErr := filepath.Abs(root)
			if absErr != nil {
				rt.Fatalf("Abs(root): %v", absErr)
			}
			for _, w := range report.Written {
				if w.AbsPath != resolvedRoot && !strings.HasPrefix(w.AbsPath, resolvedRoot+sep) {
					rt.Fatalf("%q was accepted and wrote %q, which is outside root %q",
						candidate, w.AbsPath, resolvedRoot)
				}
			}
		}
		data, readErr := os.ReadFile(canary)
		if readErr != nil || !bytes.Equal(data, []byte("untouched")) {
			rt.Fatalf("%q modified the canary outside root: %q (%v)", candidate, data, readErr)
		}
	})
}

// TestProperty_Q01_BlockedPathsAreAlwaysRefused quantifies D-46's write-intent blocklist.
//
// The generator produces the blocklist's own RULES rather than a list of names somebody guessed.
// §7.11(f) and D-46 define three: the `.env` family, `*.pem`, and anything under `~/.ssh` or
// `~/.aws`. The first two are exercised here inside a temp root; the third is an absolute
// home-directory rule that a rooted change-set cannot reach, and `fileops`' own tests cover it.
//
// Case variants are generated on purpose. The `.env` family is matched case-FOLDED, and the
// comment in `blocklist.go` records why: the comparison was case-sensitive, so `.ENV.PRODUCTION`
// was not blocked at all — and on Windows and macOS that is the same file as `.env.production`.
// A property that only ever generated lowercase would not have noticed.
func TestProperty_Q01_BlockedPathsAreAlwaysRefused(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()

		var name string
		switch rapid.SampledFrom([]string{"env", "env-suffixed", "pem"}).Draw(rt, "rule") {
		case "env":
			name = ".env"
		case "env-suffixed":
			// Any suffix EXCEPT the three writable exemptions.
			suffix := rapid.SampledFrom([]string{
				"production", "local", "prod.bak", "staging", "production.example.bak",
			}).Draw(rt, "suffix")
			name = ".env." + suffix
		case "pem":
			name = rapid.StringMatching(`[a-z]{1,6}`).Draw(rt, "stem") + ".pem"
		}
		switch rapid.SampledFrom([]string{"as-is", "upper", "title"}).Draw(rt, "case") {
		case "upper":
			name = strings.ToUpper(name)
		case "title":
			name = strings.ToUpper(name[:1]) + name[1:]
		}

		relPath := name
		if rapid.Bool().Draw(rt, "nested") {
			relPath = filepath.Join(rapid.StringMatching(`[a-z]{1,5}`).Draw(rt, "dir"), name)
		}

		_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
			{RelPath: relPath, Action: Create, Content: []byte("should never land"), Mode: 0o644},
		})
		if err == nil {
			rt.Fatalf("%q was accepted; blockedForWrite did not refuse it", relPath)
		}
		if _, statErr := os.Stat(filepath.Join(root, relPath)); statErr == nil {
			rt.Fatalf("%q exists after a refusal", relPath)
		}
	})
}

// TestProperty_Q01_EnvExampleIsWritableWhileEnvIsNot is D-46's split, stated as a property.
//
// The two names differ by one suffix and mean opposite things: `.env` holds real credentials and
// must never be written, while `.env.example` is a committed template a generator legitimately
// produces. A blocklist matching on prefix would refuse both, and one matching on the wrong
// boundary would permit both.
func TestProperty_Q01_EnvExampleIsWritableWhileEnvIsNot(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		dir := ""
		if rapid.Bool().Draw(rt, "nested") {
			dir = rapid.StringMatching(`[a-z]{1,5}`).Draw(rt, "dir")
		}
		content := []byte(rapid.StringMatching(`[A-Z_]{1,10}=[a-z]{0,10}`).Draw(rt, "content"))

		permitted := filepath.Join(dir, ".env.example")
		if _, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
			{RelPath: permitted, Action: Create, Content: content, Mode: 0o644},
		}); err != nil {
			rt.Fatalf("%q was refused; .env.example is a committed template, not a secret: %v", permitted, err)
		}
		data, err := os.ReadFile(filepath.Join(root, permitted))
		if err != nil || !bytes.Equal(data, content) {
			rt.Fatalf("%q was not written: %q (%v)", permitted, data, err)
		}

		refused := filepath.Join(dir, ".env")
		if _, err := ApplyVerified(context.Background(), verified(t, 2), root, []Entry{
			{RelPath: refused, Action: Create, Content: content, Mode: 0o644},
		}); err == nil {
			rt.Fatalf("%q was accepted; a change-set may never write a real .env", refused)
		}
		if _, statErr := os.Stat(filepath.Join(root, refused)); statErr == nil {
			rt.Fatalf("%q exists after a refusal", refused)
		}
	})
}

// TestProperty_Q01_AStaleChangeSetWritesNothing is the pre-validation branch of the disjunction.
//
// Separate from the injected-failure clause because it fails EARLIER — before any write — and the
// two guarantees have different mechanisms: this one is "validate everything before any I/O"
// (Appendix A.9's first loop), the other is the CATCH branch. A single test covering both would
// pass if either worked.
func TestProperty_Q01_AStaleChangeSetWritesNothing(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		plans := drawPlans(rt, root, rapid.IntRange(2, 5).Draw(rt, "count"))
		if len(plans) < 2 {
			return
		}
		// Find a generated entry that has a pre-image, and lie about its hash.
		stale := -1
		for i, p := range plans {
			if p.had {
				stale = i
				break
			}
		}
		if stale < 0 {
			return
		}
		entries := entriesOf(plans)
		entries[stale].ExpectedHash = hashBytes([]byte("a pre-image nobody wrote"))

		_, err := ApplyVerified(context.Background(), verified(t, 1), root, entries)
		if err == nil {
			rt.Fatalf("a stale change-set was applied")
		}
		if !errors.Is(err, ErrConflict) {
			rt.Fatalf("expected ErrConflict, got %v", err)
		}
		assertPreImagesIntact(rt, root, plans)
	})
}
