// SPDX-License-Identifier: Apache-2.0

// Q-02 — byte-exact revert (design §10.5, §11.6, Appendix A.9 postconditions, Appendix B).
//
// Property, universally quantified over apply-then-revert sequences:
//
//	Revert(manifest) restores every file byte-for-byte to its pre-image, including deleting
//	files that did not previously exist; revert is idempotent; a consumed handle cannot be
//	reused.
//
// # What each clause quantifies over, and why it is not the example test
//
// `apply_test.go` already has five `TestRevert_*` examples. They are kept — they name the
// specific shapes a reader wants to see — but each fixes one change-set. Q-02 quantifies over
// GENERATED sequences, and three of the generated dimensions are the ones that matter:
//
//   - CONTENT IS ARBITRARY BYTES, not printable text. "Byte-for-byte" is only a real claim if
//     the pre-image can contain NUL, 0xFF and invalid UTF-8. Q-01's generator draws from
//     `[a-zA-Z0-9 ]`, which a restore that round-tripped through a string would survive.
//   - THE ACTION MIX INCLUDES `Delete`. A delete's pre-image is the content of a file that is
//     gone after the apply, so restoring it exercises the branch a create/update-only generator
//     never reaches. Phase 0 had no Delete at all.
//   - DEPTH VARIES. An entry may sit at the root, one directory down, or two, so the restore
//     runs against parents the apply itself created.
//
// # The whole tree is compared, not just the targets
//
// Each clause snapshots every file under `root` before the apply and compares the whole set
// after the revert. Comparing only the entries the manifest names would miss a revert that
// restored its targets correctly and left something else behind, which is precisely the shape
// the negative control produces.
//
// Two artifacts are excluded from that comparison, and the exclusion is a limit of the claim
// rather than a convenience:
//
//   - `*.backup.*` files. `Revert` restores FROM them and is not specified to remove them, so a
//     revert leaves the backups in place. Asserting their absence would assert a behaviour the
//     design does not promise.
//   - `root/.forgeops-rollback/`. That is where the single-use marker lives, and the marker
//     existing after a revert is the point.
//
// A third leftover is NOT excluded but is not asserted either, and it is recorded here rather
// than left for a reader to discover: a revert removes the FILES an apply created but not the
// DIRECTORIES it created on the way. After reverting a change-set whose only entry was
// `d0/d1/x.txt`, `root/d0/d1` remains as an empty directory. Appendix B words Q-02 over files
// ("restores every file byte-for-byte … including deleting files that did not previously
// exist"), and a file-level snapshot is therefore faithful to the property as specified. The
// gap is real, is narrow, and belongs to `Revert`'s specification rather than to this test —
// see the leaf's journal entry.
//
// # Negative control (`mutations.toml` Q-02)
//
// "Make `Revert` skip entries marked `NO_PREVIOUS`", via a `go build -overlay` of
// `revert_entry.go`. Under it, every file the apply CREATED survives the revert.
// `TestProperty_Q02_AFileThatDidNotExistIsDeleted` seeds a guaranteed create so the control
// bites on the first example rather than on whichever generated example happens to contain one,
// and the whole-tree clause objects independently.
package mutate

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"pgregory.net/rapid"
)

// q02Plan is one generated entry together with the pre-state the generator put on disk.
type q02Plan struct {
	relPath string
	entry   Entry
	pre     []byte // the pre-image bytes; nil exactly when the target did not exist
	had     bool
}

