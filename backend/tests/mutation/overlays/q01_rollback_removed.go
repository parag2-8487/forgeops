// SPDX-License-Identifier: Apache-2.0

// Q-01's negative control, applied by `go build -overlay` (design §0.4.5, Appendix B Q-01).
//
// Appendix B words the control as "remove the rollback loop from the CATCH branch". This file
// replaces `agent/internal/executor/internal/mutate/rollback.go` with a version whose loop is
// gone: `ApplyVerified` still calls `rollback` on every error path, and the call now does nothing.
// A half-applied change-set therefore stays half-applied, which is exactly the failure Q-01 exists
// to make impossible.
//
// It is a WHOLE-FILE replacement because that is what `-overlay` does, and it is a *small* file
// because `rollback` was extracted into one for this reason (see the note in the real
// `rollback.go`). The overlay keeps `backupInfo` and the signature identical, so if either ever
// changes this file stops compiling rather than silently ceasing to mutate anything — which is the
// failure mode that makes a negative control decorative.
//
// This file is never built by the ordinary `go build ./...`: it is not under the module's package
// tree and is only reachable through the overlay JSON the mutation harness writes.
package mutate

import "os"

type backupInfo struct {
	path    string
	existed bool
}

// rollback no longer rolls anything back. THE MUTATION.
//
// The parameters are still consumed so the file compiles without `_ = written` noise and so a
// reader can see that the information needed to undo the writes was available and discarded.
func rollback(written []string, backups []backupInfo) {
	if len(written) == 0 && len(backups) == 0 {
		// Unreachable in practice; present only so `os` stays used and the two parameters are
		// referenced, keeping this file a minimal edit of the real one.
		_ = os.Remove("")
	}
}
