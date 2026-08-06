// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"os"
	"path/filepath"
	"testing"
)

func TestFilteredScanner(t *testing.T) {
	tempDir, err := os.MkdirTemp("", "scanner_test_*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	// Create test directory structure
	os.MkdirAll(filepath.Join(tempDir, ".git"), 0755)
	os.WriteFile(filepath.Join(tempDir, ".git", "HEAD"), []byte("ref: refs/heads/main"), 0644)

	os.MkdirAll(filepath.Join(tempDir, "node_modules", "package"), 0755)
	os.WriteFile(filepath.Join(tempDir, "node_modules", "package", "index.js"), []byte("console.log('skip')"), 0644)

	// Create valid files
	os.WriteFile(filepath.Join(tempDir, "package.json"), []byte(`{"name": "test"}`), 0644)
	os.WriteFile(filepath.Join(tempDir, "main.go"), []byte("package main\nfunc main() {}\n"), 0644)
	os.WriteFile(filepath.Join(tempDir, "config.yaml"), []byte("app: test\n"), 0644)

	// Create oversized file (> 100 bytes for custom limit)
	largeContent := make([]byte, 200)
	os.WriteFile(filepath.Join(tempDir, "large.txt"), largeContent, 0644)

	// Create binary file
	binaryContent := []byte{0x00, 0x01, 0x02, 0x03, 0x00}
	os.WriteFile(filepath.Join(tempDir, "binary.bin"), binaryContent, 0644)

	scanner := NewFilteredScanner(150, "go")
	inv, err := scanner.ScanDirectory(tempDir)
	if err != nil {
		t.Fatalf("ScanDirectory failed: %v", err)
	}

	if inv.FileCount != 3 { // package.json, main.go, config.yaml
		t.Errorf("expected 3 files, got %d", inv.FileCount)
	}

	if len(inv.Manifests) == 0 {
		t.Errorf("expected manifest package.json to be detected")
	}

	if len(inv.EntryPoints) == 0 {
		t.Errorf("expected entry point main.go to be detected")
	}
}
