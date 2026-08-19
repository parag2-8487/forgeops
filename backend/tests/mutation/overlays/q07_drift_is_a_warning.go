package policy

// NEGATIVE CONTROL for Q-07 (design Appendix B: "make the agent's digest comparison a
// warning"). Applied by `scripts/mutation-harness.py` via `go build -overlay`, which
// substitutes this file for `agent/internal/policy/evaluator.go` for the duration of one test
// run. It is never compiled into the agent.
//
// Byte-for-byte the committed evaluator except for the drift branch in `Evaluate`, which now
// logs and falls through to evaluation instead of returning a deny. That is exactly the
// "downgrade the rejection to a warning" the design names, and `verify.go` already warns
// against it in prose: "A rejection, never a warning. Q-07's negative control is downgrading
// it."
//
// With this applied the agent evaluates a bundle the backend never authorised, the fixture
// bundle allows unconditionally, and so every mismatched digest pair returns `allow` with no
// error. `TestPropertyQ07_DigestDisagreementDeniesFailClosed` must therefore FAIL.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"log"
	"sync"

	"github.com/open-policy-agent/opa/v1/bundle"
	"github.com/open-policy-agent/opa/v1/rego"
)

var (
	ErrNoBundle = errors.New("no policy bundle loaded")
	ErrDrift    = errors.New("policy bundle digest differs from envelope policy_context digest")
)

type Evaluator struct {
	mu           sync.RWMutex
	loadedDigest string
	hasBundle    bool
	query        *rego.PreparedEvalQuery
}

func NewEvaluator() *Evaluator {
	return &Evaluator{}
}

func (e *Evaluator) BundleDigest() string {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.loadedDigest
}

func (e *Evaluator) Load(ctx context.Context, bundleData []byte) error {
	hash := sha256.Sum256(bundleData)
	digest := hex.EncodeToString(hash[:])

	b, err := bundle.NewReader(bytes.NewReader(bundleData)).Read()
	if err != nil {
		return err
	}

	r := rego.New(
		rego.ParsedBundle("bundle", &b),
		rego.Query("data.forgeops.governance.decision"),
	)

	pq, err := r.PrepareForEval(ctx)
	if err != nil {
		return err
	}

	e.mu.Lock()
	defer e.mu.Unlock()
	e.query = &pq
	e.loadedDigest = digest
	e.hasBundle = true

	return nil
}

func (e *Evaluator) Evaluate(ctx context.Context, input map[string]interface{}, envelopeDigest string) (map[string]interface{}, error) {
	e.mu.RLock()
	has := e.hasBundle
	digest := e.loadedDigest
	pq := e.query
	e.mu.RUnlock()

	deny := map[string]interface{}{"result": "deny", "reason": "internal error"}

	if !has || pq == nil {
		return map[string]interface{}{"result": "deny", "reason": ErrNoBundle.Error()}, ErrNoBundle
	}

	// ── THE MUTATION ──────────────────────────────────────────────────────────
	// Was:
	//     if digest != envelopeDigest {
	//         return map[string]interface{}{"result": "deny", "reason": ErrDrift.Error()}, ErrDrift
	//     }
	// A warning instead of a rejection: execution continues into evaluation.
	if digest != envelopeDigest {
		log.Printf("warning: %v (agent=%s envelope=%s)", ErrDrift, digest, envelopeDigest)
	}

	rs, err := pq.Eval(ctx, rego.EvalInput(input))
	if err != nil {
		deny["reason"] = err.Error()
		return deny, err
	}

	if len(rs) == 0 || len(rs[0].Expressions) == 0 {
		return map[string]interface{}{"result": "deny", "reason": "undefined document"}, errors.New("undefined document")
	}

	val, ok := rs[0].Expressions[0].Value.(map[string]interface{})
	if !ok {
		return map[string]interface{}{"result": "deny", "reason": "invalid decision type"}, errors.New("invalid decision type")
	}
	return val, nil
}
