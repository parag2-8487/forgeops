// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"bytes"
	"io/fs"
	"os"
	"path/filepath"
	"sort"

	"github.com/parag8487/ForgeOps/agent/internal/scanner/frameworks"
	"github.com/parag8487/ForgeOps/agent/internal/scanner/langdetect"
)

const DefaultMaxFileSize = 1048576 // 1MB per phases.md §1.3

type Inventory struct {
	Languages      []string `json:"languages"`
	Manifests      []string `json:"manifests"`
	ConfigFiles    []string `json:"config_files"`
	EntryPoints    []string `json:"entry_points"`
	FileCount      int      `json:"file_count"`
	TotalSizeBytes int64    `json:"total_size_bytes"`
	// Frameworks is what the project is built with (FR-10). Each finding carries the manifest it was
	// read from, so an operator can check the conclusion rather than take it.
	Frameworks []frameworks.Finding `json:"frameworks"`
	// PackageManagers is derived from the lock files present, which is the only reliable evidence: a
	// `package.json` is compatible with npm, pnpm, yarn and bun, and the lock file names exactly one.
	PackageManagers []string `json:"package_managers"`
}

type FilteredScanner struct {
	MaxSizeBytes int64
	detector     *langdetect.Detector
}

func NewFilteredScanner(maxSizeBytes int64, projectLang string) *FilteredScanner {
	if maxSizeBytes <= 0 {
		maxSizeBytes = DefaultMaxFileSize
	}
	return &FilteredScanner{
		MaxSizeBytes: maxSizeBytes,
		detector:     langdetect.NewDetector(projectLang),
	}
}

// IsBinary returns true if content appears to be binary data (contains null bytes in first 512 bytes).
func IsBinary(content []byte) bool {
	sample := content
	if len(sample) > 512 {
		sample = sample[:512]
	}
	return bytes.IndexByte(sample, 0) != -1
}

// scannedFile is one file that passed every filter, handed to a WalkFiles callback.
type scannedFile struct {
	// RelPath is slash-separated and relative to the scan root, which is the form
	// `file_tree.path` stores. A backslash form would make the same repository index
	// differently on Windows and Linux, and `uq_file_tree_project_path` would then hold
	// two rows for one file.
	RelPath string
	Info    fs.FileInfo
	Content []byte
}

// walkFiles applies every §1.3 filter once, in one place.
//
// Extracted from ScanDirectory because the scan report needs exactly the same set of
// files as the inventory. Two independent walks would drift — a file counted in
// `Inventory.FileCount` but absent from `file_tree` is a discrepancy no test would catch,
// since neither number is wrong on its own.
func (s *FilteredScanner) walkFiles(targetDir string, visit func(scannedFile) error) error {
	return filepath.WalkDir(targetDir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil // Skip unreadable entries
		}

		name := d.Name()

		// Always skip .git and node_modules
		if d.IsDir() {
			if name == ".git" || name == "node_modules" || name == ".pytest_cache" || name == ".ruff_cache" {
				return filepath.SkipDir
			}
			return nil
		}

		// Handle symlinks or special non-regular files
		info, err := d.Info()
		if err != nil || !info.Mode().IsRegular() {
			return nil
		}

		// Size filter
		if info.Size() > s.MaxSizeBytes {
			return nil
		}

		// FR-09. The blocklist is consulted BEFORE the file is opened, not after it is read and
		// redacted. `.env`, `*.pem`, and anything under `~/.ssh` or `~/.aws` are never read into
		// memory at all. This walk previously called os.ReadFile unconditionally and relied on the
		// downstream redactor to remove whatever it recognised, which is a mitigation rather than a
		// control: a redactor is a pattern matcher, and the value had already been copied.
		if blockedForRead(path) {
			return nil
		}

		// Read head bytes for binary check and detection
		content, err := os.ReadFile(path)
		if err != nil {
			return nil
		}

		if IsBinary(content) {
			return nil
		}

		relPath, err := filepath.Rel(targetDir, path)
		if err != nil {
			return nil
		}
		return visit(scannedFile{
			RelPath: filepath.ToSlash(relPath),
			Info:    info,
			Content: content,
		})
	})
}

// ScanDirectory recursively scans targetDir returning cold-start inventory.
func (s *FilteredScanner) ScanDirectory(targetDir string) (*Inventory, error) {
	inv := &Inventory{
		Languages:   []string{},
		Manifests:   []string{},
		ConfigFiles: []string{},
		EntryPoints: []string{},
	}
	langSet := make(map[string]bool)
	entries := newEntryPointClassifier()
	// Contents are retained ONLY for the manifests, and only so framework detection can read the
	// dependency declarations without a second walk. Holding every file would put the whole tree in
	// memory for a report that needs a dozen files.
	manifestContents := map[string][]byte{}

	err := s.walkFiles(targetDir, func(f scannedFile) error {
		inv.FileCount++
		inv.TotalSizeBytes += f.Info.Size()

		res := s.detector.Detect(f.RelPath, f.Content)
		if res.Language != "unknown" {
			langSet[res.Language] = true
		}

		isManifest := res.Tier == 1
		if isManifest {
			inv.Manifests = append(inv.Manifests, f.RelPath)
			manifestContents[f.RelPath] = f.Content
		}

		// A narrower rule than `.yaml || .json || startswith(".")`, which classified every manifest,
		// every Kubernetes object and every `.gitignore` as configuration — and counted manifests
		// twice, since the branch above had already claimed them.
		if classifyConfigFile(f.RelPath, isManifest) {
			inv.ConfigFiles = append(inv.ConfigFiles, f.RelPath)
		}

		// Entry points are established from declarations and code structure (FR-11), replacing a match
		// against five hardcoded filenames that missed every `cmd/server/serve.go` and accepted every
		// `app.py` fixture.
		entries.consider(f.RelPath, f.Content)
		return nil
	})

	for l := range langSet {
		inv.Languages = append(inv.Languages, l)
	}
	// Sorted so two scans of one tree produce byte-identical inventories. Map iteration
	// order is randomised in Go, which would otherwise make `inventory_hash` — the
	// determinism evidence `analysis_reports` stores — differ between runs of the same
	// scan.
	sort.Strings(inv.Languages)
	sort.Strings(inv.Manifests)
	sort.Strings(inv.ConfigFiles)
	inv.EntryPoints = entries.resolve()

	// Framework detection (FR-10) reads the manifests this walk already collected, so it costs no
	// additional I/O. `entries.present` is the authoritative file set, which is what lets a layout
	// signal be checked without a stat call.
	report := frameworks.Detect(&walkedTree{contents: manifestContents, present: entries.present}, inv.Manifests)
	inv.Frameworks = report.Findings
	inv.PackageManagers = report.PackageManagers

	return inv, err
}

// walkedTree exposes the files a walk saw to the framework detector.
//
// A `FileReader` over what is already in memory rather than over the filesystem, so detection cannot read
// a file the scanner excluded — an ignored `node_modules/**/package.json` must not contribute a framework,
// and a filesystem-backed reader would happily open it.
type walkedTree struct {
	contents map[string][]byte
	present  map[string]bool
}

func (t *walkedTree) ReadFile(relPath string) ([]byte, bool) {
	content, ok := t.contents[relPath]
	return content, ok
}

func (t *walkedTree) Exists(relPath string) bool {
	return t.present[relPath]
}
