// SPDX-License-Identifier: Apache-2.0

// Package policy's interface obligations, stated in one greppable place (design §0.4.2).
//
// A compile-time assertion cannot rot: the compiler rechecks it on every build. It can only be absent,
// and an absent assertion is indistinguishable from a type that never implemented the interface — which
// is why `scripts/check-go-interface-assertions.sh` looks for it and refused this change until it existed.
package policy

import "github.com/parag8487/ForgeOps/agent/internal/executor"

// `Evaluator` is what makes FR-38's independent evaluation real. `executor.PolicySource` is declared by
// its consumer, and this is the assertion that the shipped evaluator satisfies it — so a signature change
// on either side is a compile error rather than a wiring failure discovered when a command is refused.
var _ executor.PolicySource = (*Evaluator)(nil)
