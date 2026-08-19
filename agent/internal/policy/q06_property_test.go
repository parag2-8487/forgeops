// SPDX-License-Identifier: Apache-2.0
package policy

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// Property Q-06 (design Appendix B; tasks.md leaf 9.6).
//
//	∀ governance inputs (operations × change-item sets × weekdays × timezones × verdicts ×
//	environments), when the bundle digests are equal: the backend OPA-server decision equals
//	the agent's embedded decision.
//
// # WHY THIS FILE DID NOT EXIST
//
// Leaf 9.6 was recorded `done` in PROGRESS.md with nothing behind it. `backend/src/policies/opa.py`
// carries the comment "It is module-level and side-effect free so that Q-06 (leaf 9.6) can generate
// inputs and feed the *same* mapping to both evaluators without an HTTP server in the loop" — the
// mapping was deliberately shaped for a test that was never written. So the two evaluators of one
// rule had never been compared, which is the defect the repository elsewhere calls the Q-06/Q-14
// lesson: two implementations agree until the day they do not.
//
// # THE CROSS-RUNTIME ARRANGEMENT
//
// The two sides cannot both be called from one process. The backend maps its stage-1 payload
// through `governance_input` and asks an OPA SERVER over HTTP; the agent evaluates the same bundle
// in-process through OPA's Go Rego library. So `scripts/gen-governance-fixtures.py` drives the
// Python side and commits the mapped inputs together with the decisions OPA returned, and this file
// re-derives the agent side from the committed inputs and compares — the same asymmetric two-way
// lock `corpus_test.go` uses for Q-14. Break Python and the corpus changes, so this file fails
// against the committed bytes; break the agent and this file fails directly.
//
// The digests are equal by construction here: the agent loads a bundle built from the same
// `policies/agent/*.rego` the corpus was generated against, and `Evaluate` is passed its own loaded
// digest. Q-06 is quantified over the digests-EQUAL case; the digests-differ case is Q-07's.
const q06CorpusPath = "../../testdata/governance/q06_corpus.json"

// corpusFloor is a committed integer that may only be raised.
//
// The same mechanism as `corpus_test.go`'s and §0.4.2's INVENTORY_FLOOR, for the same reason: a
// corpus file that failed to parse into anything would make the loop below iterate zero times and
// every assertion pass, and a suite that proves nothing looks exactly like one that proves
// everything.
const q06CorpusFloor = 48

// q06DecisionFloor guards the other direction. A corpus in which every decision is identical
// cannot distinguish an agent that agrees from an agent that returns a constant, so the number of
// DISTINCT decisions is floored too.
const q06DecisionFloor = 4

type q06Corpus struct {
	Property     string       `json:"property"`
	DecisionPath string       `json:"decision_path"`
	Fixtures     []q06Fixture `json:"fixtures"`
}

type q06Fixture struct {
	Input    map[string]interface{} `json:"input"`
	Decision map[string]interface{} `json:"decision"`
}

