// SPDX-License-Identifier: Apache-2.0

// Q-02's negative control, applied by `go build -overlay` (design §0.4.5, Appendix B Q-02).
//
// Appendix B words the control as "make `Revert` skip entries marked `NO_PREVIOUS`". This file
// replaces `agent/internal/executor/internal/mutate/revert_entry.go` with a version whose
// `NoPrevious()` branch returns without removing anything: `Revert` still walks every entry, still
// restores every pre-image, and still reports success — but a file the apply CREATED survives the
// revert. The user's tree is then not the pre-image, which is exactly the failure Q-02's
// "including deleting files that did not previously exist" clause exists to make impossible.
//
// It is a WHOLE-FILE replacement because that is what `-overlay` does, and it is a *small* file
// because `revertOne` was extracted into one for this reason (see the note in the real
// `revert_entry.go`). The signature is identical, so if `revertOne`'s shape ever changes this file
// stops compiling rather than silently ceasing to mutate anything — which is the failure mode that
// makes a negative control decorative.
//
// WHY `report.Removed` IS STILL APPENDED TO. The skip is of the FILESYSTEM effect only. Leaving
// the bookkeeping in place means the mutated `Revert` still claims to have removed the file, so the
// property fails on the ground truth (the file is on disk) rather than on a report that
// conveniently confesses. A control that also stopped reporting would be caught by the weaker of
// the two assertions and would not prove the on-disk one was doing any work.
//
// This file is never built by the ordinary `go build ./...`: it is not under the module's package
// tree and is only reachable through the overlay JSON the mutation harness writes.
package mutate

import (
	"fmt"
	"path/filepath"
)

// revertOne no longer removes the file an apply created. THE MUTATION.
func revertOne(entry BackupEntry, report *RevertReport) error {
	if entry.NoPrevious() {
		// `os.Remove(entry.AbsPath)` and its `fsyncDir` are gone. The entry is accounted for and
		// nothing on disk changes.
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
