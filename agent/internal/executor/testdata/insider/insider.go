// SPDX-License-Identifier: Apache-2.0

// Package insider is a boundary FIXTURE, not product code.
//
// It sits under `agent/internal/executor/` and imports `executor/internal/mutate`, so it
// must compile. Its counterpart, `agent/testdata/chokepoint/outsider`, sits outside the
// executor subtree and must NOT compile.
//
// Both live under a `testdata` directory so `./...` never builds them as part of the
// module, and `agent/internal/executor/internal/mutate/boundary_test.go` builds each one
// by explicit path. Having BOTH is what makes the pair evidence: a negative-only test
// would pass if `mutate` failed to compile for any unrelated reason.
package insider

import (
	"context"

	"github.com/parag8487/ForgeOps/agent/internal/executor/internal/mutate"
)

// Reachable references a real symbol, so the import cannot be elided by a compiler that
// notices it is unused.
func Reachable() error {
	_, err := mutate.ApplyVerified(context.Background(), nil, "", nil)
	return err
}
