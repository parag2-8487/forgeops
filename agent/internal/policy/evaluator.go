package policy

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"sync"

	"github.com/open-policy-agent/opa/bundle"
	"github.com/open-policy-agent/opa/rego"
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
	// Calculate digest
	hash := sha256.Sum256(bundleData)
	digest := hex.EncodeToString(hash[:])

	// Read bundle
	b, err := bundle.NewReader(bytes.NewReader(bundleData)).Read()
	if err != nil {
		return err
	}

	// Prepare query
	// The decision path is typically data.forgeops.allow, or similar.
	// Actually, the test uses forgeops.allow, but let's just make it generic or data.forgeops.allow
	r := rego.New(
		rego.ParsedBundle("bundle", &b),
		rego.Query("data.forgeops.governance.decision"),
	)
	
	pq, err := r.PrepareForEval(ctx)
	if err != nil {
		return err
	}

	// Commit atomically
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

	if digest != envelopeDigest {
		return map[string]interface{}{"result": "deny", "reason": ErrDrift.Error()}, ErrDrift
	}

	// Evaluate
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
