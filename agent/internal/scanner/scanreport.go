// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path"
	"sort"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/scanner/chunking"
	"github.com/parag8487/ForgeOps/agent/internal/scanner/depgraph"
	"github.com/parag8487/ForgeOps/agent/internal/scanner/frameworks"
	"github.com/parag8487/ForgeOps/agent/internal/scanner/langdetect"
	"github.com/parag8487/ForgeOps/agent/internal/scanner/symbols"
	"github.com/parag8487/ForgeOps/agent/internal/secretscan"
)

// ScanReportSchemaVersion is sent with every report and checked by the backend.
//
// Versioned from the first release rather than later: the backend persists what this
// structure says, so a field that changes meaning without a version bump corrupts rows
// that already exist, and a silent partial-decode is the worst possible failure here.
const ScanReportSchemaVersion = 1

// ErrRedactorRequired is returned when a report is requested without a redactor.
//
// This is a hard refusal, not a warning. design §6.3/§7.11 place `file_contents` in the
// "redacted text only" class, and that property is what makes Q-13's cache-key clause
// enforceable: if no unredacted secret is ever stored, no cache key can be derived from
// one. A report built without a redactor would carry raw file bodies off the machine, so
// the constructor refuses instead of degrading.
var ErrRedactorRequired = errors.New("scanner: a secret redactor is required before file contents may leave the machine")

// ScanChunk is one cAST chunk, carrying the metadata `embeddings` stores.
type ScanChunk struct {
	ChunkIndex int `json:"chunk_index"`
	// Symbol and ParentSymbol are omitted rather than sent empty: `embeddings.symbol` is
	// nullable, and NULL means "this chunk is not a named declaration", which is
	// different from a declaration whose name is the empty string.
	Symbol       string `json:"symbol,omitempty"`
	ParentSymbol string `json:"parent_symbol,omitempty"`
	Signature    string `json:"signature,omitempty"`
	Kind         string `json:"kind"`
	StartLine    int    `json:"start_line"`
	EndLine      int    `json:"end_line"`
	TokenCount   int    `json:"token_count"`
	// Text is REDACTED, like the file content it is cut from.
	Text string `json:"text"`
}

// ScanFile is one indexed file.
type ScanFile struct {
	Path string `json:"path"`
	// ContentHash is sha256 of the REDACTED content, hex-encoded.
	//
	// Of the redacted form deliberately. Hashing the bytes on disk would put a value
	// derived from an unredacted secret on the wire, which is the one thing §7.11 keeps
	// out of the store. Change detection is not weakened by this: a redaction marker
	// embeds an HMAC fingerprint of the secret it replaced, so editing a secret still
	// changes the redacted text and therefore this hash.
	ContentHash    string    `json:"content_hash"`
	SizeBytes      int64     `json:"size_bytes"`
	LastModified   time.Time `json:"last_modified"`
	Language       string    `json:"language"`
	DetectionTier  int       `json:"detection_tier"`
	Content        string    `json:"content"`
	RedactionCount int       `json:"redaction_count"`
	// SymbolsSupported records whether the language has a declaration matcher, so an
	// empty Chunks symbol set can be read as "none in this file" rather than "this
	// language is not understood yet".
	SymbolsSupported bool        `json:"symbols_supported"`
	Chunks           []ScanChunk `json:"chunks"`
}

// ScanDependency is one edge of the dependency graph, resolved where possible.
type ScanDependency struct {
	FromPath string `json:"from_path"`
	// ToPath is empty when the specifier resolves outside the scanned tree. The edge is
	// still sent: `file_dependencies` keeps unresolved specifiers with `resolved=false`
	// so a later scan can resolve them without re-parsing the importer, and dropping
	// them would also erase the evidence that a third-party dependency exists.
	ToPath       string `json:"to_path,omitempty"`
	RawSpecifier string `json:"raw_specifier"`
	Kind         string `json:"kind"`
	Resolved     bool   `json:"resolved"`
}

