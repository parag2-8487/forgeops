// SPDX-License-Identifier: Apache-2.0

package mutate

import "os"

// backupInfo is one row of the in-flight backup bookkeeping ApplyVerified keeps while it
// writes. It is not the durable handle — that is BackupManifest — and it exists only for the
// duration of one apply, because rollback needs to know whether each target had a pre-image.
type backupInfo struct {
	path    string
	existed bool
}

// rollback undoes completed writes in reverse order.
//
// Phase 0's function, unchanged in behaviour.
//
// WHY IT LIVES IN ITS OWN FILE. This is the clause Appendix B's Q-01 negative control removes
// ("remove the rollback loop from the CATCH branch"), and `go build -overlay` replaces a whole
// FILE. With this function inside `apply.go` the control would have to carry a copy of nearly six
// hundred lines, which would rot on the first edit to anything else in that file and would make
// the mutation impossible to read. Here the overlay is this file with an empty loop, so the diff a
// reviewer compares is four lines — and if the signature ever changes, the overlay stops
// compiling rather than silently ceasing to mutate anything.
//
// Reverse order because the forward loop wrote in ascending order: undoing a create before the
// directory it was created in is the only order that leaves nothing behind.
//
// It ignores its own errors deliberately. It runs on an error path, and there is nothing useful to
// do with a failure to restore beyond leaving the backup file in place — which it does by not
// removing it. A rollback that panicked would replace a recoverable half-applied set with a dead
// agent.
func rollback(written []string, backups []backupInfo) {
	for i := len(written) - 1; i >= 0; i-- {
		if i < len(backups) && backups[i].existed {
			_ = os.Rename(backups[i].path, written[i])
			continue
		}
		_ = os.Remove(written[i])
	}
}