// drawRevertPlans generates a change-set that is constructed to APPLY SUCCESSFULLY, because
// Q-02 quantifies over apply-then-revert SEQUENCES and a refused apply produces no manifest to
// revert.
//
// Success is engineered, not hoped for: relative paths are unique, a `Create` is only drawn for
// a path with nothing on it, `Update`/`Delete` carry the true pre-image hash, and no generated
// name is ever also used as a directory component (every leaf ends in `.txt`, every directory
// is `d0`/`d1`). A caller therefore treats an apply error as a test failure rather than
// tolerating it — a tolerated refusal is how a clause quietly stops testing anything.
func drawRevertPlans(rt *rapid.T, root string, count int) []q02Plan {
	plans := make([]q02Plan, 0, count)
	seen := make(map[string]bool, count)
	for i := 0; i < count; i++ {
		depth := rapid.IntRange(0, 2).Draw(rt, "depth")
		parts := make([]string, 0, depth+1)
		for d := 0; d < depth; d++ {
			parts = append(parts, fmt.Sprintf("d%d", d))
		}
		parts = append(parts, rapid.StringMatching(`[a-z]{1,6}`).Draw(rt, "name")+".txt")
		relPath := filepath.Join(parts...)
		if seen[relPath] {
			continue
		}
		seen[relPath] = true

		had := rapid.Bool().Draw(rt, "preexist")
		if !had {
			plans = append(plans, q02Plan{
				relPath: relPath,
				entry: Entry{
					RelPath: relPath,
					Action:  Create,
					Content: rapid.SliceOfN(rapid.Byte(), 0, 40).Draw(rt, "content"),
					Mode:    0o644,
				},
			})
			continue
		}

		pre := rapid.SliceOfN(rapid.Byte(), 0, 40).Draw(rt, "pre")
		seedFile(rt, filepath.Join(root, relPath), pre)

		// An existing target is either replaced or removed. Both have a pre-image, so both are
		// things a revert must put back; only the second one leaves nothing on disk to compare
		// against between the apply and the revert.
		if rapid.Bool().Draw(rt, "deleteIt") {
			plans = append(plans, q02Plan{
				relPath: relPath,
				entry: Entry{
					RelPath:      relPath,
					Action:       Delete,
					ExpectedHash: hashBytes(pre),
				},
				pre: pre,
				had: true,
			})
			continue
		}
		plans = append(plans, q02Plan{
			relPath: relPath,
			entry: Entry{
				RelPath:      relPath,
				Action:       Update,
				Content:      rapid.SliceOfN(rapid.Byte(), 0, 40).Draw(rt, "content"),
				ExpectedHash: hashBytes(pre),
				Mode:         0o644,
			},
			pre: pre,
			had: true,
		})
	}
	return plans
}

func q02Entries(plans []q02Plan) []Entry {
	entries := make([]Entry, len(plans))
	for i, p := range plans {
		entries[i] = p.entry
	}
	return entries
}

// seedFile writes a pre-image, creating its parents.
func seedFile(rt *rapid.T, abs string, data []byte) {
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		rt.Fatalf("MkdirAll %s: %v", filepath.Dir(abs), err)
	}
	if err := os.WriteFile(abs, data, 0o644); err != nil {
		rt.Fatalf("WriteFile %s: %v", abs, err)
	}
}

// snapshotTree hashes every file under root, keyed by slash-separated relative path.
//
// Excludes the two artifacts named in this file's header comment: backup files, which a revert
// reads and is not specified to remove, and the rollback state directory, which is where the
// single-use marker is supposed to appear.
func snapshotTree(rt *rapid.T, root string) map[string]string {
	out := make(map[string]string)
	walkErr := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if d.Name() == rollbackDirName {
				return fs.SkipDir
			}
			return nil
		}
		if strings.Contains(d.Name(), ".backup.") {
			return nil
		}
		data, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		rel, relErr := filepath.Rel(root, path)
		if relErr != nil {
			return relErr
		}
		out[filepath.ToSlash(rel)] = hashBytes(data)
		return nil
	})
	if walkErr != nil {
		rt.Fatalf("walk %s: %v", root, walkErr)
	}
	return out
}

// assertTreesEqual compares two snapshots and names every difference, in both directions.
//
// Both directions matter: a file that should be gone and is not is the negative control's
// signature, and a file that should be there and is not is a failed restore. A single-count
// comparison would report one of those as the other.
func assertTreesEqual(rt *rapid.T, want, got map[string]string, when string) {
	for rel, wantHash := range want {
		gotHash, present := got[rel]
		if !present {
			rt.Fatalf("%s: %s existed before the apply and is missing", when, rel)
		}
		if gotHash != wantHash {
			rt.Fatalf("%s: %s holds %s, the pre-image hashed %s", when, rel, gotHash, wantHash)
		}
	}
	for rel := range got {
		if _, present := want[rel]; !present {
			rt.Fatalf("%s: %s did not exist before the apply and exists now", when, rel)
		}
	}
}

