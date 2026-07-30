// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"path/filepath"
	"strings"
	"testing"
)

// A lint whose failure path has never fired is not a lint (design.md §0.4.3,
// §0.4.5). These tests exercise both directions against fixture modules under
// testdata/, which the Go tool ignores and which therefore never join the agent's
// package graph.

func fixture(t *testing.T, name string) string {
	t.Helper()
	abs, err := filepath.Abs(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("resolving fixture %s: %v", name, err)
	}
	return abs
}

func TestNegativeFixtureIsRejected(t *testing.T) {
	t.Parallel()
	var out bytes.Buffer
	err := run(fixture(t, "negative"), "./internal/...", &out)
	if err == nil {
		t.Fatalf("expected failure on the negative fixture, got success:\n%s", out.String())
	}
	if !strings.Contains(out.String(), "MISSING") {
		t.Errorf("failure did not name the missing assertion:\n%s", out.String())
	}
	if !strings.Contains(out.String(), "MemStore") {
		t.Errorf("failure did not name the implementation:\n%s", out.String())
	}
}

func TestPositiveFixtureIsAccepted(t *testing.T) {
	t.Parallel()
	var out bytes.Buffer
	if err := run(fixture(t, "positive"), "./internal/...", &out); err != nil {
		t.Fatalf("positive fixture was rejected: %v\n%s", err, out.String())
	}
	if !strings.Contains(out.String(), "1 assertions found") {
		t.Errorf("the assertion was not counted:\n%s", out.String())
	}
}

func TestAnEmptyInterfaceSetIsAFailure(t *testing.T) {
	t.Parallel()
	// A checker that discovers nothing passes forever, which is the same vacuity
	// trap §0.4.5 closes for the mutation harness and §0.4.4 for the mandatory
	// selection. The `noifaces` fixture has a concrete type and no interface at
	// all, so the run must fail on emptiness rather than report success.
	var out bytes.Buffer
	err := run(fixture(t, "noifaces"), "./internal/...", &out)
	if err == nil {
		t.Fatalf("a package set with no interfaces was accepted:\n%s", out.String())
	}
	if !strings.Contains(err.Error(), "no exported interfaces") {
		t.Errorf("wrong failure reason: %v", err)
	}
}

func TestAssertionFormsAreRecognised(t *testing.T) {
	t.Parallel()
	cases := map[string]string{
		"pointer conversion":   "var _ Store = (*MemStore)(nil)",
		"qualified pointer":    "var _ pkg.Store = (*pkg.MemStore)(nil)",
		"composite literal":    "var _ Store = MemStore{}",
		"address of composite": "var _ Store = &MemStore{}",
	}
	for name, decl := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			iface, impl := parseAssertionForTest(decl)
			if iface != "Store" {
				t.Errorf("interface name: got %q, want Store", iface)
			}
			if impl != "MemStore" {
				t.Errorf("implementation name: got %q, want MemStore", impl)
			}
		})
	}
}
