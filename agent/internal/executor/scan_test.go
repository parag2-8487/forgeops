// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
)

// recordingIndexer is the test double for the collaborator the app layer supplies.
type recordingIndexer struct {
	fullCalls    []string
	changedCalls [][]string
	summary      IndexSummary
	err          error
}

func (r *recordingIndexer) IndexFull(_ context.Context, projectID string) (IndexSummary, error) {
	r.fullCalls = append(r.fullCalls, projectID)
	return r.summary, r.err
}

func (r *recordingIndexer) IndexChanged(_ context.Context, projectID string, changed []string) (IndexSummary, error) {
	r.changedCalls = append(r.changedCalls, append([]string{projectID}, changed...))
	return r.summary, r.err
}

func dispatcherWithIndexer(t *testing.T, idx CodebaseIndexer) Dispatcher {
	t.Helper()
	d, err := New(Deps{Root: t.TempDir(), Indexer: idx})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return d
}

func TestScan_AnAgentWithNoIndexerRefusesByNameRatherThanReportingAnEmptyIndex(t *testing.T) {
	// The failure mode this guards is the expensive one. An empty report is not a harmless
	// no-op: a FULL report is authoritative, so the backend prunes every path absent from it,
	// and a successful-looking scan that submitted nothing would DELETE the project's index and
	// record the deletion as a success. Refusing leaves the previous index intact.
	d := dispatcherWithIndexer(t, nil)
	for _, op := range []Operation{OpScanFull, OpScanIncremental} {
		_, err := d.Execute(context.Background(), verified(t, op, "", map[string]any{
			"project_id": "11111111-1111-1111-1111-111111111111",
			"paths":      []any{"main.go"},
		}, 1), nil)
		if !errors.Is(err, ErrNoIndexer) {
			t.Errorf("%s gave %v, want ErrNoIndexer", op, err)
		}
	}
}

func TestScan_CapabilityAdvertisementFollowsTheWiringNotTheTable(t *testing.T) {
	// §7.4 has the backend choose what to send from the advertised set. Advertising a scan the
	// agent would then refuse turns a command that should never have been sent into a failed one,
	// and the operator sees a failure rather than an absence.
	implemented := func(d Dispatcher, op Operation) bool {
		t.Helper()
		for _, info := range d.Operations() {
			if info.Operation == op {
				return info.Implemented
			}
		}
		t.Fatalf("%s is absent from the catalogue entirely", op)
		return false
	}

	without := dispatcherWithIndexer(t, nil)
	with := dispatcherWithIndexer(t, &recordingIndexer{})
	for _, op := range []Operation{OpScanFull, OpScanIncremental} {
		if implemented(without, op) {
			t.Errorf("%s is advertised as implemented with no indexer wired", op)
		}
		if !implemented(with, op) {
			t.Errorf("%s is advertised as unimplemented despite a wired indexer", op)
		}
	}

	// The computed answer must not leak into unrelated rows, in both directions: `changeset.apply` is
	// implemented regardless of the indexer, and `project.register` is not implemented regardless of
	// it. `validate.compose` used to be the negative example and became implemented when the six
	// validators were built, which is the same drift `TestTheUnimplementedExampleIsStillUnimplemented`
	// now guards in the dispatcher tests.
	for _, d := range []Dispatcher{without, with} {
		if !implemented(d, OpChangeSetApply) {
			t.Error("changeset.apply stopped being implemented")
		}
		if implemented(d, OpProjectRegister) {
			t.Error("project.register became implemented")
		}
		// And the validators are implemented irrespective of the indexer, which is the other half of
		// "the computed answer applies only to the scan pair".
		if !implemented(d, OpValidateCompose) {
			t.Error("validate.compose is not implemented despite having a body")
		}
	}
}