func TestPropertyQ06_AgentAgreesWithTheBackendDecision(t *testing.T) {
	raw, err := os.ReadFile(q06CorpusPath)
	if err != nil {
		t.Fatalf("reading the Q-06 corpus failed: %v", err)
	}

	var corpus q06Corpus
	if err := json.Unmarshal(raw, &corpus); err != nil {
		t.Fatalf("parsing the Q-06 corpus failed: %v", err)
	}

	if len(corpus.Fixtures) < q06CorpusFloor {
		t.Fatalf(
			"the Q-06 corpus holds %d fixture(s), below the committed floor of %d. Regenerate it "+
				"with scripts/gen-governance-fixtures.py; the floor may be raised but never lowered",
			len(corpus.Fixtures), q06CorpusFloor,
		)
	}

	distinct := map[string]bool{}
	for _, fixture := range corpus.Fixtures {
		key, marshalErr := json.Marshal(canonicalise(fixture.Decision))
		if marshalErr != nil {
			t.Fatalf("marshalling a corpus decision failed: %v", marshalErr)
		}
		distinct[string(key)] = true
	}
	if len(distinct) < q06DecisionFloor {
		t.Fatalf(
			"the corpus carries only %d distinct decision(s), below the floor of %d: it could not "+
				"tell an agreeing agent from one that returns a constant",
			len(distinct), q06DecisionFloor,
		)
	}

	bundleData, digest := bundleFromPolicyDir(t, "../../../policies/agent")

	evaluator := NewEvaluator()
	ctx := context.Background()
	if err := evaluator.Load(ctx, bundleData); err != nil {
		t.Fatalf("loading the real policies/agent bundle failed: %v", err)
	}

	disagreements := 0
	for index, fixture := range corpus.Fixtures {
		got, evalErr := evaluator.Evaluate(ctx, fixture.Input, digest)
		if evalErr != nil {
			t.Fatalf("fixture %d: the agent could not evaluate the backend's own input: %v", index, evalErr)
		}

		wantDecision := canonicalise(fixture.Decision)
		gotDecision := canonicalise(got)

		if !reflect.DeepEqual(gotDecision, wantDecision) {
			disagreements++
			t.Errorf(
				"Q-06 violation at fixture %d: the two evaluators disagree over the SAME bundle.\n"+
					"  input:    %v\n  backend:  %v\n  agent:    %v",
				index, sortedPairs(fixture.Input), wantDecision, gotDecision,
			)
		}
	}
	if disagreements == 0 {
		t.Logf("Q-06: %d fixtures, %d distinct decisions, both evaluators agree on every one",
			len(corpus.Fixtures), len(distinct))
	}
}

// bundleFromPolicyDir builds an OPA bundle from every non-test .rego in dir.
//
// `*_test.rego` is excluded because those files declare `test_*` rules that are not part of the
// deployed bundle; including them would make the agent's loaded digest differ from the bundle the
// backend publishes, which is a different property (Q-07) and would mask this one.
func bundleFromPolicyDir(t *testing.T, dir string) ([]byte, string) {
	t.Helper()

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("reading the policy directory %s failed: %v", dir, err)
	}

	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".rego") || strings.HasSuffix(name, "_test.rego") {
			continue
		}
		names = append(names, name)
	}
	// Sorted so the archive — and therefore its digest — is deterministic across runs.
	sort.Strings(names)

	if len(names) == 0 {
		t.Fatalf("no .rego files found in %s, so the bundle would be empty", dir)
	}

	var buffer bytes.Buffer
	gw := gzip.NewWriter(&buffer)
	tw := tar.NewWriter(gw)

	write := func(name string, content []byte) {
		header := &tar.Header{Name: name, Mode: 0o600, Size: int64(len(content))}
		if err := tw.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write(content); err != nil {
			t.Fatal(err)
		}
	}

	for _, name := range names {
		content, readErr := os.ReadFile(filepath.Join(dir, name))
		if readErr != nil {
			t.Fatalf("reading %s failed: %v", name, readErr)
		}
		write(name, content)
	}
	write(".manifest", []byte(`{"revision":"q06","roots":["forgeops"]}`))

	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gw.Close(); err != nil {
		t.Fatal(err)
	}

	raw := buffer.Bytes()
	sum := sha256.Sum256(raw)
	return raw, hex.EncodeToString(sum[:])
}

// canonicalise renders a decision map into a stable, comparable form. OPA returns JSON numbers as
// float64 and the corpus round-trips through JSON, so comparing the maps directly would fail on
// representation rather than on disagreement.
func canonicalise(decision map[string]interface{}) map[string]string {
	out := make(map[string]string, len(decision))
	for key, value := range decision {
		encoded, err := json.Marshal(value)
		if err != nil {
			out[key] = "<unmarshalable>"
			continue
		}
		out[key] = string(encoded)
	}
	return out
}

func sortedPairs(input map[string]interface{}) string {
	keys := make([]string, 0, len(input))
	for key := range input {
		keys = append(keys, key)
	}
	sort.Strings(keys)

	var sb strings.Builder
	for i, key := range keys {
		if i > 0 {
			sb.WriteString(" ")
		}
		encoded, _ := json.Marshal(input[key])
		sb.WriteString(key)
		sb.WriteString("=")
		sb.Write(encoded)
	}
	return sb.String()
}
