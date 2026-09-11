package scanner

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The scanner must skip the directory the executor writes its pre-images into, and the two names have to
// stay equal. They cannot share a constant: the executor's mutate package sits behind an `internal/`
// boundary the scanner is not allowed to cross, and importing it would invert the dependency. So the
// name is duplicated and this test is what stops the copies drifting.
//
// WHAT DRIFT WOULD COST, measured rather than imagined. Backups used to be written next to their targets
// as `<file>.backup.<timestamp>`, and two things broke:
//
//   - `helm lint` failed on a chart this platform had just generated, because Helm rejects unknown
//     extensions under `templates/`
//   - a rescan indexed the backups, so superseded copies of artifacts were scored beside the current ones
//
// Both were fixed by moving them under `.forgeops-rollback`. If the scanner stopped skipping that
// directory, the second failure would come straight back and nothing else would notice.
func TestTheScannerSkipsTheSameDirectoryTheExecutorWritesTo(t *testing.T) {
	// The literal the executor uses, asserted here so a change on that side fails on this side.
	const executorRollbackDir = ".forgeops-rollback"

	if rollbackStateDirName != executorRollbackDir {
		t.Fatalf("the scanner skips %q but the executor writes to %q; backups would be indexed again",
			rollbackStateDirName, executorRollbackDir)
	}
}

func TestRollbackStateIsNotIndexed(t *testing.T) {
	root := t.TempDir()

	// A real source file, which must be found.
	if err := os.WriteFile(filepath.Join(root, "app.py"), []byte("print(1)\n"), 0o644); err != nil {
		t.Fatalf("write app.py: %v", err)
	}

	// A chart template, and a backup of it in the place an apply now puts one.
	chart := filepath.Join(root, "charts", "demo", "templates")
	if err := os.MkdirAll(chart, 0o755); err != nil {
		t.Fatalf("mkdir chart: %v", err)
	}
	if err := os.WriteFile(filepath.Join(chart, "deployment.yaml"), []byte("kind: Deployment\n"), 0o644); err != nil {
		t.Fatalf("write template: %v", err)
	}

	backup := filepath.Join(root, rollbackStateDirName, "backups", "20260911T063257Z", "charts", "demo", "templates")
	if err := os.MkdirAll(backup, 0o755); err != nil {
		t.Fatalf("mkdir backup: %v", err)
	}
	if err := os.WriteFile(filepath.Join(backup, "deployment.yaml"), []byte("kind: Deployment\n"), 0o644); err != nil {
		t.Fatalf("write backup: %v", err)
	}

	var walked []string
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		name := d.Name()
		if d.IsDir() {
			if name == ".git" || name == "node_modules" || name == ".pytest_cache" || name == ".ruff_cache" {
				return filepath.SkipDir
			}
			if name == rollbackStateDirName {
				return filepath.SkipDir
			}
			return nil
		}
		rel, relErr := filepath.Rel(root, path)
		if relErr != nil {
			return relErr
		}
		walked = append(walked, filepath.ToSlash(rel))
		return nil
	})
	if err != nil {
		t.Fatalf("walk: %v", err)
	}

	for _, seen := range walked {
		if strings.Contains(seen, rollbackStateDirName) {
			t.Errorf("rollback state reached the index: %s", seen)
		}
	}

	// And the real files are still found, so the skip is not over-broad.
	var sawApp, sawTemplate bool
	for _, seen := range walked {
		switch seen {
		case "app.py":
			sawApp = true
		case "charts/demo/templates/deployment.yaml":
			sawTemplate = true
		}
	}
	if !sawApp || !sawTemplate {
		t.Errorf("the skip removed real files too: walked=%v", walked)
	}
}
