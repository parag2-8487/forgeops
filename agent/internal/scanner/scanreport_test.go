// SPDX-License-Identifier: Apache-2.0
package scanner_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/scanner"
	"github.com/parag8487/ForgeOps/agent/internal/secretscan"
)

// The same synthetic GitHub token the secretscan tests use. Reused deliberately: a
// redaction test is only evidence if the detector actually fires on the input, and this
// literal is already known to fire.
const syntheticToken = "gh" + "p" + "_" + "123456789012345678901234567890123456"

// writeTree materialises a small multi-language repository whose imports are resolvable
// within it, which is what makes the dependency assertions meaningful.
func writeTree(t *testing.T) string {
	t.Helper()
	root := t.TempDir()

	write := func(rel, body string) {
		full := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", rel, err)
		}
		if err := os.WriteFile(full, []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", rel, err)
		}
	}

	write("go.mod", "module example.com/demo\n\ngo 1.24\n")
	write("main.go", `package main

import (
	"fmt"

	"example.com/demo/internal/repo"
)

func main() {
	fmt.Println(repo.New("x"))
}
`)
	write("internal/repo/repo.go", `package repo

type Repo struct {
	dsn string
}

func New(dsn string) *Repo {
	return &Repo{dsn: dsn}
}
`)
	write("app/settings.py", `import os

from app import helpers


class Settings:
    def load(self):
        return helpers.read(os.environ)
`)
	write("app/helpers.py", "def read(env):\n    return dict(env)\n")
	// A file with a real detectable credential, so the redaction assertion is not
	// vacuous.
	write("deploy/config.py", "GITHUB_TOKEN = \""+syntheticToken+"\"\n")
	return root
}

func newReportScanner(t *testing.T) *scanner.ReportScanner {
	t.Helper()
	redactor, err := secretscan.NewScanner()
	if err != nil {
		t.Fatalf("secretscan.NewScanner: %v", err)
	}
	rs, err := scanner.NewReportScanner(scanner.DefaultMaxFileSize, "", redactor)
	if err != nil {
		t.Fatalf("NewReportScanner: %v", err)
	}
	return rs
}

func fileByPath(t *testing.T, report *scanner.ScanReport, path string) scanner.ScanFile {
	t.Helper()
	for _, f := range report.Files {
		if f.Path == path {
			return f
		}
	}
	t.Fatalf("file %q missing from the report", path)
	return scanner.ScanFile{}
}

func TestAReportWithoutARedactorIsRefused(t *testing.T) {
	// The refusal is the point: a report built without a redactor would carry raw file
	// bodies off the machine, and `file_contents` is a redacted-only store (design
	// §6.3, §7.11).
	_, err := scanner.NewReportScanner(scanner.DefaultMaxFileSize, "", nil)
	if !errors.Is(err, scanner.ErrRedactorRequired) {
		t.Fatalf("NewReportScanner(nil redactor) error = %v, want ErrRedactorRequired", err)
	}
}

func TestTheReportCarriesRealPerFileMetadata(t *testing.T) {
	root := writeTree(t)
	report, err := newReportScanner(t).BuildReport(context.Background(), root)
	if err != nil {
		t.Fatalf("BuildReport: %v", err)
	}

	if report.SchemaVersion != scanner.ScanReportSchemaVersion {
		t.Errorf("schema version = %d, want %d", report.SchemaVersion, scanner.ScanReportSchemaVersion)
	}
	if report.Partial {
		t.Error("a full report must not be marked partial; the backend prunes on that flag")
	}
	if len(report.Files) != 6 {
		paths := make([]string, 0, len(report.Files))
		for _, f := range report.Files {
			paths = append(paths, f.Path)
		}
		t.Fatalf("indexed %d files (%v), want the 6 written", len(report.Files), paths)
	}

	main := fileByPath(t, report, "main.go")
	if main.Language != "go" {
		t.Errorf("main.go language = %q, want go", main.Language)
	}
	if main.SizeBytes <= 0 {
		t.Errorf("main.go size = %d, want the size on disk", main.SizeBytes)
	}
	if main.LastModified.IsZero() {
		t.Error("main.go has no modification time")
	}
	// The hash is over the redacted content, which is the only representation that
	// leaves the machine.
	sum := sha256.Sum256([]byte(main.Content))
	if main.ContentHash != hex.EncodeToString(sum[:]) {
		t.Errorf("main.go content_hash does not match sha256 of the redacted content")
	}
	if report.InventoryHash == "" {
		t.Error("the report carries no inventory hash")
	}
	// Slash-separated, so one repository indexes identically on Windows and Linux —
	// otherwise `uq_file_tree_project_path` holds two rows for one file.
	for _, f := range report.Files {
		if strings.Contains(f.Path, "\\") {
			t.Errorf("path %q is not slash-separated", f.Path)
		}
	}
}