// ScanReport is the whole payload the backend persists.
type ScanReport struct {
	SchemaVersion int       `json:"schema_version"`
	GeneratedAt   time.Time `json:"generated_at"`
	// Partial marks a report that covers only some files (a watch-mode rescan). The
	// backend prunes paths absent from a FULL report and prunes nothing from a partial
	// one — without the flag an incremental rescan of one file would delete the index.
	Partial   bool `json:"partial"`
	Inventory struct {
		Languages      []string `json:"languages"`
		Manifests      []string `json:"manifests"`
		ConfigFiles    []string `json:"config_files"`
		EntryPoints    []string `json:"entry_points"`
		FileCount      int      `json:"file_count"`
		TotalSizeBytes int64    `json:"total_size_bytes"`
		// Frameworks and PackageManagers are FR-10's answer, carried on the report rather than left in
		// the agent so the generation prompt can say "a Django project" instead of "a Python project".
		Frameworks      []frameworks.Finding `json:"frameworks"`
		PackageManagers []string             `json:"package_managers"`
	} `json:"inventory"`
	Files        []ScanFile       `json:"files"`
	Dependencies []ScanDependency `json:"dependencies"`
	// InventoryHash is sha256 over the sorted `path:content_hash` pairs. It is the
	// determinism evidence `analysis_reports.inventory_hash` stores: two scans of one
	// tree must produce the same hash, which is what lets two readiness scores be
	// compared rather than merely displayed.
	InventoryHash string `json:"inventory_hash"`
	// RedactionCount is the total across every file, so an operator can see that
	// redaction ran at all. Zero is meaningful only alongside the per-file counts.
	RedactionCount int `json:"redaction_count"`
	// DirtyClosure is the transitive set of importers affected by this report's files,
	// computed from the resolved edges. Empty for a full report, where every file is
	// already included.
	DirtyClosure []string `json:"dirty_closure,omitempty"`
}

// ReportScanner produces a ScanReport for a directory.
type ReportScanner struct {
	filtered      *FilteredScanner
	redactor      secretscan.Scanner
	targetTokens  int
	overlapTokens int
}

// NewReportScanner builds a report scanner. The redactor is mandatory (ErrRedactorRequired).
func NewReportScanner(maxSizeBytes int64, projectLang string, redactor secretscan.Scanner) (*ReportScanner, error) {
	if redactor == nil {
		return nil, ErrRedactorRequired
	}
	return &ReportScanner{
		filtered:      NewFilteredScanner(maxSizeBytes, projectLang),
		redactor:      redactor,
		targetTokens:  chunking.TargetFunctionTokens,
		overlapTokens: chunking.OverlapTokens,
	}, nil
}

