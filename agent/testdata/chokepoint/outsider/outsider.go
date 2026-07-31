// SPDX-License-Identifier: Apache-2.0

// Package outsider is a boundary FIXTURE that must NOT compile.
//
// It sits outside `agent/internal/executor/**` and imports
// `agent/internal/executor/internal/mutate`. Go's nested-`internal` rule makes that a
// compile error, which is §2.2.1 mechanism 3 — the strongest enforcement available,
// because it needs no lint, no review step and no discipline.
//
// `boundary_test.go` asserts `go build` on this directory FAILS, and asserts the failure
// message names the internal rule. Asserting the message matters: a build that failed
// because of a typo here would otherwise be read as the boundary holding.
package outsider

import (
	"context"

	"github.com/parag8487/ForgeOps/agent/internal/executor/internal/mutate"
)

func Bypass() error {
	_, err := mutate.ApplyVerified(context.Background(), nil, "", nil)
	return err
}
