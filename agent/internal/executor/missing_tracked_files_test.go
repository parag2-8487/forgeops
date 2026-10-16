// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// An uncommitted deletion changes what gets built, and the error it causes points at the wrong layer.
// These tests pin both halves: that it is detected, and that it is reported without repairing.
func TestMissingTrackedFilesDetectsUncommittedDeletion(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not on PATH")
	}

	dir := t.TempDir()
	git := func(args ...string) {
		t.Helper()
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t.invalid",
			"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@t.invalid")
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}

	// The shape that produced the report: a Vite project whose tsconfig references vite.config.ts,
	// and that file deleted from the working tree but never committed.
	if err := os.WriteFile(filepath.Join(dir, "tsconfig.node.json"),
		[]byte(`{"include":["vite.config.ts"]}`), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "vite.config.ts"),
		[]byte("export default {}\n"), 0644); err != nil {
		t.Fatal(err)
	}
	git("init", "-q")
	git("add", ".")
	git("commit", "-qm", "init")

	// A clean tree has nothing missing.
	if got := missingTrackedFiles(dir); len(got) != 0 {
		t.Errorf("a clean checkout reported missing files: %v", got)
	}

	// Delete the tracked file from the working tree, exactly as `git rm` without a commit does.
	if err := os.Remove(filepath.Join(dir, "vite.config.ts")); err != nil {
		t.Fatal(err)
	}

	got := missingTrackedFiles(dir)
	if len(got) != 1 || got[0] != "vite.config.ts" {
		t.Fatalf("expected [vite.config.ts], got %v", got)
	}
}

// The detection must never be the reason a deployment stops. A directory that is not a repository,
// or a machine without git, has to return cleanly rather than error.
func TestMissingTrackedFilesIsSilentOutsideARepository(t *testing.T) {
	dir := t.TempDir() // not a git repository
	if got := missingTrackedFiles(dir); len(got) != 0 {
		t.Errorf("a non-repository reported missing files: %v", got)
	}
}

// And it must not REPAIR anything. Restoring a file the user deleted is their decision; the check
// exists to report, so a regression that silently resurrected files would be worse than the bug.
func TestMissingTrackedFilesDoesNotRestoreAnything(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not on PATH")
	}
	dir := t.TempDir()
	git := func(args ...string) {
		t.Helper()
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t.invalid",
			"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@t.invalid")
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	path := filepath.Join(dir, "deleted.ts")
	if err := os.WriteFile(path, []byte("x\n"), 0644); err != nil {
		t.Fatal(err)
	}
	git("init", "-q")
	git("add", ".")
	git("commit", "-qm", "init")
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}

	_ = missingTrackedFiles(dir)

	if _, err := os.Stat(path); err == nil {
		t.Errorf("the check restored a file the user deleted; detection must not mutate the tree")
	}
}