// BuildReport walks targetDir and returns the full report.
//
// Redaction happens HERE, before the report exists as a value — there is deliberately no
// intermediate structure holding raw file bodies for a caller to serialise by mistake.
func (r *ReportScanner) BuildReport(ctx context.Context, targetDir string) (*ScanReport, error) {
	if r.redactor == nil {
		return nil, ErrRedactorRequired
	}

	inv, err := r.filtered.ScanDirectory(targetDir)
	if err != nil {
		return nil, fmt.Errorf("scanner: walking %s: %w", targetDir, err)
	}

	report := &ScanReport{
		SchemaVersion: ScanReportSchemaVersion,
		GeneratedAt:   time.Now().UTC(),
	}
	report.Inventory.Languages = inv.Languages
	report.Inventory.Manifests = inv.Manifests
	report.Inventory.ConfigFiles = inv.ConfigFiles
	report.Inventory.EntryPoints = inv.EntryPoints
	report.Inventory.FileCount = inv.FileCount
	report.Inventory.TotalSizeBytes = inv.TotalSizeBytes
	report.Inventory.Frameworks = inv.Frameworks
	report.Inventory.PackageManagers = inv.PackageManagers

	detector := langdetect.NewDetector("")
	res := &resolver{
		known:          make(map[string]bool),
		goModulePath:   goModulePath(targetDir),
		dirPackageFile: make(map[string]string),
	}

	// Two passes. Resolution needs the whole path set, so an import of a file that is
	// walked later would otherwise be recorded as unresolved purely because of walk
	// order — a determinism bug that only shows up as a missing edge.
	type pending struct {
		file    ScanFile
		imports []ImportRef
	}
	var staged []pending

	err = r.filtered.walkFiles(targetDir, func(f scannedFile) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		detection := detector.Detect(f.RelPath, f.Content)

		findings, scanErr := r.redactor.Scan(ctx, f.RelPath, f.Content)
		if scanErr != nil {
			// A failed secret scan must not fall through to sending the raw body. The
			// whole report fails, because a half-redacted index is worse than none.
			return fmt.Errorf("scanner: secret scan of %s: %w", f.RelPath, scanErr)
		}
		redacted := r.redactor.Redact(ctx, secretscan.Chunk{Text: string(f.Content)}, findings).Text()

		sum := sha256.Sum256([]byte(redacted))
		lines := strings.Split(redacted, "\n")
		declarations := symbols.Extract(detection.Language, []byte(redacted))

		staged = append(staged, pending{
			file: ScanFile{
				Path:             f.RelPath,
				ContentHash:      hex.EncodeToString(sum[:]),
				SizeBytes:        f.Info.Size(),
				LastModified:     f.Info.ModTime().UTC(),
				Language:         detection.Language,
				DetectionTier:    detection.Tier,
				Content:          redacted,
				RedactionCount:   len(findings),
				SymbolsSupported: symbols.Supported(detection.Language),
				Chunks:           r.chunk(lines, declarations),
			},
			imports: ExtractImports(detection.Language, []byte(redacted)),
		})

		res.known[f.RelPath] = true
		if strings.HasSuffix(f.RelPath, ".go") && !strings.HasSuffix(f.RelPath, "_test.go") {
			dir := path.Dir(f.RelPath)
			if dir == "." {
				dir = ""
			}
			// First file in lexical order represents the package, so an import of a
			// PACKAGE can be recorded as an edge to a FILE — `to_file_id` is a file
			// reference and Go has no file-level import to point at.
			if existing, ok := res.dirPackageFile[dir]; !ok || f.RelPath < existing {
				res.dirPackageFile[dir] = f.RelPath
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}

	for _, p := range staged {
		report.Files = append(report.Files, p.file)
		report.RedactionCount += p.file.RedactionCount
		for _, ref := range p.imports {
			to := res.Resolve(p.file.Language, p.file.Path, ref.Specifier)
			if to == p.file.Path {
				// A self-edge is what a package-level Go import inside the package
				// itself resolves to; it carries no information and would make every
				// file its own dependent in the closure.
				continue
			}
			report.Dependencies = append(report.Dependencies, ScanDependency{
				FromPath:     p.file.Path,
				ToPath:       to,
				RawSpecifier: ref.Specifier,
				Kind:         ref.Kind,
				Resolved:     to != "",
			})
		}
	}

	sort.Slice(report.Files, func(i, j int) bool { return report.Files[i].Path < report.Files[j].Path })
	sort.Slice(report.Dependencies, func(i, j int) bool {
		if report.Dependencies[i].FromPath != report.Dependencies[j].FromPath {
			return report.Dependencies[i].FromPath < report.Dependencies[j].FromPath
		}
		return report.Dependencies[i].RawSpecifier < report.Dependencies[j].RawSpecifier
	})
	// NEITHER LIST MAY BE NIL ON THE WIRE, for the reason `ScanChunk`'s own comment gives at length: Go
	// marshals a nil slice as `null`, `ScanReportIn` declares both as required lists, and pydantic refuses
	// `null` for one — so the backend rejects the ENTIRE report with a 422 and the index is lost for every
	// file in it.
	//
	// `TestBuildReport_NoSliceIsSerialisedAsNull` has forbidden `"dependencies":null` since it was written,
	// and the bug survived anyway: its fixture tree contains an import, so `Dependencies` was never empty
	// there and the assertion never fired. A tree with no import edges at all — a repository of manifests
	// and data files, which is a perfectly ordinary thing to scan — produced `"dependencies": null` and a
	// 422. Found by dumping a real report and feeding it to the real pydantic model, which is what
	// `cmd/reportdump` and `test_scan_report_contract.py` now do on every run.
	if report.Files == nil {
		report.Files = []ScanFile{}
	}
	if report.Dependencies == nil {
		report.Dependencies = []ScanDependency{}
	}
	report.InventoryHash = inventoryHash(report.Files)
	return report, nil
}

// BuildIncrementalReport reports only `changedPaths` plus every file that transitively
// imports them, which is §1.3's dependency-graph-aware rescan.
//
// The closure is computed from a FULL walk of the tree because the graph lives nowhere
// else: the agent holds no persistent index, so "who imports this file" can only be
// answered by re-reading the importers. Reading is cheap; embedding is not, and the
// closure is what bounds the expensive half.
func (r *ReportScanner) BuildIncrementalReport(ctx context.Context, targetDir string, changedPaths []string) (*ScanReport, error) {
	full, err := r.BuildReport(ctx, targetDir)
	if err != nil {
		return nil, err
	}

	graph := depgraph.NewDependencyGraph()
	for _, dep := range full.Dependencies {
		if dep.Resolved {
			graph.AddDependency(dep.FromPath, dep.ToPath)
		}
	}
	changed := make([]string, 0, len(changedPaths))
	for _, p := range changedPaths {
		changed = append(changed, toSlash(p))
	}
	closure := graph.DirtyClosure(changed)
	inClosure := make(map[string]bool, len(closure))
	for _, p := range closure {
		inClosure[p] = true
	}
	// A changed file with no edges at all is absent from the closure, because
	// DirtyClosure walks dependents. It still has to be re-indexed.
	for _, p := range changed {
		inClosure[p] = true
	}

	partial := &ScanReport{
		SchemaVersion:  ScanReportSchemaVersion,
		GeneratedAt:    full.GeneratedAt,
		Partial:        true,
		Inventory:      full.Inventory,
		InventoryHash:  full.InventoryHash,
		RedactionCount: 0,
	}
	for _, f := range full.Files {
		if inClosure[f.Path] {
			partial.Files = append(partial.Files, f)
			partial.RedactionCount += f.RedactionCount
		}
	}
	for _, dep := range full.Dependencies {
		if inClosure[dep.FromPath] {
			partial.Dependencies = append(partial.Dependencies, dep)
		}
	}
	partial.DirtyClosure = closure
	return partial, nil
}

// chunk cuts a file into cAST chunks: one per declaration, one per gap between
// declarations, and a constraint-based split of anything over the token target.
//
// Declarations nested inside an already-emitted declaration are skipped rather than
// emitted twice. A method's body would otherwise appear both in its own chunk and inside
// its class's chunk, and duplicate text retrieved twice is a retrieval-quality problem,
// not merely wasted space.
//
// THE RETURN IS NEVER NIL, and that is a wire contract rather than a style preference. Go
// marshals a nil slice as `null`, the backend's `ScanReportIn.chunks` is a required list, and
// pydantic refuses `null` for one — so a single zero-byte file anywhere in the tree (every
// `.gitkeep` in this repository, for instance) made the backend reject the ENTIRE report with a
// 422. A real scan of `backend/src` hit exactly that on seven files. An empty list is also the
// honest encoding: this field's own comment says an empty set reads as "none in this file", and
// `null` reads as "not known", which is a different claim.
func (r *ReportScanner) chunk(lines []string, declarations []symbols.Declaration) []ScanChunk {
	if len(lines) == 0 || (len(lines) == 1 && strings.TrimSpace(lines[0]) == "") {
		return []ScanChunk{}
	}

	sorted := make([]symbols.Declaration, len(declarations))
	copy(sorted, declarations)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].StartLine < sorted[j].StartLine })

	out := []ScanChunk{}
	index := 0
	emit := func(startLine, endLine int, decl *symbols.Declaration, kind string) {
		if startLine < 1 {
			startLine = 1
		}
		if endLine > len(lines) {
			endLine = len(lines)
		}
		if endLine < startLine {
			return
		}
		body := lines[startLine-1 : endLine]
		if strings.TrimSpace(strings.Join(body, "\n")) == "" {
			return
		}
		for _, part := range r.split(body, startLine) {
			part.ChunkIndex = index
			part.Kind = kind
			if decl != nil {
				part.Symbol = decl.Name
				part.ParentSymbol = decl.Parent
				part.Signature = decl.Signature
			}
			out = append(out, part)
			index++
		}
	}

	cursor := 1
	for i := range sorted {
		d := sorted[i]
		if d.StartLine < cursor {
			continue
		}
		if d.StartLine > cursor {
			emit(cursor, d.StartLine-1, nil, symbols.KindModule)
		}
		emit(d.StartLine, d.EndLine, &d, d.Kind)
		cursor = d.EndLine + 1
	}
	if cursor <= len(lines) {
		kind := symbols.KindBlock
		if len(sorted) == 0 {
			// No declaration anywhere: the file IS the module, so calling the whole
			// thing a `block` would lose that.
			kind = symbols.KindModule
		}
		emit(cursor, len(lines), nil, kind)
	}
	return out
}

