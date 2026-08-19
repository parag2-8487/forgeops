// SPDX-License-Identifier: Apache-2.0
package depgraph

import (
	"fmt"
	"reflect"
	"sort"
	"testing"

	"pgregory.net/rapid"
)

// Property Q-10 (design Appendix B; tasks.md leaf 11.11).
//
//	∀ edit sequences over a generated project: the incrementally maintained index equals
//	FullRescan(final_tree) — same chunks, same edges, same summary invalidation, no orphans.
//
// # WHAT THIS FILE REPLACES, AND WHAT IT DOES NOT CLAIM
//
// Q-10's property file was `agent/internal/scanner/q10_property_test.go`. It created some files,
// called `ScanDirectory`, then called `ScanDirectory` again on the SAME unmodified directory and
// compared the two `Languages` maps. The comment above the second call read "Mutate one file and
// re-scan" and no mutation was performed. So it compared a scan to itself, which is true for any
// deterministic scanner, and it never touched the dependency closure at all — which is why
// Appendix B's control for Q-10 ("drop the `Dependants(deleted)` term from `DirtySet`") could not
// possibly have failed it.
//
// This file tests the clause Appendix B's control targets and that the tree actually implements:
// the DIRTY CLOSURE. It does not claim to test chunk-level or summary-level equality, because
// `analysis/index_service` has no incremental path to compare against a full rescan yet. Stating
// that here rather than implying full coverage is the point — Appendix E's Phase 0 defect was an
// evidence bar that named more than it delivered.
//
// # WHY THE REFERENCE IMPLEMENTATION IS IN THE TEST
//
// "Incremental equals full" needs two computations of the same answer. `DirtyClosure` walks the
// graph's own `dependents` index incrementally; the reference below recomputes the closure from
// the raw edge list by fixpoint, touching none of the graph's internal maps. Two implementations
// of one rule, which is the same two-way lock Q-06 and Q-14 use across runtimes. A mutation to
// the traversal breaks one and not the other.
func TestPropertyQ10_DirtyClosureEqualsFullRecomputation(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		fileCount := rapid.IntRange(1, 8).Draw(rt, "fileCount")
		files := make([]string, fileCount)
		for i := range files {
			files[i] = fmt.Sprintf("pkg/file_%d.go", i)
		}

		// Edges are drawn without forbidding self-loops or cycles: Appendix B names cycles
		// explicitly, and a closure that only terminates on a DAG is a closure that hangs on a
		// real import graph.
		edgeCount := rapid.IntRange(0, 12).Draw(rt, "edgeCount")
		type edge struct{ from, to string }
		edges := make([]edge, 0, edgeCount)

		graph := NewDependencyGraph()
		for i := 0; i < edgeCount; i++ {
			from := files[rapid.IntRange(0, fileCount-1).Draw(rt, "from")]
			to := files[rapid.IntRange(0, fileCount-1).Draw(rt, "to")]
			edges = append(edges, edge{from: from, to: to})
			graph.AddDependency(from, to)
		}

		// The edit set: the files a watcher reported as changed or deleted.
		modifiedCount := rapid.IntRange(1, fileCount).Draw(rt, "modifiedCount")
		modified := make([]string, 0, modifiedCount)
		for i := 0; i < modifiedCount; i++ {
			modified = append(modified, files[rapid.IntRange(0, fileCount-1).Draw(rt, "modified")])
		}

		got := graph.DirtyClosure(modified)

		// ── the independent recomputation ─────────────────────────────────────
		// Every file reachable by following "is imported by" edges from any modified file.
		want := map[string]bool{}
		for _, m := range modified {
			want[m] = true
		}
		for changed := true; changed; {
			changed = false
			for _, e := range edges {
				// e.from imports e.to, so a change to e.to dirties e.from.
				if want[e.to] && !want[e.from] {
					want[e.from] = true
					changed = true
				}
			}
		}
		expected := sortedSet(want)

		if !reflect.DeepEqual(got, expected) {
			rt.Fatalf(
				"Q-10 violation: DirtyClosure != independent recomputation.\n"+
					"  edges: %v\n  modified: %v\n  got: %v\n  expected: %v",
				edges, modified, got, expected,
			)
		}

		// ── no orphans: every modified file is in its own closure ─────────────
		inClosure := map[string]bool{}
		for _, f := range got {
			inClosure[f] = true
		}
		for _, m := range modified {
			if !inClosure[m] {
				rt.Fatalf("Q-10 violation: modified file %q is absent from its own dirty closure", m)
			}
		}

		// ── idempotence: re-closing a closed set adds nothing ─────────────────
		// A closure that grows on a second application is not a fixpoint, and an index built
		// from it would rescan a different set on every pass.
		again := graph.DirtyClosure(got)
		if !reflect.DeepEqual(again, got) {
			rt.Fatalf(
				"Q-10 violation: DirtyClosure is not idempotent.\n  first: %v\n  second: %v",
				got, again,
			)
		}
	})
}

func sortedSet(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for key := range set {
		out = append(out, key)
	}
	sort.Strings(out)
	return out
}
