// SPDX-License-Identifier: Apache-2.0
package policy

import (
	"context"
	"errors"
	"testing"

	"pgregory.net/rapid"
)

// Property Q-07 (design Appendix B; tasks.md leaf 9.7).
//
//	∀ digest pairs: if the agent's bundle digest ≠ the envelope's `policy_context` digest,
//	the agent denies AND the chokepoint refuses to mint; no mutation occurs on either path.
//
// This file owns the AGENT half, which is Appendix B's first named target
// (`agent/internal/policy`). The chokepoint half lives in Python and is asserted by
// `backend/tests/unit/test_governance_envelope.py`, whose
// `test_no_digest_is_never_readable_as_any_digest` states the same rule from the minting side:
// `policy_context.bundle_digest` is required, so an envelope with no digest cannot be minted
// and therefore cannot be presented here at all.
//
// WHY THIS FILE DID NOT EXIST, AND WHAT THAT COST
//
// Leaf 9.7 was recorded `done` in PROGRESS.md with no file behind it. `verify.go` carries the
// comment "Q-07 is the property" against the comparison, and `evaluator.go`'s `ErrDrift` was
// written for it, so the code was built expecting this test and then shipped without it. The
// drift comparison — the single line that stops an agent evaluating a policy bundle the
// backend did not authorise — had no property-level coverage and no negative control.
//
// THE POSITIVE CONTROL IS LOAD-BEARING
//
// A test that only checks "mismatch ⇒ deny" passes just as well against a bundle that denies
// everything, or against an evaluator that has failed to load at all: both produce a deny for
// reasons that have nothing to do with the digest. So the fixture bundle is asserted to ALLOW
// under a matching digest before the property runs. If that baseline ever stops allowing, this
// test fails loudly rather than continuing to "pass" while proving nothing — which is the
// vacuity Appendix B's negative-control rule exists to catch, applied to this file's own
// premise.
func TestPropertyQ07_DigestDisagreementDeniesFailClosed(t *testing.T) {
	// A bundle whose decision is unconditionally `allow`. Anything this test observes as a
	// deny is therefore attributable to the digest comparison and to nothing else.
	regoCode := `package forgeops.governance

decision := {"result": "allow", "reason": "fixture bundle allows unconditionally"}
`

	bundleData, loadedDigest := createTestBundle(t, regoCode)

	evaluator := NewEvaluator()
	ctx := context.Background()
	if err := evaluator.Load(ctx, bundleData); err != nil {
		t.Fatalf("loading the fixture bundle failed: %v", err)
	}
	if got := evaluator.BundleDigest(); got != loadedDigest {
		t.Fatalf("loaded digest %q does not match the bundle's computed digest %q", got, loadedDigest)
	}

	// ── the positive control ──────────────────────────────────────────────────
	baseline, err := evaluator.Evaluate(ctx, map[string]interface{}{"operation": "apply"}, loadedDigest)
	if err != nil {
		t.Fatalf("the fixture bundle must evaluate cleanly when the digests agree, got: %v", err)
	}
	if baseline["result"] != "allow" {
		t.Fatalf(
			"the fixture bundle must ALLOW when the digests agree, got %q. Without this the "+
				"property below cannot distinguish a drift deny from a bundle that denies everything",
			baseline["result"],
		)
	}

	rapid.Check(t, func(rt *rapid.T) {
		// A hex digest of the right shape, so the comparison is exercised rather than
		// short-circuited by an obviously malformed value.
		envelopeDigest := rapid.StringMatching(`[0-9a-f]{64}`).Draw(rt, "envelopeDigest")

		// The input is generated too: the property is over digest pairs for ANY input, and a
		// fixed input would leave "the drift check runs before evaluation" unproven.
		operation := rapid.SampledFrom([]string{"apply", "revert", "deploy", "restart"}).Draw(rt, "operation")
		environment := rapid.SampledFrom([]string{"dev", "staging", "prod"}).Draw(rt, "environment")
		input := map[string]interface{}{
			"operation":   operation,
			"environment": environment,
		}

		result, err := evaluator.Evaluate(ctx, input, envelopeDigest)

		if envelopeDigest == loadedDigest {
			// Astronomically unlikely, but if rapid ever draws the real digest the property
			// says the opposite thing, and asserting it here keeps the branch honest.
			if errors.Is(err, ErrDrift) {
				rt.Fatalf("Q-07 violation: digests are EQUAL (%q) yet drift was reported", envelopeDigest)
			}
			return
		}

		// ── the property ──────────────────────────────────────────────────────
		if !errors.Is(err, ErrDrift) {
			rt.Fatalf(
				"Q-07 violation: agent digest %q ≠ envelope digest %q but the error was %v, not ErrDrift. "+
					"A mismatch that does not report drift is an agent evaluating a bundle the backend "+
					"did not authorise",
				loadedDigest, envelopeDigest, err,
			)
		}
		if result["result"] != "deny" {
			rt.Fatalf(
				"Q-07 violation: agent digest %q ≠ envelope digest %q returned %q, not deny",
				loadedDigest, envelopeDigest, result["result"],
			)
		}
		// "No mutation occurs": the decision carries deny and the drift reason, so no caller
		// can read an authorisation out of it. `Evaluate` is side-effect free by construction —
		// it holds a read lock and returns a fresh map — so there is no mutation to observe
		// here beyond the decision itself.
		if reason, ok := result["reason"].(string); !ok || reason != ErrDrift.Error() {
			rt.Fatalf("Q-07 violation: deny did not carry the drift reason, got %v", result["reason"])
		}
	})
}
