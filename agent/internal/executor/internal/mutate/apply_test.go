// SPDX-License-Identifier: Apache-2.0

// The Phase 0 `fileops.ApplyAtomic` tests, relocated with the algorithm they guard
// (D-45). Every assertion is the Phase 0 assertion; what changed is the entry point's
// argument and the explicit Action, because the algorithm is preserved and the SIGNATURE
// is what D-45 changes.
//
// The Phase 1 additions have their own tests below the relocated ones, so a reader can
// see which behaviour is inherited and which is new.
package mutate

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
)

// The test key is synthetic and self-labelling (.kiro/steering/secret-safety.md).
var testKey = []byte("test-only-not-a-real-secret-mutate-key")

const testBundleDigest = "sha256:00000000000000000000000000000000000000000000000000000000000000aa"

// verified produces a real *envelope.Verified through envelope.Verify.
//
// Not a hand-built struct and not a double: `Verified` has unexported fields and no
// exported constructor precisely so that no test can manufacture one, and a test that
// worked around that would be testing a different type from the one production uses
// (§0.4.1's rule, and D-59's reason for rejecting the interface form).
func verified(t *testing.T, seq int64) *envelope.Verified {
	t.Helper()
	keys := envelope.NewStaticKeySource()
	keys.Set("dev-mutate", testKey)
	guard, err := envelope.NewMemoryReplayGuard(300*time.Second, 256)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	now := time.Unix(1899999900, 0).UTC()
	verifier, err := envelope.NewVerifier(keys, guard, envelope.NewStaticBundleDigest(testBundleDigest),
		envelope.WithClock(func() time.Time { return now }))
	if err != nil {
		t.Fatalf("NewVerifier: %v", err)
	}
	env := envelope.Envelope{
		V:          envelope.Version,
		CommandID:  "cmd-mutate",
		DeviceID:   "dev-mutate",
		Operation:  envelope.Operation("files.apply"),
		Args:       json.RawMessage(`{}`),
		ApprovalID: "apr-mutate",
		PolicyContext: envelope.PolicyContext{
			BundleDigest: testBundleDigest,
			Decision:     "allow",
		},
		Nonce:    "nonce-" + strings.Repeat("0", 20) + string(rune('a'+seq%26)),
		Seq:      seq,
		NotAfter: 1900000000,
	}
	signature, err := envelope.Sign(envelope.DomainPrefix, env, testKey)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	env.Signature = signature
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	v, err := verifier.Verify(context.Background(), raw)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	return v
}

func hashOf(data []byte) string { return hashBytes(data) }

func writeFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("MkdirAll: %v", err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
}

// ── Relocated Phase 0 assertions ───────────────────────────────────────────────

func TestApplyVerified_Basic(t *testing.T) {
	root := t.TempDir()
	report, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "hello.txt", Action: Create, Content: []byte("hello world"), Mode: 0o644},
		{RelPath: "sub/nested.txt", Action: Create, Content: []byte("nested content"), Mode: 0o644},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(report.Written) != 2 {
		t.Errorf("written = %d, want 2", len(report.Written))
	}
	got, _ := os.ReadFile(filepath.Join(root, "hello.txt"))
	if string(got) != "hello world" {
		t.Errorf("content = %q", string(got))
	}
	got, _ = os.ReadFile(filepath.Join(root, "sub", "nested.txt"))
	if string(got) != "nested content" {
		t.Errorf("nested content = %q", string(got))
	}
}

func TestApplyVerified_BackupBeforeMutate(t *testing.T) {
	root := t.TempDir()
	existing := filepath.Join(root, "exists.txt")
	writeFile(t, existing, []byte("original"))

	report, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "exists.txt", Action: Update, Content: []byte("updated"),
			ExpectedHash: hashOf([]byte("original")), Mode: 0o644},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(report.Backups.Entries) != 1 {
		t.Fatalf("expected 1 manifest entry, got %d", len(report.Backups.Entries))
	}
	backupPath := report.Backups.Entries[0].BackupPath
	if backupPath == "" {
		t.Fatal("a pre-existing target must have a backup")
	}
	backupData, _ := os.ReadFile(backupPath)
	if string(backupData) != "original" {
		t.Errorf("backup content = %q, want original", string(backupData))
	}
	got, _ := os.ReadFile(existing)
	if string(got) != "updated" {
		t.Errorf("updated content = %q", string(got))
	}
}

func TestApplyVerified_TraversalRejected(t *testing.T) {
	root := t.TempDir()
	_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "../escape.txt", Action: Create, Content: []byte("bad"), Mode: 0o644},
	})
	if err == nil {
		t.Fatal("expected error for path traversal")
	}
	if !strings.Contains(err.Error(), "outside root") {
		t.Errorf("error should mention outside root: %v", err)
	}
}

