// SPDX-License-Identifier: Apache-2.0
package app

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestDependencyBoundary verifies that go.mod contains exactly the expected
// direct dependencies and excludes forbidden ones. This ensures the dependency
// matrix stays controlled.
func TestDependencyBoundary(t *testing.T) {
	modFile := filepath.Join("..", "..", "go.mod")
	data, err := os.ReadFile(modFile)
	if err != nil {
		t.Fatalf("read go.mod: %v", err)
	}
	content := string(data)

	// Must NOT contain — these were explicitly rejected or replaced.
	forbidden := []string{
		"tree-sitter",
		"nhooyr.io/websocket",
	}
	for _, f := range forbidden {
		if strings.Contains(content, f) {
			t.Errorf("go.mod contains forbidden dependency %q", f)
		}
	}

	// Must contain — all direct dependencies from the dependency matrix.
	required := []string{
		"github.com/coder/websocket",
		"github.com/docker/docker",
		"k8s.io/client-go",
		"go.uber.org/zap",
		"github.com/spf13/cobra",
		"github.com/fsnotify/fsnotify",
		"github.com/minio/selfupdate",
		"github.com/sergi/go-diff",
		"github.com/mark3labs/mcp-go",
		"github.com/go-git/go-git/v5",
		"github.com/google/go-github",
		"golang.org/x/sync",
		"pgregory.net/rapid",
	}
	for _, r := range required {
		if !strings.Contains(content, r) {
			t.Errorf("go.mod missing required dependency %q", r)
		}
	}
}
