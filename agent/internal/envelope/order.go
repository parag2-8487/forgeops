// SPDX-License-Identifier: Apache-2.0

// The six-check order of §10.4, in its own file.
//
// Separated for the same reason `domain.go` and `rollback.go` are separated: `go build -overlay`
// replaces a whole file, and Q-15's negative control — "update `last_seq` before the signature
// check" — has to be a readable diff rather than a copy of three hundred lines that rots on the
// first unrelated edit. The ORDER is the thing this file holds, and the order is the property.
package envelope

import "context"

// Verify parses and checks raw, returning the only value that proves it passed.
//
// The order is §10.4's, and it short-circuits on the first failure:
//
//  1. schema      — required members present, `v` known, no unknown members, seq and
//     not_after integral, no float anywhere;
//  2. freshness   — now <= not_after, and not_after - now <= maxAge, with clockSkew
//     tolerated;
//  3. signature   — constant-time compare of HMAC-SHA256(key, prefix||0x00||JCS(e));
//  4. policy bind — policy_context.bundle_digest == the loaded bundle's digest (D-84);
//  5. ordering    — seq > lastSeq for this device, then lastSeq = seq atomically;
//  6. uniqueness  — nonce unseen in a set covering at least maxAge.
//
// Why 3 precedes the two that mutate, which is the part that is easy to get backwards: verifying
// order first would let an UNAUTHENTICATED attacker advance a device's seq high-water mark or
// burn a nonce, locking out the real backend. A denial of service through a check that was
// supposed to be a defence. Q-15's negative control is exactly this inversion.
//
// No failure path returns a non-nil *Verified, so no failure can reach the executor.
//
// One check from Appendix A.2 is NOT here, and its absence is deliberate rather than an
// oversight: A.2's step 6 rejects an `operation` outside §7.7's closed catalogue. This package
// is a leaf that cannot import `executor` (D-59), so the check lands where the table does —
// `executor.Dispatcher` (leaf 8.7), which also owns the `approval_id` requirement for the
// mutating half of §7.7's table (D-83). Until dispatch, an unknown operation is rejected in a
// different place, not in no place. Said out loud because a docstring that lists six checks and
// renumbers A.2's would leave a reader believing verification is complete.
//
// D-84: the POLICY BINDING check runs before ordering and uniqueness, which inverts A.2's
// numbering (4, 5, then 6) while preserving A.2's postcondition, "no mutation is performed on
// any failure path". Ordering and uniqueness are the two checks that mutate — the seq
// compare-and-set and the nonce record — so any non-mutating check placed after them turns its
// own rejection into a state change. A stale-bundle rejection used to consume the envelope's
// seq and burn its nonce, which is exactly the shape Q-15 forbids. The numbered order is not
// itself a property; the postcondition is.
func (v *Verifier) Verify(ctx context.Context, raw []byte) (*Verified, error) {
	env, err := v.parse(raw)
	if err != nil {
		return nil, err
	}
	if err := v.checkFreshness(env); err != nil {
		return nil, err
	}
	digest, err := v.checkSignature(ctx, env)
	if err != nil {
		return nil, err
	}
	// Non-mutating, and therefore before the two that mutate (D-84).
	if err := v.checkPolicyBinding(ctx, env); err != nil {
		return nil, err
	}
	if err := v.checkOrdering(ctx, env); err != nil {
		return nil, err
	}
	if err := v.checkUniqueness(ctx, env); err != nil {
		return nil, err
	}
	return &Verified{env: env, verifiedAt: v.now(), digest: digest}, nil
}