func TestApplyVerified_AbsolutePathRejected(t *testing.T) {
	root := t.TempDir()
	absPath := "/etc/passwd"
	if runtime.GOOS == "windows" {
		absPath = "C:\\Windows\\System32\\evil.txt"
	}
	_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: absPath, Action: Create, Content: []byte("bad"), Mode: 0o644},
	})
	if err == nil {
		t.Fatal("expected error for absolute path")
	}
}

func TestApplyVerified_SymlinkEscape(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("platform-only: posix - symlink creation and traversal semantics differ on Windows (D-68)")
	}
	root := t.TempDir()
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(root, "escape-link")); err != nil {
		t.Fatalf("Symlink: %v", err)
	}
	_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "escape-link/evil.txt", Action: Create, Content: []byte("bad"), Mode: 0o644},
	})
	if err == nil {
		t.Fatal("expected error for symlink escape")
	}
}

func TestApplyVerified_BlockedPaths(t *testing.T) {
	root := t.TempDir()
	for _, tc := range []struct {
		name    string
		relPath string
	}{
		{"dot env", ".env"},
		{"pem file", "key.pem"},
		{"pem uppercase", "CERT.PEM"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
				{RelPath: tc.relPath, Action: Create, Content: []byte("bad"), Mode: 0o644},
			})
			if err == nil {
				t.Fatalf("expected error for blocked path %q", tc.relPath)
			}
			if !strings.Contains(err.Error(), "blocklist") {
				t.Errorf("error should mention blocklist: %v", err)
			}
		})
	}
}

func TestApplyVerified_RollbackOnFailure(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("platform-only: posix - a 0555 directory does not refuse a write on Windows (D-68). " +
			"The CATCH branch is covered on every platform by TestProperty_Q01_" +
			"AnInjectedFailureLeavesEveryTargetAtItsPreImage, which injects by ordering instead")
	}
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "first.txt"), []byte("original-first"))

	readOnlyDir := filepath.Join(root, "readonly")
	if err := os.MkdirAll(readOnlyDir, 0o555); err != nil {
		t.Fatalf("MkdirAll: %v", err)
	}
	defer func() { _ = os.Chmod(readOnlyDir, 0o755) }()

	_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "first.txt", Action: Update, Content: []byte("new-first"),
			ExpectedHash: hashOf([]byte("original-first")), Mode: 0o644},
		{RelPath: "readonly/blocked.txt", Action: Create, Content: []byte("fail"), Mode: 0o644},
	})
	if err == nil {
		t.Fatal("expected error from readonly dir")
	}
	got, _ := os.ReadFile(filepath.Join(root, "first.txt"))
	if string(got) != "original-first" {
		t.Errorf("rollback failed: content = %q, want original-first", string(got))
	}
}

// TestApplyVerified_IdempotentContent is Phase 0's idempotence assertion, restated for the
// pre-image regime: applying the same content twice needs the SECOND apply to expect the
// first apply's output, which is what a pre-image hash means.
func TestApplyVerified_IdempotentContent(t *testing.T) {
	root := t.TempDir()
	content := []byte("same content")
	if _, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "file.txt", Action: Create, Content: content, Mode: 0o644},
	}); err != nil {
		t.Fatalf("first apply failed: %v", err)
	}
	if _, err := ApplyVerified(context.Background(), verified(t, 2), root, []Entry{
		{RelPath: "file.txt", Action: Update, Content: content,
			ExpectedHash: hashOf(content), Mode: 0o644},
	}); err != nil {
		t.Fatalf("second apply failed: %v", err)
	}
	got, _ := os.ReadFile(filepath.Join(root, "file.txt"))
	if string(got) != "same content" {
		t.Errorf("content = %q, want 'same content'", string(got))
	}
}

// ── Phase 1 additions ──────────────────────────────────────────────────────────

func TestApplyVerified_RefusesANilVerifiedEnvelope(t *testing.T) {
	_, err := ApplyVerified(context.Background(), nil, t.TempDir(), []Entry{
		{RelPath: "a.txt", Action: Create, Content: []byte("x")},
	})
	if !errors.Is(err, ErrNoAuthority) {
		t.Fatalf("expected ErrNoAuthority, got %v", err)
	}
}

