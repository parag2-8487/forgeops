package policy

// NEGATIVE CONTROL for Q-06. Applied by `scripts/mutation-harness.py` via `go build -overlay`,
// which substitutes this file for `agent/internal/policy/evaluator.go` for the duration of one test
// run. It is never compiled into the agent.
//
// The committed evaluator plus one rewrite inside `Load`: approval.rego's prod comparison is
// flipped from `==` to `!=` in the copy the AGENT evaluates, after the digest has been taken from
// the original bytes. GENERATED, so it cannot drift from its original.
//
// With this applied, TestPropertyQ06_AgentAgreesWithTheBackendDecision must FAIL: the corpus holds
// the backend's decisions from the unmodified bundle, and every fixture whose only approval reason
// was `environment == "prod"` now evaluates to allow on the agent side, while non-prod fixtures
// start requiring approval.

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"strings"
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
	// Calculate digest
	hash := sha256.Sum256(bundleData)
	digest := hex.EncodeToString(hash[:])

	// THE MUTATION (Q-06 negative control, Appendix B: "in the agent's copy of the bundle only,
	// invert the comparison in approval.rego's `require_approval if input.environment == \"prod\"`
	// clause to `!=`").
	//
	// Applied AFTER the digest above was computed from the original bytes, so `loadedDigest` still
	// matches what the caller passes to Evaluate. Q-06 is quantified over the digests-EQUAL case;
	// recomputing the digest here would make every fixture fail with ErrDrift, which is Q-07's
	// property, and a control that fails for the wrong reason is not evidence about this one.
	bundleData = invertProdClauseForControl(bundleData)

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

// invertProdClauseForControl rewrites approval.rego inside a bundle archive, flipping the prod
// comparison. Part of the Q-06 negative control only.
func invertProdClauseForControl(bundleData []byte) []byte {
	gzr, err := gzip.NewReader(bytes.NewReader(bundleData))
	if err != nil {
		return bundleData
	}
	defer gzr.Close()

	type entry struct {
		header *tar.Header
		body   []byte
	}
	var entries []entry

	tr := tar.NewReader(gzr)
	for {
		header, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return bundleData
		}
		body, err := io.ReadAll(tr)
		if err != nil {
			return bundleData
		}
		if strings.HasSuffix(header.Name, "approval.rego") {
			// Anchored on the LEADING NEWLINE, which matters. `approval.rego`'s header comment
			// quotes this clause verbatim while explaining that Q-06's control targets it, so an
			// unanchored replace-first rewrites the COMMENT and leaves the rule untouched. That
			// is exactly what happened on the first attempt and the harness reported the row
			// VACUOUS — the mutant survived because the mutation had been applied to prose.
			const oldClause = "\nrequire_approval if input.environment == \"prod\""
			const newClause = "\nrequire_approval if input.environment != \"prod\""
			text := string(body)
			if !strings.Contains(text, oldClause) {
				// Loud, not silent. A control that quietly no-ops reports VACUOUS and looks like
				// an under-specified property instead of a broken mutation.
				panic("Q-06 control: the prod clause was not found in approval.rego; the mutation would be a no-op")
			}
			body = []byte(strings.Replace(text, oldClause, newClause, 1))
			header.Size = int64(len(body))
		}
		entries = append(entries, entry{header: header, body: body})
	}

	var buf bytes.Buffer
	gzw := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gzw)
	for _, e := range entries {
		if err := tw.WriteHeader(e.header); err != nil {
			return bundleData
		}
		if _, err := tw.Write(e.body); err != nil {
			return bundleData
		}
	}
	if err := tw.Close(); err != nil {
		return bundleData
	}
	if err := gzw.Close(); err != nil {
		return bundleData
	}
	return buf.Bytes()
}
