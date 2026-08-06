// SPDX-License-Identifier: Apache-2.0
package langdetect

import (
	"bytes"
	"path/filepath"
	"strings"
)

type DetectionResult struct {
	Language string
	Tier     int    // 1: Manifest, 2: Extension, 3: Shebang, 4: Content Heuristic
	Source   string // Explanatory description
}

type Detector struct {
	projectLanguage string
}

func NewDetector(projectLanguage string) *Detector {
	return &Detector{projectLanguage: projectLanguage}
}

// Detect determines language according to the four-tier strategy in phases.md §10.8.1.
func (d *Detector) Detect(filename string, content []byte) DetectionResult {
	base := filepath.Base(filename)
	ext := strings.ToLower(filepath.Ext(filename))

	// Tier 1: Package manager / manifest files
	switch base {
	case "package.json":
		return DetectionResult{Language: "javascript", Tier: 1, Source: "manifest: package.json"}
	case "go.mod", "go.sum":
		return DetectionResult{Language: "go", Tier: 1, Source: "manifest: go.mod"}
	case "requirements.txt", "Pipfile", "pyproject.toml":
		return DetectionResult{Language: "python", Tier: 1, Source: "manifest: python"}
	case "Cargo.toml", "Cargo.lock":
		return DetectionResult{Language: "rust", Tier: 1, Source: "manifest: cargo"}
	case "pom.xml", "build.gradle", "build.gradle.kts":
		return DetectionResult{Language: "java", Tier: 1, Source: "manifest: java/gradle"}
	case "Gemfile":
		return DetectionResult{Language: "ruby", Tier: 1, Source: "manifest: gemfile"}
	case "composer.json":
		return DetectionResult{Language: "php", Tier: 1, Source: "manifest: composer"}
	case "Dockerfile":
		return DetectionResult{Language: "dockerfile", Tier: 1, Source: "manifest: Dockerfile"}
	}

	// Tier 2: Extension mapping
	switch ext {
	case ".js", ".mjs", ".cjs":
		return DetectionResult{Language: "javascript", Tier: 2, Source: "extension: .js"}
	case ".ts":
		return DetectionResult{Language: "typescript", Tier: 2, Source: "extension: .ts"}
	case ".tsx":
		return DetectionResult{Language: "tsx", Tier: 2, Source: "extension: .tsx"}
	case ".py":
		return DetectionResult{Language: "python", Tier: 2, Source: "extension: .py"}
	case ".go":
		return DetectionResult{Language: "go", Tier: 2, Source: "extension: .go"}
	case ".rs":
		return DetectionResult{Language: "rust", Tier: 2, Source: "extension: .rs"}
	case ".java":
		return DetectionResult{Language: "java", Tier: 2, Source: "extension: .java"}
	case ".kt", ".kts":
		return DetectionResult{Language: "kotlin", Tier: 2, Source: "extension: .kt"}
	case ".rb":
		return DetectionResult{Language: "ruby", Tier: 2, Source: "extension: .rb"}
	case ".php":
		return DetectionResult{Language: "php", Tier: 2, Source: "extension: .php"}
	case ".cs":
		return DetectionResult{Language: "csharp", Tier: 2, Source: "extension: .cs"}
	case ".yaml", ".yml":
		return DetectionResult{Language: "yaml", Tier: 2, Source: "extension: .yaml"}
	case ".tf", ".hcl":
		return DetectionResult{Language: "hcl", Tier: 2, Source: "extension: .hcl"}
	}

	// Tier 3: Shebang header check (bounded to first line)
	if bytes.HasPrefix(content, []byte("#!")) {
		firstLine := content
		if idx := bytes.IndexByte(content, '\n'); idx != -1 {
			firstLine = content[:idx]
		}
		shebang := string(firstLine)
		if strings.Contains(shebang, "python") {
			return DetectionResult{Language: "python", Tier: 3, Source: "shebang: python"}
		}
		if strings.Contains(shebang, "node") {
			return DetectionResult{Language: "javascript", Tier: 3, Source: "shebang: node"}
		}
		if strings.Contains(shebang, "ruby") {
			return DetectionResult{Language: "ruby", Tier: 3, Source: "shebang: ruby"}
		}
		if strings.Contains(shebang, "bash") || strings.Contains(shebang, "sh") {
			return DetectionResult{Language: "bash", Tier: 3, Source: "shebang: shell"}
		}
	}

	// Tier 4: Content heuristics (bounded to first 8 KiB)
	bound := len(content)
	if bound > 8192 {
		bound = 8192
	}
	sample := content[:bound]

	if bytes.Contains(sample, []byte("package main")) || bytes.Contains(sample, []byte("import \"fmt\"")) {
		return DetectionResult{Language: "go", Tier: 4, Source: "heuristic: go keywords"}
	}
	if bytes.Contains(sample, []byte("def ")) && bytes.Contains(sample, []byte("import ")) {
		return DetectionResult{Language: "python", Tier: 4, Source: "heuristic: python keywords"}
	}
	if bytes.Contains(sample, []byte("fn main()")) || bytes.Contains(sample, []byte("use std::")) {
		return DetectionResult{Language: "rust", Tier: 4, Source: "heuristic: rust keywords"}
	}

	// Tie-breaking toward project language if specified
	if d.projectLanguage != "" {
		return DetectionResult{Language: d.projectLanguage, Tier: 4, Source: "fallback: project language default"}
	}

	return DetectionResult{Language: "unknown", Tier: 4, Source: "fallback: no signal"}
}