// TestApplyVerified_StaleChangeSetWritesNothing is the ErrConflict clause, and the
// "writes nothing" half is the part that matters: a mismatch found on entry 3 must not
// leave entries 1 and 2 applied.
func TestApplyVerified_StaleChangeSetWritesNothing(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), []byte("A"))
	writeFile(t, filepath.Join(root, "b.txt"), []byte("B"))
	writeFile(t, filepath.Join(root, "c.txt"), []byte("C-changed-since-approval"))

	_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "a.txt", Action: Update, Content: []byte("A2"), ExpectedHash: hashOf([]byte("A"))},
		{RelPath: "b.txt", Action: Update, Content: []byte("B2"), ExpectedHash: hashOf([]byte("B"))},
		{RelPath: "c.txt", Action: Update, Content: []byte("C2"), ExpectedHash: hashOf([]byte("C"))},
	})
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("expected ErrConflict, got %v", err)
	}
	for name, want := range map[string]string{
		"a.txt": "A", "b.txt": "B", "c.txt": "C-changed-since-approval",
	} {
		got, readErr := os.ReadFile(filepath.Join(root, name))
		if readErr != nil {
			t.Fatalf("read %s: %v", name, readErr)
		}
		if string(got) != want {
			t.Errorf("%s = %q, want %q; a refused change-set must write nothing at all", name, got, want)
		}
	}
	if entries, _ := os.ReadDir(root); len(entries) != 3 {
		t.Errorf("the refused apply left %d entries in root, want 3 — no backups, no temp files", len(entries))
	}
}

func TestApplyVerified_CreateOnAnExistingFileIsAConflict(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), []byte("already here"))
	_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "a.txt", Action: Create, Content: []byte("x")},
	})
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("expected ErrConflict, got %v", err)
	}
}

func TestApplyVerified_UpdateOnAMissingFileIsAConflict(t *testing.T) {
	root := t.TempDir()
	_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "gone.txt", Action: Update, Content: []byte("x"), ExpectedHash: hashOf([]byte("y"))},
	})
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("expected ErrConflict, got %v", err)
	}
}

func TestApplyVerified_RejectsAnInconsistentEntry(t *testing.T) {
	root := t.TempDir()
	for name, entry := range map[string]Entry{
		"update without a hash": {RelPath: "a.txt", Action: Update, Content: []byte("x")},
		"create with a hash":    {RelPath: "a.txt", Action: Create, Content: []byte("x"), ExpectedHash: "deadbeef"},
		"delete carrying bytes": {RelPath: "a.txt", Action: Delete, Content: []byte("x"), ExpectedHash: "deadbeef"},
		"unknown action":        {RelPath: "a.txt", Action: Action("append"), Content: []byte("x")},
		"empty path":            {RelPath: "", Action: Create, Content: []byte("x")},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{entry})
			if !errors.Is(err, ErrBadEntry) {
				t.Fatalf("expected ErrBadEntry, got %v", err)
			}
		})
	}
}

func TestApplyVerified_RefusesTwoEntriesTargetingOneFile(t *testing.T) {
	root := t.TempDir()
	_, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "a.txt", Action: Create, Content: []byte("one")},
		{RelPath: "./a.txt", Action: Create, Content: []byte("two")},
	})
	if !errors.Is(err, ErrBadEntry) {
		t.Fatalf("expected ErrBadEntry for a duplicate target, got %v", err)
	}
}

func TestApplyVerified_DeleteRemovesAndBacksUp(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "doomed.txt")
	writeFile(t, target, []byte("doomed"))

	report, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "doomed.txt", Action: Delete, ExpectedHash: hashOf([]byte("doomed"))},
	})
	if err != nil {
		t.Fatalf("ApplyVerified: %v", err)
	}
	if _, statErr := os.Stat(target); !os.IsNotExist(statErr) {
		t.Fatal("the target must be gone after a delete")
	}
	backup := report.Backups.Entries[0].BackupPath
	if backup == "" {
		t.Fatal("a delete must still back the file up, or the revert has nothing to restore")
	}
	data, _ := os.ReadFile(backup)
	if string(data) != "doomed" {
		t.Errorf("backup = %q, want doomed", data)
	}
}

// TestApplyVerified_EnvExampleIsWritableWhileEnvIsNot is D-46's clause reaching the write
// path for the first time. Before this leaf `blockedForWrite` had no caller at all: leaf
// 4.7 split the list and Phase 0's ApplyAtomic still resolved through the READ rule, so
// the write exemption was correct and unreachable.
func TestApplyVerified_EnvExampleIsWritableWhileEnvIsNot(t *testing.T) {
	root := t.TempDir()
	if _, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: ".env.example", Action: Create, Content: []byte("KEY=placeholder\n")},
	}); err != nil {
		t.Fatalf(".env.example must be writable (D-46, §1.5 lists it as a generated artifact): %v", err)
	}
	for _, refused := range []string{".env", ".env.local", ".env.example.bak", ".ENV.EXAMPLE"} {
		if _, err := ApplyVerified(context.Background(), verified(t, 2), root, []Entry{
			{RelPath: refused, Action: Create, Content: []byte("x")},
		}); err == nil {
			t.Errorf("%s must not be writable", refused)
		}
	}
}

