// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"errors"
	"strings"
	"testing"
)

// FR-38: the agent evaluates the same policy independently and refuses if it disagrees.
//
// The backend's half was always real. The agent's half was a DIGEST comparison — `envelope.Verifier`
// refuses a stale `policy_context.bundle_digest`, which proves the two sides hold the same rules and says
// nothing about whether they reach the same verdict. `policy.Evaluator`, real embedded OPA over the real
// bundle, was reachable only from `cmd/evalhelper`.
//
// These tests use a stub `PolicySource` rather than the real evaluator, deliberately: the subject is the
// DISPATCHER's behaviour on each verdict — allow, deny, require_approval, an unrecognised value, and an
// evaluation that could not run — and driving those five through a real bundle would need five contrived
// policies and would test OPA rather than this code. That the real evaluator produces those verdicts over
// the real bundle is `internal/policy`'s own test set, and the backend/agent agreement over identical
// input is `backend/tests/integration/test_agent_backend_policy_agreement.py`.

type stubPolicy struct {
	decision  map[string]any
	err       error
	digest    string
	calls     int
	lastInput map[string]any
}

func (s *stubPolicy) Evaluate(_ context.Context, input map[string]any, _ string) (map[string]any, error) {
	s.calls++
	s.lastInput = input
	return s.decision, s.err
}

func (s *stubPolicy) BundleDigest() string { return s.digest }

func dispatcherWithPolicy(t *testing.T, policy PolicySource) Dispatcher {
	t.Helper()
	d, err := New(Deps{Root: t.TempDir(), Policy: policy})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return d
}

func TestPolicyGate_AnAgentDenyRefusesEvenThoughTheBackendAllowed(t *testing.T) {
	// FR-38's whole point. The envelope is signed, carries an approval, and records the backend's
	// `allow` — and the agent's own rules say no, so the command does not run.
	policy := &stubPolicy{decision: map[string]any{"result": "deny", "reason": "paths.protected_path"}}
	d := dispatcherWithPolicy(t, policy)

	_, err := d.Execute(context.Background(),
		verified(t, OpValidateYAML, "", map[string]any{"path": "x.yaml"}, 51), nil)
	if err == nil {
		t.Fatal("the agent executed a command its own policy denies")
	}
	if !errors.Is(err, ErrPolicyDisagreement) {
		t.Errorf("error %v is not an ErrPolicyDisagreement", err)
	}
	// The reason the bundle gave must survive: an operator needs to know WHICH rule refused.
	if !strings.Contains(err.Error(), "paths.protected_path") {
		t.Errorf("the refusal does not name the rule: %v", err)
	}
	if policy.calls != 1 {
		t.Errorf("the evaluator was called %d times", policy.calls)
	}
}

func TestPolicyGate_AnAgentAllowLetsTheCommandProceed(t *testing.T) {
	// The control. Without it, a gate that refuses everything passes the test above.
	policy := &stubPolicy{decision: map[string]any{"result": "allow"}}
	d := dispatcherWithPolicy(t, policy)

	_, err := d.Execute(context.Background(),
		verified(t, OpValidateYAML, "", map[string]any{"path": "absent.yaml"}, 52), nil)
	// The handler is reached and fails on the missing file, which is the proof that the gate did not
	// stop it: a policy refusal returns before any argument is resolved.
	if errors.Is(err, ErrPolicyDisagreement) {
		t.Fatalf("an allow was treated as a disagreement: %v", err)
	}
}

func TestPolicyGate_RequireApprovalIsNotARefusal(t *testing.T) {
	// `require_approval` is the backend's business — it is what makes a change set pend. By the time an
	// envelope exists the approval has happened, so the agent treating it as a deny would refuse every
	// human-approved mutation.
	policy := &stubPolicy{decision: map[string]any{"result": "require_approval"}}
	d := dispatcherWithPolicy(t, policy)

	_, err := d.Execute(context.Background(),
		verified(t, OpValidateYAML, "", map[string]any{"path": "absent.yaml"}, 53), nil)
	if errors.Is(err, ErrPolicyDisagreement) {
		t.Fatalf("require_approval was treated as a refusal: %v", err)
	}
}

func TestPolicyGate_AnEvaluationThatCouldNotRunIsNotAnAllow(t *testing.T) {
	// The agent holds the bundle and was asked to check it. Failing to do so means the command is
	// unverified, and an unverified mutation is what the second opinion exists to stop.
	policy := &stubPolicy{err: errors.New("bundle not loaded")}
	d := dispatcherWithPolicy(t, policy)

	_, err := d.Execute(context.Background(),
		verified(t, OpValidateYAML, "", map[string]any{"path": "x.yaml"}, 54), nil)
	if !errors.Is(err, ErrPolicyDisagreement) {
		t.Fatalf("a failed evaluation did not refuse: %v", err)
	}
	if !strings.Contains(err.Error(), "could not be performed") {
		t.Errorf("the refusal does not say the evaluation failed: %v", err)
	}
}