func TestScan_TheSummaryTravelsBackButTheIndexDoesNot(t *testing.T) {
	// §7.3 sizes `command.result` for a status. A repository's worth of file contents in that
	// frame is the reason the indexer submits the report itself rather than returning it.
	idx := &recordingIndexer{summary: IndexSummary{
		FilesIndexed: 12, ChunksIndexed: 40, Dependencies: 7,
		RedactionCount: 2, InventoryHash: "abc123",
		VectorsAbsentReason: "no embedding provider is configured",
	}}
	d := dispatcherWithIndexer(t, idx)

	res, err := d.Execute(context.Background(), verified(t, OpScanFull, "", map[string]any{
		"project_id": "22222222-2222-2222-2222-222222222222",
	}, 1), nil)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Status != "succeeded" {
		t.Errorf("status = %q", res.Status)
	}
	var got IndexSummary
	if err := json.Unmarshal([]byte(res.Output), &got); err != nil {
		t.Fatalf("the summary is not decodable JSON: %v", err)
	}
	if got != idx.summary {
		t.Errorf("summary = %+v, want %+v", got, idx.summary)
	}
	// The reason retrieval is sparse-only has to survive the round trip, or an operator reads a
	// complete-looking index that no vector query can search.
	if got.VectorsAbsentReason == "" {
		t.Error("the vectors-absent reason was dropped on the way back")
	}
	if len(idx.fullCalls) != 1 || idx.fullCalls[0] != "22222222-2222-2222-2222-222222222222" {
		t.Errorf("IndexFull calls = %v", idx.fullCalls)
	}
}

func TestScan_AScanWithoutAProjectIsRefusedBeforeTheWorkStarts(t *testing.T) {
	// The index is per project. A scan with no project id could only be guessed at, and a guess
	// would write one project's contents into another's index.
	idx := &recordingIndexer{}
	d := dispatcherWithIndexer(t, idx)
	if _, err := d.Execute(context.Background(), verified(t, OpScanFull, "", map[string]any{}, 1), nil); err == nil {
		t.Fatal("a scan with no project_id was accepted")
	}
	if len(idx.fullCalls) != 0 {
		t.Errorf("the workspace was read anyway: %v", idx.fullCalls)
	}
}

func TestScan_AnIncrementalScanWithNoPathsIsRefusedRatherThanSilentlyDoingNothing(t *testing.T) {
	// A partial report prunes nothing, so an empty one would index nothing and delete nothing:
	// the command would report success having had no effect at all, and the caller could not tell
	// that from a rescan where genuinely nothing needed reindexing.
	idx := &recordingIndexer{}
	d := dispatcherWithIndexer(t, idx)
	_, err := d.Execute(context.Background(), verified(t, OpScanIncremental, "", map[string]any{
		"project_id": "33333333-3333-3333-3333-333333333333",
	}, 1), nil)
	if err == nil {
		t.Fatal("an incremental scan with no changed paths was accepted")
	}
	if len(idx.changedCalls) != 0 {
		t.Errorf("the indexer ran anyway: %v", idx.changedCalls)
	}
}

func TestScan_TheChangedPathsReachTheIndexer(t *testing.T) {
	idx := &recordingIndexer{summary: IndexSummary{FilesIndexed: 2}}
	d := dispatcherWithIndexer(t, idx)
	if _, err := d.Execute(context.Background(), verified(t, OpScanIncremental, "", map[string]any{
		"project_id": "44444444-4444-4444-4444-444444444444",
		"paths":      []any{"cmd/main.go", "internal/pkg/x.go"},
	}, 1), nil); err != nil {
		t.Fatalf("Execute: %v", err)
	}
	want := []string{"44444444-4444-4444-4444-444444444444", "cmd/main.go", "internal/pkg/x.go"}
	if len(idx.changedCalls) != 1 || len(idx.changedCalls[0]) != len(want) {
		t.Fatalf("changed calls = %v, want one call of %v", idx.changedCalls, want)
	}
	for i, v := range want {
		if idx.changedCalls[0][i] != v {
			t.Errorf("changed[%d] = %q, want %q", i, idx.changedCalls[0][i], v)
		}
	}
}

func TestScan_AFailingIndexerIsReportedRatherThanTreatedAsAnEmptyIndex(t *testing.T) {
	idx := &recordingIndexer{err: errors.New("the backend refused the report")}
	d := dispatcherWithIndexer(t, idx)
	_, err := d.Execute(context.Background(), verified(t, OpScanFull, "", map[string]any{
		"project_id": "55555555-5555-5555-5555-555555555555",
	}, 1), nil)
	if err == nil {
		t.Fatal("a failed scan reported success")
	}
	// The cause has to survive: "full scan failed" alone sends an operator to the scanner when
	// the backend was the thing that refused.
	if !contains(err.Error(), "the backend refused the report") {
		t.Errorf("err = %v; the indexer's own reason was dropped", err)
	}
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (haystack == needle || indexOf(haystack, needle) >= 0)
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}