// TestProperty_Q02_RevertRestoresEveryFileByteForByte is the main clause: the whole tree, before
// the apply and after the revert, hash for hash.
func TestProperty_Q02_RevertRestoresEveryFileByteForByte(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		plans := drawRevertPlans(rt, root, rapid.IntRange(1, 5).Draw(rt, "count"))
		if len(plans) == 0 {
			return
		}
		before := snapshotTree(rt, root)

		report, err := ApplyVerified(context.Background(), verified(t, 1), root, q02Entries(plans))
		if err != nil {
			rt.Fatalf("the generated change-set was built to apply cleanly and did not: %v", err)
		}

		// NOTE ON TRIVIAL EXAMPLES. A generated set can be a no-op on disk — updates writing
		// the bytes that were already there. Such an example is not filtered out and not
		// skipped: the clause is an equality that holds either way, and a `return` here would be
		// the silent-skip shape §0.4.4 forbids dressed up as a guard. The informative examples
		// are the ones that do change the tree, and the neighbouring clauses seed a guaranteed
		// create and a guaranteed delete so that at least one change is present by construction.
		if _, err := Revert(context.Background(), verified(t, 2), report.Backups); err != nil {
			rt.Fatalf("Revert: %v", err)
		}
		assertTreesEqual(rt, before, snapshotTree(rt, root), "after revert")
	})
}

// TestProperty_Q02_AFileThatDidNotExistIsDeleted is the clause the negative control removes.
//
// A guaranteed create is seeded on top of the generated set, so the clause has something to
// assert on EVERY example. Relying on the generator to draw a create would make the control's
// bite depend on which examples rapid happened to produce, and a control that bites only
// sometimes is a control a future reader cannot trust.
func TestProperty_Q02_AFileThatDidNotExistIsDeleted(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		plans := drawRevertPlans(rt, root, rapid.IntRange(0, 4).Draw(rt, "count"))

		created := filepath.Join("q02", "guaranteed-create.txt")
		entries := append(q02Entries(plans), Entry{
			RelPath: created,
			Action:  Create,
			Content: rapid.SliceOfN(rapid.Byte(), 0, 40).Draw(rt, "newContent"),
			Mode:    0o644,
		})

		report, err := ApplyVerified(context.Background(), verified(t, 1), root, entries)
		if err != nil {
			rt.Fatalf("the generated change-set was built to apply cleanly and did not: %v", err)
		}
		if _, statErr := os.Stat(filepath.Join(root, created)); statErr != nil {
			rt.Fatalf("the apply did not create %s: %v", created, statErr)
		}

		revertReport, err := Revert(context.Background(), verified(t, 2), report.Backups)
		if err != nil {
			rt.Fatalf("Revert: %v", err)
		}

		// Every entry with no pre-image must be gone from disk …
		var wantRemoved []string
		for _, p := range plans {
			if !p.had {
				wantRemoved = append(wantRemoved, p.entry.RelPath)
			}
		}
		wantRemoved = append(wantRemoved, created)
		for _, rel := range wantRemoved {
			if _, statErr := os.Stat(filepath.Join(root, rel)); !os.IsNotExist(statErr) {
				rt.Fatalf("%s did not exist before the apply and survived the revert (stat: %v)", rel, statErr)
			}
		}

		// … and the report must SAY so. The on-disk assertion above and this one fail together
		// under the control, but they fail for different reasons — the first is the user's
		// filesystem, the second is what the backend records as having happened — and a revert
		// that removed the files while under-reporting them would be a different defect.
		sort.Strings(wantRemoved)
		if !equalStrings(revertReport.Removed, wantRemoved) {
			rt.Fatalf("report.Removed = %v, want %v", revertReport.Removed, wantRemoved)
		}
	})
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// TestProperty_Q02_RevertIsIdempotentInEffect quantifies over the NUMBER of extra reverts.
//
// Idempotence here is a statement about the filesystem, not about the return value: the second
// and later calls are REFUSED (`ErrHandleConsumed`), and the refusal is what makes the effect
// idempotent — a second success would restore from backups that no longer describe the current
// state. Both halves are asserted together because either alone is satisfiable by the wrong
// implementation: a `Revert` that always errored would pass the refusal check, and one that
// re-restored identical bytes would pass the filesystem check.
//
// The consumption marker's own bytes are compared too. It is written with a `reverted_at`
// timestamp, so a refused revert that nonetheless rewrote the marker would be visible here and
// invisible to a snapshot that skips the rollback directory.
func TestProperty_Q02_RevertIsIdempotentInEffect(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		plans := drawRevertPlans(rt, root, rapid.IntRange(1, 4).Draw(rt, "count"))
		if len(plans) == 0 {
			return
		}

		report, err := ApplyVerified(context.Background(), verified(t, 1), root, q02Entries(plans))
		if err != nil {
			rt.Fatalf("the generated change-set was built to apply cleanly and did not: %v", err)
		}
		if _, err := Revert(context.Background(), verified(t, 2), report.Backups); err != nil {
			rt.Fatalf("first Revert: %v", err)
		}

		settled := snapshotTree(rt, root)
		marker := filepath.Join(root, rollbackDirName, report.Backups.HandleID+".consumed")
		markerBefore, err := os.ReadFile(marker)
		if err != nil {
			rt.Fatalf("the consumption marker is missing after a successful revert: %v", err)
		}

		for attempt := 0; attempt < rapid.IntRange(1, 3).Draw(rt, "extraReverts"); attempt++ {
			_, err := Revert(context.Background(), verified(t, int64(3+attempt)), report.Backups)
			if !errors.Is(err, ErrHandleConsumed) {
				rt.Fatalf("revert %d: expected ErrHandleConsumed, got %v", attempt+2, err)
			}
			assertTreesEqual(rt, settled, snapshotTree(rt, root),
				fmt.Sprintf("after refused revert %d", attempt+2))
			markerAfter, readErr := os.ReadFile(marker)
			if readErr != nil {
				rt.Fatalf("marker unreadable after a refused revert: %v", readErr)
			}
			if !bytes.Equal(markerBefore, markerAfter) {
				rt.Fatalf("a refused revert rewrote the consumption marker")
			}
		}
	})
}

