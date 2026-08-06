// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"bytes"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/parag8487/ForgeOps/agent/internal/scanner/langdetect"
)

const DefaultMaxFileSize = 1048576 // 1MB per phases.md §1.3

type Inventory struct {
	Languages     []string          `json:"languages"`
	Manifests     []string          `json:"manifests"`
	ConfigFiles   []string          `json:"config_files"`
	EntryPoints   []string          `json:"entry_points"`
	FileCount     int               `json:"file_count"`
	TotalSizeBytes int64            `json:"total_size_bytes"`
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

// ScanDirectory recursively scans targetDir returning cold-start inventory.
func (s *FilteredScanner) ScanDirectory(targetDir string) (*Inventory, error) {
	inv := &Inventory{
		Languages:   []string{},
		Manifests:   []string{},
		ConfigFiles: []string{},
		EntryPoints: []string{},
	}
	langSet := make(map[string]bool)

	err := filepath.WalkDir(targetDir, func(path string, d fs.DirEntry, err error) error {
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

		inv.FileCount++
		inv.TotalSizeBytes += info.Size()

		res := s.detector.Detect(path, content)
		if res.Language != "unknown" {
			langSet[res.Language] = true
		}

		relPath, _ := filepath.Rel(targetDir, path)
		if res.Tier == 1 {
			inv.Manifests = append(inv.Manifests, relPath)
		}

		if strings.HasSuffix(name, ".yaml") || strings.HasSuffix(name, ".json") || strings.HasPrefix(name, ".") {
			inv.ConfigFiles = append(inv.ConfigFiles, relPath)
		}

		if name == "main.go" || name == "index.ts" || name == "main.py" || name == "app.py" || name == "server.js" {
			inv.EntryPoints = append(inv.EntryPoints, relPath)
		}

		return nil
	})

	for l := range langSet {
		inv.Languages = append(inv.Languages, l)
	}

	return inv, err
}