// split applies the token constraint with the configured overlap.
//
// Overlap exists so a declaration split across two chunks does not lose the context that
// straddles the cut; §1.3 fixes it at 128 tokens against a 512-token target.
func (r *ReportScanner) split(body []string, firstLine int) []ScanChunk {
	total := chunking.EstimateTokens(strings.Join(body, "\n"))
	if total <= r.targetTokens {
		return []ScanChunk{{
			StartLine:  firstLine,
			EndLine:    firstLine + len(body) - 1,
			TokenCount: total,
			Text:       strings.Join(body, "\n"),
		}}
	}

	var parts []ScanChunk
	start := 0
	for start < len(body) {
		tokens := 0
		end := start
		for end < len(body) {
			lineTokens := chunking.EstimateTokens(body[end])
			// A single line over the target is emitted alone rather than dropped: a
			// minified file is still evidence.
			if tokens > 0 && tokens+lineTokens > r.targetTokens {
				break
			}
			tokens += lineTokens
			end++
		}
		text := strings.Join(body[start:end], "\n")
		parts = append(parts, ScanChunk{
			StartLine:  firstLine + start,
			EndLine:    firstLine + end - 1,
			TokenCount: chunking.EstimateTokens(text),
			Text:       text,
		})
		if end >= len(body) {
			break
		}
		// Step back by the overlap budget, but always make forward progress — an
		// overlap equal to the chunk size would loop forever.
		back := 0
		overlap := 0
		for i := end - 1; i > start; i-- {
			t := chunking.EstimateTokens(body[i])
			if overlap+t > r.overlapTokens {
				break
			}
			overlap += t
			back++
		}
		next := end - back
		if next <= start {
			next = end
		}
		start = next
	}
	return parts
}

// inventoryHash is sha256 over the sorted `path:hash` pairs.
func inventoryHash(files []ScanFile) string {
	pairs := make([]string, 0, len(files))
	for _, f := range files {
		pairs = append(pairs, f.Path+":"+f.ContentHash)
	}
	sort.Strings(pairs)
	sum := sha256.Sum256([]byte(strings.Join(pairs, "\n")))
	return hex.EncodeToString(sum[:])
}

// goModulePath reads the module path from go.mod, or "" when there is none.
func goModulePath(targetDir string) string {
	raw, err := os.ReadFile(path.Join(toSlash(targetDir), "go.mod"))
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(raw), "\n") {
		if rest, ok := strings.CutPrefix(strings.TrimSpace(line), "module "); ok {
			return strings.TrimSpace(rest)
		}
	}
	return ""
}