func TestPolicyGate_AnUnrecognisedVerdictRefuses(t *testing.T) {
	// A policy engine returning something outside its vocabulary is not a reason to proceed.
	for _, verdict := range []any{"maybe", "", nil, 42} {
		policy := &stubPolicy{decision: map[string]any{"result": verdict}}
		d := dispatcherWithPolicy(t, policy)
		_, err := d.Execute(context.Background(),
			verified(t, OpValidateYAML, "", map[string]any{"path": "x.yaml"}, 55), nil)
		if !errors.Is(err, ErrPolicyDisagreement) {
			t.Errorf("verdict %v was accepted: %v", verdict, err)
		}
	}
}

func TestPolicyGate_AnAgentWithNoEvaluatorStillEnforcesEverythingElse(t *testing.T) {
	// Refusing every command would make an agent built without a bundle useless; silently allowing
	// would make FR-38 a claim. The honest middle is that there is nothing to disagree with, and the
	// signature, the approval requirement and the digest binding still gate the command.
	d := dispatcherWithPolicy(t, nil)
	_, err := d.Execute(context.Background(),
		verified(t, OpValidateYAML, "", map[string]any{"path": "absent.yaml"}, 56), nil)
	if errors.Is(err, ErrPolicyDisagreement) {
		t.Fatalf("an agent with no evaluator reported a disagreement: %v", err)
	}
	// And the approval rule is untouched by the absence of an evaluator.
	if _, err := d.Execute(context.Background(),
		verified(t, OpChangeSetApply, "", map[string]any{}, 57), nil); !errors.Is(err, ErrApprovalRequired) {
		t.Errorf("the approval requirement stopped applying: %v", err)
	}
}

func TestPolicyGate_TheGateRunsBeforeTheApprovalIsSpent(t *testing.T) {
	// Ordering matters: a command the agent's rules refuse must cost an evaluation and nothing more —
	// no argument decoded, no file opened, no tool started.
	policy := &stubPolicy{decision: map[string]any{"result": "deny", "reason": "no"}}
	root := t.TempDir()
	d, err := New(Deps{Root: root, Policy: policy})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	before := countFiles(t, root)
	if _, err := d.Execute(context.Background(), verified(t, OpSecretsInject, "approval-pg-1", map[string]any{
		"project_id": "proj-1",
		"values":     map[string]string{"TOKEN": "x"},
	}, 58), nil); !errors.Is(err, ErrPolicyDisagreement) {
		t.Fatalf("a denied mutation was not refused: %v", err)
	}
	if after := countFiles(t, root); after != before {
		t.Error("a refused command touched the workspace")
	}
	// And nothing was injected, which is the observable half of "no work began".
	inner, ok := d.(*dispatcher)
	if !ok {
		t.Fatal("unexpected dispatcher type")
	}
	if len(inner.secrets.keys("proj-1")) != 0 {
		t.Error("a refused injection still stored values")
	}
}

func TestPolicyGate_TheInputCarriesTheOperationAndTheChangeItems(t *testing.T) {
	// The bundle reads `input.operation` and `input.change_items`, so both must be present or every
	// rule that depends on them is silently inert — which is the exact failure `policy_parameters`
	// had on the backend side.
	policy := &stubPolicy{decision: map[string]any{"result": "allow"}}
	d := dispatcherWithPolicy(t, policy)
	_, _ = d.Execute(context.Background(), verified(t, OpChangeSetApply, "approval-pg-2", map[string]any{
		"items": []map[string]any{
			{"path": "package.json", "action": "modify"},
			{"file_path": "src/index.ts", "action": "create"},
		},
	}, 59), nil)

	if policy.lastInput == nil {
		t.Fatal("the evaluator was never called")
	}
	if got := policy.lastInput["operation"]; got != string(OpChangeSetApply) {
		t.Errorf("input.operation = %v", got)
	}
	if _, present := policy.lastInput["now_rfc3339"]; !present {
		t.Error("input.now_rfc3339 is absent, so schedule.rego cannot evaluate")
	}
	items, ok := policy.lastInput["change_items"].([]map[string]any)
	if !ok {
		t.Fatalf("input.change_items is %T", policy.lastInput["change_items"])
	}
	if len(items) != 2 {
		t.Fatalf("got %d change items, want 2: %v", len(items), items)
	}
	// Both field spellings are accepted, because the backend has used each and a disagreement caused by
	// a field NAME would be indistinguishable from a policy disagreement.
	paths := []string{items[0]["path"].(string), items[1]["path"].(string)}
	if paths[0] != "package.json" || paths[1] != "src/index.ts" {
		t.Errorf("paths = %v", paths)
	}
}

func TestPolicyGate_ProjectParametersAreAbsentRatherThanEmpty(t *testing.T) {
	// The information asymmetry, asserted so it cannot be "fixed" by sending an empty object.
	//
	// The bundle distinguishes "no parameters" from "a parameter I could not read", and an empty
	// `input.project` is the former. The agent genuinely does not have the project's stored parameters —
	// they do not travel in the envelope — and the rules are total over an absent member, which is what
	// makes the agent's verdict no STRICTER than the backend's and therefore safe to act on when it
	// denies.
	policy := &stubPolicy{decision: map[string]any{"result": "allow"}}
	d := dispatcherWithPolicy(t, policy)
	_, _ = d.Execute(context.Background(),
		verified(t, OpValidateYAML, "", map[string]any{"path": "absent.yaml"}, 60), nil)

	if _, present := policy.lastInput["project"]; present {
		t.Error("input.project was sent; the agent does not have the project's parameters and must " +
			"not imply it evaluated them")
	}
}