func TestNoSecretLeavesTheAgentInTheReport(t *testing.T) {
	root := writeTree(t)
	report, err := newReportScanner(t).BuildReport(context.Background(), root)
	if err != nil {
		t.Fatalf("BuildReport: %v", err)
	}

	if report.RedactionCount == 0 {
		t.Fatal("no redactions recorded; the detector must fire on the synthetic token")
	}
	config := fileByPath(t, report, "deploy/config.py")
	if config.RedactionCount == 0 {
		t.Error("deploy/config.py records no redaction")
	}
	if !strings.Contains(config.Content, "FORGEOPS_REDACTED:") {
		t.Errorf("deploy/config.py content carries no redaction marker: %q", config.Content)
	}

	// Serialised, because serialisation is what actually leaves the process: a field
	// added later that forwards raw content would be caught here and nowhere else.
	encoded, err := json.Marshal(report)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if strings.Contains(string(encoded), syntheticToken) {
		t.Fatal("the serialised report contains the raw secret")
	}
}

func TestChunksCarrySymbolKindAndLineRange(t *testing.T) {
	root := writeTree(t)
	report, err := newReportScanner(t).BuildReport(context.Background(), root)
	if err != nil {
		t.Fatalf("BuildReport: %v", err)
	}

	repo := fileByPath(t, report, "internal/repo/repo.go")
	if !repo.SymbolsSupported {
		t.Error("go must be reported as a language with symbol support")
	}
	if len(repo.Chunks) == 0 {
		t.Fatal("repo.go produced no chunks")
	}

	seen := map[string]scanner.ScanChunk{}
	indexes := map[int]bool{}
	for _, c := range repo.Chunks {
		if indexes[c.ChunkIndex] {
			// `uq_embeddings_file_chunk` makes a duplicate index a constraint
			// violation on the ingest path rather than a silent overwrite.
			t.Errorf("chunk_index %d appears twice", c.ChunkIndex)
		}
		indexes[c.ChunkIndex] = true
		if c.Kind == "" {
			t.Errorf("chunk %d has no kind", c.ChunkIndex)
		}
		if c.StartLine < 1 || c.EndLine < c.StartLine {
			t.Errorf("chunk %d has line range %d..%d", c.ChunkIndex, c.StartLine, c.EndLine)
		}
		if c.TokenCount <= 0 {
			t.Errorf("chunk %d has token_count %d", c.ChunkIndex, c.TokenCount)
		}
		if c.Symbol != "" {
			seen[c.Symbol] = c
		}
	}

	newChunk, ok := seen["New"]
	if !ok {
		t.Fatalf("no chunk carries the symbol New; got %v", seen)
	}
	if newChunk.Kind != "function" {
		t.Errorf("New chunk kind = %q, want function", newChunk.Kind)
	}
	if !strings.Contains(newChunk.Text, "func New(") {
		t.Errorf("the New chunk does not contain its own declaration: %q", newChunk.Text)
	}

	settings := fileByPath(t, report, "app/settings.py")
	var loadChunk *scanner.ScanChunk
	for i := range settings.Chunks {
		if settings.Chunks[i].Symbol == "Settings" {
			loadChunk = &settings.Chunks[i]
		}
	}
	if loadChunk == nil {
		t.Fatalf("no chunk carries the Settings class")
	}
	if loadChunk.Kind != "class" {
		t.Errorf("Settings chunk kind = %q, want class", loadChunk.Kind)
	}
}

