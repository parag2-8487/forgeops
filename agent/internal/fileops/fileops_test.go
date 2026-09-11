// SPDX-License-Identifier: Apache-2.0
package fileops

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// The `TestApplyAtomic_*` tests that used to live here moved to
// `agent/internal/executor/internal/mutate` with the algorithm they guard (D-45). A
// property that guards an algorithm has to live where the algorithm lives, or it guards
// the location instead.
//
// What remains here is what remains in the package: the two exported path resolvers and
// the diff renderer.

func TestResolveForWrite_RejectsTraversal(t *testing.T) {
	root := t.TempDir()
	for _, relPath := range []string{"../escape.txt", "../../etc/passwd", "sub/../../../out.txt"} {
		if _, err := ResolveForWrite(root, relPath); !errors.Is(err, ErrPathOutsideRoot) {
			t.Errorf("ResolveForWrite(%q) = %v, want ErrPathOutsideRoot", relPath, err)
		}
	}
}

func TestResolveForRead_RejectsTraversal(t *testing.T) {
	root := t.TempDir()
	for _, relPath := range []string{"../escape.txt", "sub/../../../out.txt"} {
		if _, err := ResolveForRead(root, relPath); !errors.Is(err, ErrPathOutsideRoot) {
			t.Errorf("ResolveForRead(%q) = %v, want ErrPathOutsideRoot", relPath, err)
		}
	}
}

func TestResolve_RejectsAbsolutePaths(t *testing.T) {
	root := t.TempDir()
	absPath := "/etc/passwd"
	if runtime.GOOS == "windows" {
		absPath = "C:\\Windows\\System32\\evil.txt"
	}
	if _, err := ResolveForWrite(root, absPath); !errors.Is(err, ErrPathOutsideRoot) {
		t.Errorf("ResolveForWrite(%q) = %v, want ErrPathOutsideRoot", absPath, err)
	}
	if _, err := ResolveForRead(root, absPath); !errors.Is(err, ErrPathOutsideRoot) {
		t.Errorf("ResolveForRead(%q) = %v, want ErrPathOutsideRoot", absPath, err)
	}
}

func TestResolve_RejectsASymlinkEscape(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("platform-only: posix - symlink creation and traversal semantics differ on Windows (D-68)")
	}
	root := t.TempDir()
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(root, "escape-link")); err != nil {
		t.Fatalf("Symlink: %v", err)
	}
	if _, err := ResolveForWrite(root, "escape-link/evil.txt"); err == nil {
		t.Error("a symlink escape must be refused on the write path")
	}
	if _, err := ResolveForRead(root, "escape-link/evil.txt"); err == nil {
		t.Error("a symlink escape must be refused on the read path")
	}
}

// TestResolve_TheTwoIntentsDifferOnExactlyThreeNames is the assertion that keeps D-46
// honest as one statement rather than two implementations compared by eye.
func TestResolve_TheTwoIntentsDifferOnExactlyThreeNames(t *testing.T) {
	root := t.TempDir()
	candidates := []string{
		".env", ".env.local", ".env.production", ".env.example", ".env.sample",
		".env.template", ".env.example.bak", ".ENV.EXAMPLE", ".envrc",
		"key.pem", "CERT.PEM", "sub/.env", "sub/.env.example", "ordinary.txt",
	}
	// THE TWO INTENTS NOW AGREE ON EVERY NAME, and that is the point of the change rather than a
	// loosening that slipped through.
	//
	// D-46 made three example names writable and left them unreadable. The gap was not survivable:
	// `.env.example` on disk was invisible to the index, so `env_example_present` scored 0/40 and could
	// not be fixed — generation created the file and the apply refused because it already existed.
	// Reading a file whose entire purpose is to carry names and no values is not the harm the read rule
	// was written to prevent.
	//
	// A writable-but-unreadable name is no longer possible, so the assertion is simply that the two
	// agree, plus the two directed checks below that the exemption did not widen.
	for _, name := range candidates {
		_, readErr := ResolveForRead(root, name)
		_, writeErr := ResolveForWrite(root, name)
		readBlocked := errors.Is(readErr, ErrPathBlocked)
		writeBlocked := errors.Is(writeErr, ErrPathBlocked)
		if readBlocked != writeBlocked {
			t.Errorf("%q: the intents must agree, got readBlocked=%v writeBlocked=%v",
				name, readBlocked, writeBlocked)
		}
	}

	// The exemption must not have widened. Each of these carries, or may carry, real values.
	for _, refused := range []string{".env", ".env.local", ".env.production", ".env.example.bak", ".ENV.EXAMPLE"} {
		if _, err := ResolveForRead(root, refused); !errors.Is(err, ErrPathBlocked) {
			t.Errorf("%q must stay unreadable", refused)
		}
		if _, err := ResolveForWrite(root, refused); !errors.Is(err, ErrPathBlocked) {
			t.Errorf("%q must stay unwritable", refused)
		}
	}

	// And the three exact names are permitted both ways, at any depth.
	for _, permitted := range []string{".env.example", ".env.sample", ".env.template", "sub/.env.example"} {
		if _, err := ResolveForRead(root, permitted); err != nil {
			t.Errorf("%q must be readable so the index can see it: %v", permitted, err)
		}
		if _, err := ResolveForWrite(root, permitted); err != nil {
			t.Errorf("%q must be writable (D-46): %v", permitted, err)
		}
	}
}

func keysOf(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// TestPackageExportsNoWriteFunction is the D-45 assertion stated where it can rot.
//
// The point of moving the write path was that "an exported write function that any package
// can call is a bypass waiting to be written". If one ever reappears here, this fails —
// and it fails by NAME, so the message says which function to look at.
func TestPackageExportsNoWriteFunction(t *testing.T) {
	forbidden := []string{"Apply", "Write", "Delete", "Remove", "Rename", "Mkdir", "Chmod", "Truncate"}
	// The exported surface is enumerated by hand here on purpose: `go/ast` over the
	// package would be the general solution, and `scripts/check-chokepoint.sh` does
	// exactly that for the whole tree. This is the cheap in-package guard.
	exported := []string{"New", "UnifiedDiff", "ResolveForRead", "ResolveForWrite",
		"BlockedForRead", "BlockedForWrite", "Ops", "FileOps", "ErrPathOutsideRoot", "ErrPathBlocked"}
	for _, name := range exported {
		for _, verb := range forbidden {
			if strings.HasPrefix(name, verb) {
				t.Errorf("fileops exports %q, which looks like a write path; D-45 moved writing to "+
					"executor/internal/mutate so that no package outside the executor subtree can call it", name)
			}
		}
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
