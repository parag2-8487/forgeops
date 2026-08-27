// SPDX-License-Identifier: Apache-2.0
package app

import (
	"os"
	"path/filepath"
	"testing"
)

// The rule that decides whether a batch can be handled incrementally at all.
//
// WHY IT IS WORTH ITS OWN TEST. Running watch mode against a real workspace found that a DELETION
// produced an empty incremental report, which the backend refused with a 422 — so the deleted file
// stayed in the index permanently. The cause is structural rather than a slip: `BuildIncrementalReport`
// derives its closure from a fresh scan of the tree as it is NOW, and a file that is gone is both
// absent from that scan and no longer the target of any resolvable specifier, so the closure is empty.
// Finding its dependants would need the previous graph, which the agent does not keep.
//
// This is the predicate that routes those batches to a full re-index instead.
func TestMissingPaths_NamesOnlyWhatIsGone(t *testing.T) {
	root := t.TempDir()
	for _, rel := range []string{"present.js", filepath.Join("sub", "also-present.js")} {
		full := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatalf("mkdir: %v", err)
		}
		if err := os.WriteFile(full, []byte("x\n"), 0o644); err != nil {
			t.Fatalf("write %s: %v", rel, err)
		}
	}

	got := missingPaths(root, []string{"present.js", "sub/also-present.js", "deleted.js", "sub/gone.js"})

	want := map[string]bool{"deleted.js": true, "sub/gone.js": true}
	if len(got) != len(want) {
		t.Fatalf("want %d missing, got %d: %v", len(want), len(got), got)
	}
	for _, p := range got {
		if !want[p] {
			t.Errorf("%s exists and must not be reported missing", p)
		}
	}
}

func TestMissingPaths_AnEmptyBatchIsNotADeletion(t *testing.T) {
	// The distinction matters: an empty result must mean "handle this incrementally", so a batch with
	// nothing missing cannot be allowed to trigger a full re-index of the whole tree.
	if got := missingPaths(t.TempDir(), nil); len(got) != 0 {
		t.Errorf("want nothing missing for an empty batch, got %v", got)
	}
}

func TestMissingPaths_ADirectoryThatStillExistsIsNotMissing(t *testing.T) {
	// fsnotify reports directory events too, and a directory is not a deleted file. Reporting one as
	// missing would send every mkdir through a full re-index.
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "newdir"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if got := missingPaths(root, []string{"newdir"}); len(got) != 0 {
		t.Errorf("an existing directory must not be reported missing, got %v", got)
	}
}

// The verb's SHAPE, constructed directly.
//
// `NewRootCommand(&App{})` cannot be used here: `newPairCmd` dereferences collaborators while building
// itself, so an empty App panics before the tree exists. Building this one command is enough for what
// is being asserted — `RunE` is never invoked, so no collaborator is touched.
func TestTheWatchCommandDeclaresTheFlagsItsCallersUse(t *testing.T) {
	cmd := newWatchCmd(&App{})

	if cmd.Name() != "watch" {
		t.Errorf("want the verb to be named watch, got %q", cmd.Name())
	}
	// `--project` because the index is per project; `--debounce` because the quiet window is the
	// whole point of coalescing; `--once` because the live demonstration needs to observe exactly one
	// re-index without leaving a daemon running.
	for _, flag := range []string{"project", "debounce", "once"} {
		if cmd.Flags().Lookup(flag) == nil {
			t.Errorf("watch must accept --%s", flag)
		}
	}
	// The default has to be a real quiet window rather than zero: `NewDebouncedWatcher` would
	// substitute 250ms for a zero, so a zero default would mean the flag's stated default lied.
	if d := cmd.Flags().Lookup("debounce").DefValue; d == "0" || d == "" {
		t.Errorf("the debounce default must be a real window, got %q", d)
	}
}

func TestTheWatchCommandRefusesWithoutAProject(t *testing.T) {
	// Refusing BY NAME rather than defaulting is what keeps a watch from silently indexing into
	// whichever project happened to be first.
	cmd := newWatchCmd(&App{})
	cmd.SetArgs([]string{})
	cmd.SilenceUsage = true
	cmd.SilenceErrors = true

	err := cmd.Execute()
	if err == nil {
		t.Fatal("watch without --project must fail")
	}
	if indexOf(err.Error(), "--project") < 0 {
		t.Errorf("the refusal should name the missing flag, got: %v", err)
	}
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}