func TestDependencyEdgesResolveInTreeAndKeepTheRest(t *testing.T) {
	root := writeTree(t)
	report, err := newReportScanner(t).BuildReport(context.Background(), root)
	if err != nil {
		t.Fatalf("BuildReport: %v", err)
	}

	var goResolved, goUnresolved, pyResolved bool
	for _, d := range report.Dependencies {
		switch {
		case d.FromPath == "main.go" && d.RawSpecifier == "example.com/demo/internal/repo":
			goResolved = d.Resolved && d.ToPath == "internal/repo/repo.go"
			if !goResolved {
				t.Errorf("in-tree Go import resolved to %q (resolved=%v)", d.ToPath, d.Resolved)
			}
		case d.FromPath == "main.go" && d.RawSpecifier == "fmt":
			// Kept as unresolved rather than dropped: the edge is the evidence that a
			// third-party dependency exists.
			goUnresolved = !d.Resolved && d.ToPath == ""
			if !goUnresolved {
				t.Errorf("stdlib import recorded as resolved to %q", d.ToPath)
			}
		case d.FromPath == "app/settings.py" && d.RawSpecifier == "app.helpers":
			pyResolved = d.Resolved && d.ToPath == "app/helpers.py"
			if !pyResolved {
				t.Errorf("python import resolved to %q (resolved=%v)", d.ToPath, d.Resolved)
			}
		}
		if d.Kind == "" {
			t.Errorf("edge %+v has no kind", d)
		}
	}
	if !goResolved || !goUnresolved || !pyResolved {
		t.Fatalf("missing expected edges in %+v", report.Dependencies)
	}
}

func TestTwoScansOfOneTreeAgree(t *testing.T) {
	root := writeTree(t)
	rs := newReportScanner(t)

	first, err := rs.BuildReport(context.Background(), root)
	if err != nil {
		t.Fatalf("first BuildReport: %v", err)
	}
	second, err := rs.BuildReport(context.Background(), root)
	if err != nil {
		t.Fatalf("second BuildReport: %v", err)
	}

	// Determinism evidence: `analysis_reports.inventory_hash` is what lets one readiness
	// score be compared with another rather than merely displayed, so a hash that varies
	// between identical scans makes the comparison meaningless.
	if first.InventoryHash != second.InventoryHash {
		t.Errorf("inventory hash differs between scans: %s vs %s", first.InventoryHash, second.InventoryHash)
	}
	if len(first.Files) != len(second.Files) {
		t.Fatalf("file counts differ: %d vs %d", len(first.Files), len(second.Files))
	}
	for i := range first.Files {
		if first.Files[i].Path != second.Files[i].Path {
			t.Errorf("file order differs at %d: %s vs %s", i, first.Files[i].Path, second.Files[i].Path)
		}
	}
}

func TestAnIncrementalReportCoversTheDependencyClosure(t *testing.T) {
	root := writeTree(t)
	report, err := newReportScanner(t).BuildIncrementalReport(context.Background(), root, []string{"internal/repo/repo.go"})
	if err != nil {
		t.Fatalf("BuildIncrementalReport: %v", err)
	}

	if !report.Partial {
		t.Error("an incremental report must be marked partial, or the backend prunes the rest of the index")
	}
	paths := map[string]bool{}
	for _, f := range report.Files {
		paths[f.Path] = true
	}
	if !paths["internal/repo/repo.go"] {
		t.Error("the changed file is absent from its own rescan")
	}
	// The importer, not just the changed file: §1.3's rescan is dependency-graph-aware
	// precisely so a change to a dependency re-indexes what depends on it.
	if !paths["main.go"] {
		t.Errorf("main.go imports the changed file but was not re-indexed; got %v", paths)
	}
	if paths["app/helpers.py"] {
		t.Errorf("an unrelated file was included: %v", paths)
	}
}

func TestALargeDeclarationIsSplitUnderTheTokenTarget(t *testing.T) {
	root := t.TempDir()
	var body strings.Builder
	body.WriteString("package big\n\nfunc Huge() {\n")
	for i := 0; i < 400; i++ {
		body.WriteString("\tone := two + three + four + five + six\n")
	}
	body.WriteString("}\n")
	if err := os.WriteFile(filepath.Join(root, "big.go"), []byte(body.String()), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}

	report, err := newReportScanner(t).BuildReport(context.Background(), root)
	if err != nil {
		t.Fatalf("BuildReport: %v", err)
	}
	big := fileByPath(t, report, "big.go")

	var parts int
	for _, c := range big.Chunks {
		if c.Symbol == "Huge" {
			parts++
			if c.TokenCount > 2*512 {
				t.Errorf("chunk %d has %d tokens, far over the 512 target", c.ChunkIndex, c.TokenCount)
			}
		}
	}
	if parts < 2 {
		t.Errorf("a 400-line function produced %d chunks; constraint-based splitting did not run", parts)
	}
}
