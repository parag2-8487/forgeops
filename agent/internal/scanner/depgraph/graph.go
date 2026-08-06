// SPDX-License-Identifier: Apache-2.0
package depgraph

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

		// Add all direct dependents (files that import 'current')
		for dep := range g.dependents[current] {
			if !visited[dep] {
				queue = append(queue, dep)
			}
		}
	}

	result := make([]string, 0, len(visited))
	for f := range visited {
		result = append(result, f)
	}

	sort.Strings(result)
	return result
}
