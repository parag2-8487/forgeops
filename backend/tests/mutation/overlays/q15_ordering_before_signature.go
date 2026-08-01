// SPDX-License-Identifier: Apache-2.0

// MUTATION OVERLAY — Q-15's negative control. Not part of the build.
//
// This is `agent/internal/envelope/order.go` with the ordering check moved AHEAD of the signature
// check, which is Appendix B's "update `last_seq` before the signature check" verbatim.
// `scripts/mutation-harness.py` swaps it in with `go build -overlay` for one run; nothing imports
// it.
//
// What the mutation does, stated as an attack rather than as a diff: `AdvanceSeq` is a
// compare-and-set, so running it first means an UNAUTHENTICATED caller — anyone who can put bytes
// on the socket — can push a device's high-water mark arbitrarily high before the signature is
// ever checked. Every subsequent envelope the real backend mints then carries a `seq` at or below
// that mark and is refused as replayed. A denial of service delivered through a check whose whole
// purpose is to prevent one, and the reason §10.4 states the ordering constraint explicitly rather
// than leaving it to the implementer's taste.
//
// Every clause of `TestProperty_Q15_NoRejectedEnvelopeAdvancesACounter` that involves a forged or
// tampered envelope then fails, because the mark moves on a rejection that never authenticated.
// `TestVerify_ABadSignatureDoesNotAdvanceSeq` and the eleven-row table in `rejection_test.go`
// object as well.
package envelope

import "context"

// Verify is MUTATED: ordering runs before the signature check.
//
// The docstring of the real function is deliberately not copied — a reader diffing the two files
// should see the reordering, not a wall of prose. The signature and the check helpers are
// identical, so a change to any of them stops the mutated build compiling rather than silently
// ceasing to mutate anything.
func (v *Verifier) Verify(ctx context.Context, raw []byte) (*Verified, error) {
	env, err := v.parse(raw)
	if err != nil {
		return nil, err
	}
	if err := v.checkFreshness(env); err != nil {
		return nil, err
	}
	// MUTATION: the seq compare-and-set, before anything has been authenticated.
	if err := v.checkOrdering(ctx, env); err != nil {
		return nil, err
	}
	digest, err := v.checkSignature(ctx, env)
	if err != nil {
		return nil, err
	}
	if err := v.checkPolicyBinding(ctx, env); err != nil {
		return nil, err
	}
	if err := v.checkUniqueness(ctx, env); err != nil {
		return nil, err
	}
	return &Verified{env: env, verifiedAt: v.now(), digest: digest}, nil
}