// TestProperty_Q02_AConsumedHandleCannotBeReused pins WHERE single-use lives.
//
// Two reuse attempts that a weaker implementation would let through:
//
//	(1) a FRESH authority. The revert is authorised by its own envelope mint, so "already
//	    reverted" cannot be a property of the authority — the marker has to be keyed on the
//	    handle.
//	(2) a RECONSTRUCTED manifest VALUE with the same HandleID. The manifest is serialised to
//	    the backend and handed back, so in-memory state would not survive the round trip. This
//	    is the clause that would fail if `consumed` were ever moved onto the struct.
func TestProperty_Q02_AConsumedHandleCannotBeReused(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		plans := drawRevertPlans(rt, root, rapid.IntRange(1, 4).Draw(rt, "count"))
		if len(plans) == 0 {
			return
		}

		report, err := ApplyVerified(context.Background(), verified(t, 1), root, q02Entries(plans))
		if err != nil {
			rt.Fatalf("the generated change-set was built to apply cleanly and did not: %v", err)
		}
		if _, err := Revert(context.Background(), verified(t, 2), report.Backups); err != nil {
			rt.Fatalf("first Revert: %v", err)
		}
		settled := snapshotTree(rt, root)

		// (2) — a value the backend could plausibly have deserialised, sharing nothing with the
		// original but the identifiers.
		roundTripped := BackupManifest{
			HandleID:       report.Backups.HandleID,
			Root:           report.Backups.Root,
			CreatedAt:      report.Backups.CreatedAt,
			EnvelopeDigest: report.Backups.EnvelopeDigest,
			Entries:        append([]BackupEntry(nil), report.Backups.Entries...),
		}
		if _, err := Revert(context.Background(), verified(t, 3), roundTripped); !errors.Is(err, ErrHandleConsumed) {
			rt.Fatalf("a rebuilt manifest with the same HandleID was accepted: %v", err)
		}
		assertTreesEqual(rt, settled, snapshotTree(rt, root), "after a rebuilt-manifest revert")

		// A DIFFERENT handle over the same entries is a different question and must NOT be
		// refused by the marker — otherwise single-use would be "one revert per root", which
		// would break the second apply of the same change-set.
		fresh := roundTripped
		fresh.HandleID = report.Backups.HandleID + "0"
		if _, err := Revert(context.Background(), verified(t, 4), fresh); errors.Is(err, ErrHandleConsumed) {
			rt.Fatalf("a different handle was refused as consumed; the marker is not handle-scoped")
		}
	})
}

