// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/secretscan"
)

// The §1.3 property that "a first scan works" does not satisfy: a change to a file invalidates that
// file AND everything that imports it, and nothing else.
//
// WHY THIS IS A UNIT TEST RATHER THAN A LIVE ONE. The live watch was exercised too, but the
// FAN-OUT is a property of the dependency closure rather than of the filesystem, and asserting it
// here is both deterministic and specific: the live run can only show that a re-index happened,
// whereas this shows exactly which paths were in it and which were left alone. The filesystem half is
// covered by the watcher tests and by a live run against the agent's own workspace.
//
// The tree is a real directory with real `require` statements, not a hand-built graph, so the
// assertion covers import extraction and in-tree resolution as well as the closure walk. A
// hand-built graph would pass even if `ExtractImports` returned nothing.
func TestBuildIncrementalReport_ReIndexesTheChangedFileAndItsDependantsOnly(t *testing.T) {
	root := t.TempDir()
	write := func(rel, body string) {
		full := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatalf("mkdir for %s: %v", rel, err)
		}
		if err := os.WriteFile(full, []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", rel, err)
		}
	}

	// lib.js is imported by a.js and b.js. c.js imports a.js, so it is a TRANSITIVE dependant and must
	// also be re-indexed — a closure that stopped at direct importers would leave c.js stale.
	// unrelated.js imports nothing and must be left alone.
	write("depdemo/lib.js", "module.exports = { greet: (n) => 'hello ' + n };\n")
	write("depdemo/a.js", "const lib = require('./lib');\nmodule.exports = lib;\n")
	write("depdemo/b.js", "const lib = require('./lib');\nconsole.log(lib.greet('b'));\n")
	write("depdemo/c.js", "const a = require('./a');\nconsole.log(a);\n")
	write("depdemo/unrelated.js", "console.log('i import nothing');\n")

	redactor, err := secretscan.NewScanner()
	if err != nil {
		t.Fatalf("secret scanner: %v", err)
	}
	rs, err := NewReportScanner(1<<20, "", redactor)
	if err != nil {
		t.Fatalf("report scanner: %v", err)
	}

	ctx := context.Background()

	// The full report first, to establish that the edges are discovered at all. Without this the
	// incremental assertion could pass for the wrong reason: no edges means no fan-out, and a closure
	// of exactly one file would look like a correct narrow re-index.
	full, err := rs.BuildReport(ctx, root)
	if err != nil {
		t.Fatalf("full report: %v", err)
	}
	edges := map[string]string{}
	for _, d := range full.Dependencies {
		if d.Resolved {
			edges[d.FromPath+" -> "+d.ToPath] = d.RawSpecifier
		}
	}
	for _, want := range []string{
		"depdemo/a.js -> depdemo/lib.js",
		"depdemo/b.js -> depdemo/lib.js",
		"depdemo/c.js -> depdemo/a.js",
	} {
		if _, ok := edges[want]; !ok {
			t.Fatalf("the full scan did not resolve %q; edges were %v", want, keysOf(edges))
		}
	}

	// Now the incremental report for a change to lib.js alone.
	partial, err := rs.BuildIncrementalReport(ctx, root, []string{"depdemo/lib.js"})
	if err != nil {
		t.Fatalf("incremental report: %v", err)
	}
	if !partial.Partial {
		t.Error("an incremental report must mark itself partial, or the backend prunes everything absent from it")
	}

	got := map[string]bool{}
	for _, f := range partial.Files {
		got[f.Path] = true
	}

	// The changed file, its direct importers, and its transitive importer.
	for _, want := range []string{
		"depdemo/lib.js", "depdemo/a.js", "depdemo/b.js", "depdemo/c.js",
	} {
		if !got[want] {
			t.Errorf("%s should have been re-indexed; got %v", want, keysOf(boolKeys(got)))
		}
	}

	// AND NOTHING ELSE. This is the half that makes it incremental rather than a full scan wearing a
	// different name, and the half a test asserting only "lib.js is present" would miss entirely.
	if got["depdemo/unrelated.js"] {
		t.Error("depdemo/unrelated.js imports nothing from the closure and must not be re-indexed")
	}
	if len(partial.Files) != 4 {
		t.Errorf("want exactly the 4 closure files, got %d: %v", len(partial.Files), keysOf(boolKeys(got)))
	}

	// The closure is reported so the backend can prune the dependency rows of exactly these files
	// rather than of the whole project.
	inClosure := map[string]bool{}
	for _, p := range partial.DirtyClosure {
		inClosure[p] = true
	}
	for _, want := range []string{"depdemo/a.js", "depdemo/b.js", "depdemo/c.js"} {
		if !inClosure[want] {
			t.Errorf("%s should appear in the reported dirty closure, got %v", want, partial.DirtyClosure)
		}
	}
}

func TestBuildIncrementalReport_AChangedFileWithNoEdgesIsStillReIndexed(t *testing.T) {
	// The case the implementation calls out: `DirtyClosure` walks DEPENDENTS, so a file nothing imports
	// is absent from it. It still changed, and an incremental report that omitted it would mean the one
	// file the operator actually edited was the one file not re-read.
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "lonely.js"), []byte("console.log(1);\n"), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "other.js"), []byte("console.log(2);\n"), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}

	redactor, err := secretscan.NewScanner()
	if err != nil {
		t.Fatalf("secret scanner: %v", err)
	}
	rs, err := NewReportScanner(1<<20, "", redactor)
	if err != nil {
		t.Fatalf("report scanner: %v", err)
	}

	partial, err := rs.BuildIncrementalReport(context.Background(), root, []string{"lonely.js"})
	if err != nil {
		t.Fatalf("incremental report: %v", err)
	}
	if len(partial.Files) != 1 || partial.Files[0].Path != "lonely.js" {
		t.Fatalf("want exactly lonely.js, got %d files", len(partial.Files))
	}
}

func keysOf[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func boolKeys(m map[string]bool) map[string]bool { return m }
