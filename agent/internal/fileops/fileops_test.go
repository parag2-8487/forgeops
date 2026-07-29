// SPDX-License-Identifier: Apache-2.0
package fileops

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestApplyAtomic_Basic(t *testing.T) {
	root := t.TempDir()

	ops := New()
	report, err := ops.ApplyAtomic(context.Background(), root, []WriteEntry{
		{RelPath: "hello.txt", Content: []byte("hello world"), Mode: 0o644},
		{RelPath: "sub/nested.txt", Content: []byte("nested content"), Mode: 0o644},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(report.Written) != 2 {
		t.Errorf("written = %d, want 2", len(report.Written))
	}

	// Verify content
	got, _ := os.ReadFile(filepath.Join(root, "hello.txt"))
	if string(got) != "hello world" {
		t.Errorf("content = %q", string(got))
	}
	got, _ = os.ReadFile(filepath.Join(root, "sub", "nested.txt"))
	if string(got) != "nested content" {
		t.Errorf("nested content = %q", string(got))
	}
}

func TestApplyAtomic_BackupBeforeMutate(t *testing.T) {
	root := t.TempDir()
	existing := filepath.Join(root, "exists.txt")
	os.WriteFile(existing, []byte("original"), 0o644)

	ops := New()
	report, err := ops.ApplyAtomic(context.Background(), root, []WriteEntry{
		{RelPath: "exists.txt", Content: []byte("updated"), Mode: 0o644},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(report.Backups) != 1 {
		t.Fatalf("expected 1 backup, got %d", len(report.Backups))
	}

	// Verify backup contains original
	backupData, _ := os.ReadFile(report.Backups[0])
	if string(backupData) != "original" {
		t.Errorf("backup content = %q, want original", string(backupData))
	}

	// Verify new content
	got, _ := os.ReadFile(existing)
	if string(got) != "updated" {
		t.Errorf("updated content = %q", string(got))
	}
}

func TestApplyAtomic_TraversalRejected(t *testing.T) {
	root := t.TempDir()
	ops := New()

	_, err := ops.ApplyAtomic(context.Background(), root, []WriteEntry{
		{RelPath: "../escape.txt", Content: []byte("bad"), Mode: 0o644},
	})
	if err == nil {
		t.Fatal("expected error for path traversal")
	}
	if !strings.Contains(err.Error(), "outside root") {
		t.Errorf("error should mention outside root: %v", err)
	}
}

func TestApplyAtomic_AbsolutePathRejected(t *testing.T) {
	root := t.TempDir()
	ops := New()

	var absPath string
	if runtime.GOOS == "windows" {
		absPath = "C:\\Windows\\System32\\evil.txt"
	} else {
		absPath = "/etc/passwd"
	}

	_, err := ops.ApplyAtomic(context.Background(), root, []WriteEntry{
		{RelPath: absPath, Content: []byte("bad"), Mode: 0o644},
	})
	if err == nil {
		t.Fatal("expected error for absolute path")
	}
}

func TestApplyAtomic_SymlinkEscape(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlink test requires Unix")
	}

	root := t.TempDir()
	outside := t.TempDir()

	// Create a symlink inside root that points outside
	link := filepath.Join(root, "escape-link")
	os.Symlink(outside, link)

	ops := New()
	_, err := ops.ApplyAtomic(context.Background(), root, []WriteEntry{
		{RelPath: "escape-link/evil.txt", Content: []byte("bad"), Mode: 0o644},
	})
	if err == nil {
		t.Fatal("expected error for symlink escape")
	}
}

func TestApplyAtomic_BlockedPaths(t *testing.T) {
	root := t.TempDir()
	ops := New()

	cases := []struct {
		name    string
		relPath string
	}{
		{"dot env", ".env"},
		{"pem file", "key.pem"},
		{"pem uppercase", "CERT.PEM"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ops.ApplyAtomic(context.Background(), root, []WriteEntry{
				{RelPath: tc.relPath, Content: []byte("bad"), Mode: 0o644},
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

func TestApplyAtomic_RollbackOnFailure(t *testing.T) {
	root := t.TempDir()

	// Create existing file
	os.WriteFile(filepath.Join(root, "first.txt"), []byte("original-first"), 0o644)

	ops := New()
	// Second entry writes to a read-only directory to trigger failure
	readOnlyDir := filepath.Join(root, "readonly")
	os.MkdirAll(readOnlyDir, 0o555)
	defer os.Chmod(readOnlyDir, 0o755) // cleanup

	if runtime.GOOS == "windows" {
		t.Skip("read-only directory enforcement differs on Windows")
	}

	_, err := ops.ApplyAtomic(context.Background(), root, []WriteEntry{
		{RelPath: "first.txt", Content: []byte("new-first"), Mode: 0o644},
		{RelPath: "readonly/blocked.txt", Content: []byte("fail"), Mode: 0o644},
	})
	if err == nil {
		t.Fatal("expected error from readonly dir")
	}

	// first.txt should be rolled back to original
	got, _ := os.ReadFile(filepath.Join(root, "first.txt"))
	if string(got) != "original-first" {
		t.Errorf("rollback failed: content = %q, want original-first", string(got))
	}
}

func TestApplyAtomic_IdempotentContent(t *testing.T) {
	root := t.TempDir()
	ops := New()

	entries := []WriteEntry{
		{RelPath: "file.txt", Content: []byte("same content"), Mode: 0o644},
	}

	// Apply twice
	_, err := ops.ApplyAtomic(context.Background(), root, entries)
	if err != nil {
		t.Fatalf("first apply failed: %v", err)
	}
	_, err = ops.ApplyAtomic(context.Background(), root, entries)
	if err != nil {
		t.Fatalf("second apply failed: %v", err)
	}

	// Content is identical regardless
	got, _ := os.ReadFile(filepath.Join(root, "file.txt"))
	if string(got) != "same content" {
		t.Errorf("content = %q, want 'same content'", string(got))
	}
}

func TestUnifiedDiff(t *testing.T) {
	ops := New()
	before := "line1\nline2\nline3\n"
	after := "line1\nmodified\nline3\n"

	diff := ops.UnifiedDiff(before, after, "test.txt")
	if !strings.Contains(diff, "--- a/test.txt") {
		t.Error("diff should contain file header")
	}
	if !strings.Contains(diff, "+++ b/test.txt") {
		t.Error("diff should contain file header")
	}
	if !strings.Contains(diff, "-line2") {
		t.Error("diff should show deleted line")
	}
	if !strings.Contains(diff, "+modified") {
		t.Error("diff should show added line")
	}
}
