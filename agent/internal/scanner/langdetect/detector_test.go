// SPDX-License-Identifier: Apache-2.0
package langdetect

import (
	"testing"
)

func TestLanguageDetectorTiers(t *testing.T) {
	detector := NewDetector("python")

	tests := []struct {
		name         string
		filename     string
		content      string
		expectedLang string
		expectedTier int
	}{
		// Tier 1: Manifest
		{"Go manifest", "go.mod", "module foo", "go", 1},
		{"Python manifest", "pyproject.toml", "[tool.poetry]", "python", 1},
		{"Dockerfile manifest", "Dockerfile", "FROM alpine", "dockerfile", 1},

		// Tier 2: Extensions
		{"TypeScript ext", "index.ts", "const x = 1;", "typescript", 2},
		{"Rust ext", "main.rs", "fn main() {}", "rust", 2},
		{"YAML ext", "deploy.yaml", "key: value", "yaml", 2},

		// Tier 3: Shebang
		{"Python shebang", "script", "#!/usr/bin/env python3\nprint(1)", "python", 3},
		{"Node shebang", "cli", "#!/usr/bin/env node\nconsole.log(1)", "javascript", 3},

		// Tier 4: Content Heuristic
		{"Go content heuristic", "unknown_file", "package main\nimport \"fmt\"\n", "go", 4},
		{"Rust content heuristic", "script_no_ext", "fn main() { use std::io; }", "rust", 4},

		// Fallback / Tie break
		{"No signal fallback", "random.bin", "binary blob data", "python", 4},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			res := detector.Detect(tt.filename, []byte(tt.content))
			if res.Language != tt.expectedLang {
				t.Errorf("expected language %q, got %q", tt.expectedLang, res.Language)
			}
			if res.Tier != tt.expectedTier {
				t.Errorf("expected tier %d, got %d", tt.expectedTier, res.Tier)
			}
		})
	}
}
