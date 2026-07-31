// SPDX-License-Identifier: Apache-2.0

package mutate

import (
	"fmt"
	"os"
	"path/filepath"
)

// revertOne undoes one manifest entry: restore its pre-image, or remove the file the apply
// created.
//
// WHY IT LIVES IN ITS OWN FILE. The `NoPrevious()` branch below is the clause Appendix B's Q-02
// negative control removes ("make `Revert` skip entries marked `NO_PREVIOUS`"), and
// `go build -overlay` replaces a whole FILE. With this step inline in `Revert` the overlay would
// have carried a copy of nearly six hundred lines of `apply.go`, rotting on the first unrelated
// edit. Here the overlay is this file with the removal branch turned into a `continue`, so the diff
// a reviewer compares is three lines — the same arrangement `rollback.go` has for Q-01, and for the
// same reason.
//
// It appends to `report` rather than returning a verdict, because the caller needs to know WHICH
// paths were restored and which removed, and threading two slices back through a return value
// would put the bookkeeping at the call site where the control is supposed to be readable.
func revertOne(entry BackupEntry, report *RevertReport) error {
	if entry.NoPrevious() {
		// The apply created this file, so reverting means removing it. Q-02's "including
		// deleting files that did not previously exist".
		//
		// `os.IsNotExist` is tolerated: a revert that runs after the user deleted the file
		// themselves has nothing to do, and failing there would make the handle unusable for the
		// entries that DO need restoring.
		if err := os.Remove(entry.AbsPath); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("remove %s: %w", entry.RelPath, err)
		}
		fsyncDir(filepath.Dir(entry.AbsPath))
		report.Removed = append(report.Removed, entry.RelPath)
		return nil
	}

	if err := copyFile(entry.BackupPath, entry.AbsPath); err != nil {
		return fmt.Errorf("restore %s: %w", entry.RelPath, err)
	}
	fsyncDir(filepath.Dir(entry.AbsPath))
	report.Restored = append(report.Restored, entry.RelPath)
	return nil
}
