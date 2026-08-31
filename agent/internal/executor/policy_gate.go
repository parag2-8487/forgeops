// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
)

// The agent's own policy evaluation (FR-38), and why it can only ever add refusals.
//
// FR-38 is "the local agent evaluates the same policy independently and refuses if it disagrees". The
// backend's half was real — `OpaGovernancePolicy` queries a live OPA and writes a `policy_evaluations`
// row with `side="backend"`. The agent's half was a DIGEST COMPARISON: `envelope.Verifier` checks that
// `policy_context.bundle_digest` matches the bundle the agent holds and refuses a stale one, which
// proves the two sides hold the same rules and says nothing about whether they reach the same verdict.
// `policy.Evaluator` — real embedded OPA over the real bundle — existed and was reachable only from
// `cmd/evalhelper`, a debugging binary.
//
// THE INFORMATION ASYMMETRY, STATED, BECAUSE IT DETERMINES THE DESIGN
// -------------------------------------------------------------------
// The backend evaluates with `input.project.*` — the merged parameters of the project's stored policies.
// Those do not travel in the envelope, so the agent cannot reconstruct them. Pretending otherwise, or
// adding them to the envelope, would mean the agent is re-checking the backend's arithmetic using the
// backend's own numbers, which is not an independent check.
//
// What makes this sound is the direction of the asymmetry. `policies/agent/schedule.rego` and
// `paths.rego` are total over an ABSENT parameter — proven by
// `test_a_project_with_no_parameters_gets_a_defined_allow` — so evaluating without them yields a verdict
// that is *no stricter* than the backend's. Therefore:
//
//   - agent says DENY  → the rules deny on the operation and items alone, so the backend's allow was
//     wrong or its envelope was tampered with. REFUSE. This is FR-38's whole point.
//   - agent says ALLOW while the backend said DENY → the agent is missing the parameter that caused
//     the deny. The backend's decision stands, because the envelope carries it and the agent must not
//     be able to *widen* an authorisation.
//
// So a disagreement in the direction of refusal is honoured and a disagreement in the direction of
// permission is not. An agent that could overrule a deny would be a bypass, not a second opinion.

// PolicySource is the agent's own evaluator, declared here as the consumer needs it.
//
// AN INTERFACE RATHER THAN AN IMPORT of `internal/policy`, for the same reason `CodebaseIndexer` is one:
// `policy.Evaluator` links OPA's whole rego runtime, and putting that behind every `executor` test binary
// would make the dispatcher's tests depend on a bundle they do not exercise.
type PolicySource interface {
	// Evaluate returns the decision document for an input, or an error when it cannot.
	Evaluate(ctx context.Context, input map[string]any, envelopeDigest string) (map[string]any, error)
	// BundleDigest is the digest of the bundle currently loaded.
	BundleDigest() string
}

// ErrPolicyDisagreement is the refusal when the agent's own evaluation denies what it was sent.
//
// A distinct error rather than a generic refusal, because it is the single most important thing this
// agent can report: the backend authorised something its own rules forbid. An operator needs to be able
// to find every one of these.
var ErrPolicyDisagreement = errors.New("executor: the agent's own policy evaluation refuses this command")

// : Decisions the bundle can return. Anything else is treated as a deny, because an unrecognised verdict
// : from a policy engine is not a reason to proceed.
const (
	decisionAllow           = "allow"
	decisionDeny            = "deny"
	decisionRequireApproval = "require_approval"
)

// policyInput rebuilds the document the bundle is asked about, from what the envelope carries.
//
// `input.project` is deliberately ABSENT rather than empty-but-present: the bundle distinguishes "no
// parameters" from "a parameter I could not read", and an empty object is the former. See the package
// comment for why an absent parameter set makes the agent's verdict no stricter than the backend's.
func policyInput(v *envelope.Verified, now string) map[string]any {
	document := map[string]any{
		"operation":    string(v.Operation()),
		"now_rfc3339":  now,
		"change_items": policyChangeItems(v),
	}
	return document
}

// policyChangeItems extracts the paths a command would touch, for `paths.rego`.
//
// Read from the arguments rather than from a dedicated envelope field, because that is where they are:
// `changeset.apply` carries its items, and an operation with none contributes an empty list — which is
// the honest input for an operation that touches no path, and `paths.rego` allows it.
func policyChangeItems(v *envelope.Verified) []map[string]any {
	var args struct {
		Items []struct {
			Path     string `json:"path"`
			FilePath string `json:"file_path"`
			Action   string `json:"action"`
		} `json:"items"`
	}
	if json.Unmarshal(v.Args(), &args) != nil {
		return []map[string]any{}
	}
	items := make([]map[string]any, 0, len(args.Items))
	for _, item := range args.Items {
		// The backend has used both spellings across versions; accepting either is cheaper than a
		// disagreement caused by a field name.
		path := item.Path
		if path == "" {
			path = item.FilePath
		}
		if path == "" {
			continue
		}
		items = append(items, map[string]any{"path": path, "action": item.Action})
	}
	return items
}

// evaluateIndependently is the agent's second opinion. It returns nil when the command may proceed.
func (d *dispatcher) evaluateIndependently(ctx context.Context, v *envelope.Verified) error {
	if d.policy == nil {
		// No evaluator wired. Refusing every command would make an agent built without a bundle
		// useless, and silently allowing would make FR-38 a claim rather than a control — so the
		// capability is advertised as absent (see `Operations`) and this path is the honest middle:
		// nothing to disagree with, and the backend's decision plus the envelope signature still gate
		// the command.
		return nil
	}

	backendDecision := strings.TrimSpace(v.PolicyContext().Decision)
	document := policyInput(v, d.now().UTC().Format("2006-01-02T15:04:05Z07:00"))

	decision, err := d.policy.Evaluate(ctx, document, v.PolicyContext().BundleDigest)
	if err != nil {
		// An evaluation that could not run is not an allow. The agent holds the bundle and was asked to
		// check it; failing to do so means the command is unverified, and an unverified mutation is
		// exactly what the second opinion exists to stop.
		return fmt.Errorf("%w: the evaluation could not be performed: %w", ErrPolicyDisagreement, err)
	}

	verdict, _ := decision["result"].(string)
	switch verdict {
	case decisionAllow, decisionRequireApproval:
		// The agent does not object. Where the backend denied, its decision still stands — the agent
		// may not widen an authorisation, and it is missing the parameters that produced the deny.
		return nil
	case decisionDeny:
		reason, _ := decision["reason"].(string)
		if reason == "" {
			reason = "the bundle denied this command"
		}
		return fmt.Errorf("%w: %s (the backend recorded %q)", ErrPolicyDisagreement, reason, backendDecision)
	default:
		// An unrecognised verdict from a policy engine is not a reason to proceed.
		return fmt.Errorf("%w: the bundle returned an unrecognised decision %q", ErrPolicyDisagreement, verdict)
	}
}
