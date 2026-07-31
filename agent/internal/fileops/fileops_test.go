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
	// Collected as a SET of base names, because the exemption is a base-name rule: both
	// `.env.example` and `sub/.env.example` are exempt, and counting them separately would
	// make the assertion depend on how many directories the candidate list happens to use.
	differ := make(map[string]bool)
	for _, name := range candidates {
		_, readErr := ResolveForRead(root, name)
		_, writeErr := ResolveForWrite(root, name)
		readBlocked := errors.Is(readErr, ErrPathBlocked)
		writeBlocked := errors.Is(writeErr, ErrPathBlocked)
		if readBlocked != writeBlocked {
			if readBlocked && !writeBlocked {
				differ[filepath.Base(name)] = true
				continue
			}
			t.Errorf("%q is writable-but-unreadable, which is backwards", name)
		}
	}
	want := map[string]bool{".env.example": true, ".env.sample": true, ".env.template": true}
	if len(differ) != len(want) {
		t.Fatalf("the intents differ on %v; D-46 permits exactly %v", keysOf(differ), keysOf(want))
	}
	for name := range differ {
		if !want[name] {
			t.Errorf("%q is exempt for writing and is not one of D-46's three names", name)
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