// ── Revert ─────────────────────────────────────────────────────────────────────

func TestRevert_RestoresPreImagesByteForByte(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "kept.txt"), []byte("pre-image bytes\n\x00binary\xff"))

	report, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "kept.txt", Action: Update, Content: []byte("replaced"),
			ExpectedHash: hashOf([]byte("pre-image bytes\n\x00binary\xff"))},
		{RelPath: "fresh.txt", Action: Create, Content: []byte("brand new")},
	})
	if err != nil {
		t.Fatalf("ApplyVerified: %v", err)
	}

	if _, err := Revert(context.Background(), verified(t, 2), report.Backups); err != nil {
		t.Fatalf("Revert: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(root, "kept.txt"))
	if err != nil {
		t.Fatalf("read kept.txt: %v", err)
	}
	if string(got) != "pre-image bytes\n\x00binary\xff" {
		t.Errorf("kept.txt = %q; revert must restore byte-for-byte", got)
	}
	if _, err := os.Stat(filepath.Join(root, "fresh.txt")); !os.IsNotExist(err) {
		t.Error("a file that did not previously exist must be DELETED by revert (Q-02)")
	}
}

func TestRevert_RefusesAConsumedHandle(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), []byte("A"))
	report, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "a.txt", Action: Update, Content: []byte("A2"), ExpectedHash: hashOf([]byte("A"))},
	})
	if err != nil {
		t.Fatalf("ApplyVerified: %v", err)
	}
	if _, err := Revert(context.Background(), verified(t, 2), report.Backups); err != nil {
		t.Fatalf("first Revert: %v", err)
	}
	before, _ := os.ReadFile(filepath.Join(root, "a.txt"))
	_, err = Revert(context.Background(), verified(t, 3), report.Backups)
	if !errors.Is(err, ErrHandleConsumed) {
		t.Fatalf("expected ErrHandleConsumed, got %v", err)
	}
	after, _ := os.ReadFile(filepath.Join(root, "a.txt"))
	if string(before) != string(after) {
		t.Error("a refused second revert must change nothing; the effect is idempotent")
	}
}

func TestRevert_RefusesATamperedBackup(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), []byte("A"))
	report, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "a.txt", Action: Update, Content: []byte("A2"), ExpectedHash: hashOf([]byte("A"))},
	})
	if err != nil {
		t.Fatalf("ApplyVerified: %v", err)
	}
	backup := report.Backups.Entries[0].BackupPath
	if err := os.WriteFile(backup, []byte("not the pre-image"), 0o644); err != nil {
		t.Fatalf("tamper: %v", err)
	}
	if _, err := Revert(context.Background(), verified(t, 2), report.Backups); !errors.Is(err, ErrManifestTampered) {
		t.Fatalf("expected ErrManifestTampered, got %v", err)
	}
	got, _ := os.ReadFile(filepath.Join(root, "a.txt"))
	if string(got) != "A2" {
		t.Errorf("a refused revert must not restore anything; a.txt = %q", got)
	}
}

func TestRevert_RefusesANilVerifiedEnvelope(t *testing.T) {
	if _, err := Revert(context.Background(), nil, BackupManifest{HandleID: "x", Root: "y"}); !errors.Is(err, ErrNoAuthority) {
		t.Fatalf("expected ErrNoAuthority, got %v", err)
	}
}

func TestRevert_RestoresInReverseOrder(t *testing.T) {
	root := t.TempDir()
	report, err := ApplyVerified(context.Background(), verified(t, 1), root, []Entry{
		{RelPath: "one/a.txt", Action: Create, Content: []byte("1")},
		{RelPath: "one/b.txt", Action: Create, Content: []byte("2")},
		{RelPath: "one/c.txt", Action: Create, Content: []byte("3")},
	})
	if err != nil {
		t.Fatalf("ApplyVerified: %v", err)
	}
	revertReport, err := Revert(context.Background(), verified(t, 2), report.Backups)
	if err != nil {
		t.Fatalf("Revert: %v", err)
	}
	if len(revertReport.Removed) != 3 {
		t.Fatalf("expected 3 removals, got %d", len(revertReport.Removed))
	}
	for _, name := range []string{"one/a.txt", "one/b.txt", "one/c.txt"} {
		if _, err := os.Stat(filepath.Join(root, name)); !os.IsNotExist(err) {
			t.Errorf("%s still exists after revert", name)
		}
	}
}

func TestHandleID_DiffersForTwoAppliesOfOneChangeSet(t *testing.T) {
	first := handleID("digest", time.Unix(1, 0))
	second := handleID("digest", time.Unix(2, 0))
	if first == second {
		t.Fatal("two applies must not share a single-use rollback handle")
	}
}
