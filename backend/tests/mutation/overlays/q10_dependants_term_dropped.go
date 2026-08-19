// SPDX-License-Identifier: Apache-2.0
package depgraph

// NEGATIVE CONTROL for Q-10. Applied by `scripts/mutation-harness.py` via `go build -overlay`,
// which substitutes this file for `agent/internal/scanner/depgraph/graph.go` for the duration of
// one test run. It is never compiled into the agent.
//
// Byte-for-byte the committed graph.go except that `DirtyClosure` no longer walks `dependents`.
// GENERATED, so it cannot drift from its original.
//
// With this applied, TestPropertyQ10_DirtyClosureEqualsFullRecomputation must FAIL for any drawn
// graph where a modified file has a dependent: the independent fixpoint recomputation in the test
// still finds it and `DirtyClosure` no longer does.

import (
	"sort"
)

type DependencyGraph struct {
	// dependencies: file -> set of files it depends on (imports)
	dependencies map[string]map[string]bool
	// dependents: file -> set of files that import it
	dependents map[string]map[string]bool
}

func NewDependencyGraph() *DependencyGraph {
	return &DependencyGraph{
		dependencies: make(map[string]map[string]bool),
		dependents:   make(map[string]map[string]bool),
	}
}

// AddDependency records that 'file' depends on (imports) 'importedFile'.
func (g *DependencyGraph) AddDependency(file, importedFile string) {
	if g.dependencies[file] == nil {
		g.dependencies[file] = make(map[string]bool)
	}
	g.dependencies[file][importedFile] = true

	if g.dependents[importedFile] == nil {
		g.dependents[importedFile] = make(map[string]bool)
	}
	g.dependents[importedFile][file] = true
}

// DirtyClosure computes the transitive closure of all files affected downstream by modifiedFiles.
func (g *DependencyGraph) DirtyClosure(modifiedFiles []string) []string {
	visited := make(map[string]bool)
	queue := append([]string{}, modifiedFiles...)

	for len(queue) > 0 {
		current := queue[0]
		queue = queue[1:]

		if visited[current] {
			continue
		}
		visited[current] = true

		// THE MUTATION (Q-10 negative control, Appendix B: "drop the Dependants(deleted) term
		// from DirtySet"). The dependents of a changed file are no longer enqueued, so the
		// closure returns only what it was given and every transitively affected file is left
		// clean. An incremental pass then reindexes strictly less than a full rescan.
		_ = g.dependents
	}

	result := make([]string, 0, len(visited))
	for f := range visited {
		result = append(result, f)
	}

	sort.Strings(result)
	return result
}
