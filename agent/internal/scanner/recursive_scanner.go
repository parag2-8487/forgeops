// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"bytes"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

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

	err := s.walkFiles(targetDir, func(f scannedFile) error {
		name := filepath.Base(f.RelPath)

		inv.FileCount++
		inv.TotalSizeBytes += f.Info.Size()

		res := s.detector.Detect(f.RelPath, f.Content)
		if res.Language != "unknown" {
			langSet[res.Language] = true
		}

		if res.Tier == 1 {
			inv.Manifests = append(inv.Manifests, f.RelPath)
		}

		if strings.HasSuffix(name, ".yaml") || strings.HasSuffix(name, ".json") || strings.HasPrefix(name, ".") {
			inv.ConfigFiles = append(inv.ConfigFiles, f.RelPath)
		}

		if name == "main.go" || name == "index.ts" || name == "main.py" || name == "app.py" || name == "server.js" {
			inv.EntryPoints = append(inv.EntryPoints, f.RelPath)
		}

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

	return inv, err
}