// TestProperty_Q02_ADeletedFileIsRestoredFromItsBackup isolates the Delete action.
//
// Separated from the whole-tree clause because a delete is the one action whose target is ABSENT
// between the apply and the revert, so "restore the pre-image" is the only thing that can put it
// back — there is nothing on disk for a partial implementation to leave behind and look correct.
func TestProperty_Q02_ADeletedFileIsRestoredFromItsBackup(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		plans := drawRevertPlans(rt, root, rapid.IntRange(0, 3).Draw(rt, "count"))

		doomed := filepath.Join("q02", "guaranteed-delete.txt")
		pre := rapid.SliceOfN(rapid.Byte(), 0, 60).Draw(rt, "doomedPre")
		seedFile(rt, filepath.Join(root, doomed), pre)

		entries := append(q02Entries(plans), Entry{
			RelPath:      doomed,
			Action:       Delete,
			ExpectedHash: hashBytes(pre),
		})
		report, err := ApplyVerified(context.Background(), verified(t, 1), root, entries)
		if err != nil {
			rt.Fatalf("the generated change-set was built to apply cleanly and did not: %v", err)
		}
		if _, statErr := os.Stat(filepath.Join(root, doomed)); !os.IsNotExist(statErr) {
			rt.Fatalf("the apply did not delete %s (stat: %v)", doomed, statErr)
		}

		revertReport, err := Revert(context.Background(), verified(t, 2), report.Backups)
		if err != nil {
			rt.Fatalf("Revert: %v", err)
		}
		got, err := os.ReadFile(filepath.Join(root, doomed))
		if err != nil {
			rt.Fatalf("%s was not restored: %v", doomed, err)
		}
		if !bytes.Equal(got, pre) {
			rt.Fatalf("%s restored to %x, the pre-image was %x", doomed, got, pre)
		}
		found := false
		for _, rel := range revertReport.Restored {
			if rel == doomed {
				found = true
			}
		}
		if !found {
			rt.Fatalf("report.Restored = %v, missing the deleted file %s", revertReport.Restored, doomed)
		}
	})
}

// TestProperty_Q02_ATamperedBackupIsRefusedAndNothingIsRestored is the other half of
// "byte-for-byte": a revert must never restore bytes that are not the pre-image.
//
// The tampered value is drawn, and drawn to differ from the honest one. A generated value equal
// to the honest one would make the example a no-op that passes for the wrong reason — the shape
// leaf 7.9's tamper matrix had to be corrected for.
func TestProperty_Q02_ATamperedBackupIsRefusedAndNothingIsRestored(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		root := t.TempDir()
		plans := drawRevertPlans(rt, root, rapid.IntRange(0, 3).Draw(rt, "count"))

		target := filepath.Join("q02", "tamper-target.txt")
		pre := rapid.SliceOfN(rapid.Byte(), 1, 40).Draw(rt, "targetPre")
		seedFile(rt, filepath.Join(root, target), pre)

		entries := append(q02Entries(plans), Entry{
			RelPath:      target,
			Action:       Update,
			Content:      []byte("post-apply content"),
			ExpectedHash: hashBytes(pre),
			Mode:         0o644,
		})
		report, err := ApplyVerified(context.Background(), verified(t, 1), root, entries)
		if err != nil {
			rt.Fatalf("the generated change-set was built to apply cleanly and did not: %v", err)
		}

		var backup string
		for _, e := range report.Backups.Entries {
			if e.RelPath == target {
				backup = e.BackupPath
			}
		}
		if backup == "" {
			rt.Fatalf("no backup was recorded for %s, which had a pre-image", target)
		}

		forged := rapid.SliceOfN(rapid.Byte(), 0, 40).Draw(rt, "forged")
		if bytes.Equal(forged, pre) {
			// Equal to the honest bytes is not tampering, and asserting a refusal for it would
			// assert something false. Drawn again rather than skipped: a skip inside a
			// generated clause is the silent-skip shape §0.4.4 forbids.
			forged = append(forged, 0x00)
		}
		if err := os.WriteFile(backup, forged, 0o644); err != nil {
			rt.Fatalf("tamper: %v", err)
		}

		postApply := snapshotTree(rt, root)
		if _, err := Revert(context.Background(), verified(t, 2), report.Backups); !errors.Is(err, ErrManifestTampered) {
			rt.Fatalf("expected ErrManifestTampered, got %v", err)
		}
		// Refused before the first write: the tree must be exactly as the apply left it, and the
		// handle must still be usable once the backup is put right.
		assertTreesEqual(rt, postApply, snapshotTree(rt, root), "after a refused tampered revert")
		if _, statErr := os.Stat(filepath.Join(root, rollbackDirName,
			report.Backups.HandleID+".consumed")); statErr == nil {
			rt.Fatalf("a refused revert consumed the handle")
		}
	})
}
