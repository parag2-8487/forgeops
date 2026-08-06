// SPDX-License-Identifier: Apache-2.0
package depgraph

import (
	"reflect"
	"testing"
)

func TestDirtyClosureDiamond(t *testing.T) {
	g := NewDependencyGraph()
	// Diamond graph: A imports B & C, B & C import D
	g.AddDependency("B.go", "D.go")
	g.AddDependency("C.go", "D.go")
	g.AddDependency("A.go", "B.go")
	g.AddDependency("A.go", "C.go")

	// If D.go is modified, dirty closure must contain D.go, B.go, C.go, A.go
	dirty := g.DirtyClosure([]string{"D.go"})
	expected := []string{"A.go", "B.go", "C.go", "D.go"}

	if !reflect.DeepEqual(dirty, expected) {
		t.Errorf("expected %v, got %v", expected, dirty)
	}
}

func TestDirtyClosureCircular(t *testing.T) {
	g := NewDependencyGraph()
	// Circular: A imports B, B imports A
	g.AddDependency("A.go", "B.go")
	g.AddDependency("B.go", "A.go")

	dirty := g.DirtyClosure([]string{"A.go"})
	expected := []string{"A.go", "B.go"}

	if !reflect.DeepEqual(dirty, expected) {
		t.Errorf("expected %v, got %v", expected, dirty)
	}
}

func TestDirtyClosureEmpty(t *testing.T) {
	g := NewDependencyGraph()
	dirty := g.DirtyClosure([]string{})
	if len(dirty) != 0 {
		t.Errorf("expected empty dirty closure, got %v", dirty)
	}
}
