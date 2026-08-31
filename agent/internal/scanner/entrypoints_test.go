// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// FR-11's classifier, tested against the two ways the five-filename rule it replaces was wrong: the entry
// points it missed, and the ones it invented.

func classify(files map[string]string) []string {
	c := newEntryPointClassifier()
	// Two passes, mirroring the walk: `present` must be complete before a declaration can be confirmed
	// against it, which is the whole reason `resolve` is separate from `consider`.
	for path := range files {
		c.present[path] = true
	}
	for path, content := range files {
		c.consider(path, []byte(content))
	}
	return c.resolve()
}

const goMain = "package main\n\nimport \"fmt\"\n\nfunc main() {\n\tfmt.Println(\"hi\")\n}\n"

// writeFile creates a file under dir, making its parent directories.
//
// Local to this file rather than shared: the sibling scanner tests build their trees inline, and a helper
// hoisted into a shared file would have to grow options for each of them.
func writeFile(t *testing.T, dir, relPath, content string) {
	t.Helper()
	full := filepath.Join(dir, filepath.FromSlash(relPath))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatalf("MkdirAll %s: %v", relPath, err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		t.Fatalf("WriteFile %s: %v", relPath, err)
	}
}

// ── the entry points the old rule missed ─────────────────────────────────────────────────────────

func TestAGoProgramIsFoundByItsStructureNotItsFilename(t *testing.T) {
	// `cmd/server/serve.go` is the real entry point of a great many Go repositories and was invisible to
	// a match against `main.go`.
	got := classify(map[string]string{
		"cmd/server/serve.go": goMain,
		"internal/repo/db.go": "package repo\n\nfunc Open() {}\n",
	})
	if !slices.Contains(got, "cmd/server/serve.go") {
		t.Errorf("entry points %v", got)
	}
	if slices.Contains(got, "internal/repo/db.go") {
		t.Error("a library file was reported as an entry point")
	}
}

func TestPackageMainWithoutAMainFunctionIsNotAnEntryPoint(t *testing.T) {
	// A build-tagged fragment or a generated stub. It does not build into a program.
	got := classify(map[string]string{
		"tools/gen.go": "//go:build ignore\n\npackage main\n\nvar x = 1\n",
	})
	if len(got) != 0 {
		t.Errorf("entry points %v", got)
	}
}

func TestAMainFunctionInALibraryPackageIsNotAnEntryPoint(t *testing.T) {
	got := classify(map[string]string{"pkg/run.go": "package pkg\n\nfunc main() {}\n"})
	if len(got) != 0 {
		t.Errorf("entry points %v", got)
	}
}

func TestAPythonDunderMainGuardIsAnEntryPoint(t *testing.T) {
	got := classify(map[string]string{
		"tools/report.py": "import sys\n\nif __name__ == \"__main__\":\n    sys.exit(0)\n",
		"pkg/__main__.py": "print('x')\n",
		"pkg/helpers.py":  "def helper():\n    return 1\n",
	})
	for _, want := range []string{"tools/report.py", "pkg/__main__.py"} {
		if !slices.Contains(got, want) {
			t.Errorf("%q missed: %v", want, got)
		}
	}
	if slices.Contains(got, "pkg/helpers.py") {
		t.Errorf("a plain module was reported: %v", got)
	}
}

func TestSingleQuotedDunderMainIsRecognised(t *testing.T) {
	got := classify(map[string]string{"run.py": "if __name__ == '__main__':\n    pass\n"})
	if len(got) != 1 {
		t.Errorf("entry points %v", got)
	}
}

func TestAJvmMainIsAnEntryPoint(t *testing.T) {
	got := classify(map[string]string{
		"src/main/java/com/acme/App.java":  "package com.acme;\npublic class App {\n  public static void main(String[] args) {}\n}\n",
		"src/main/java/com/acme/Util.java": "package com.acme;\nclass Util {}\n",
	})
	if !slices.Contains(got, "src/main/java/com/acme/App.java") {
		t.Errorf("entry points %v", got)
	}
	if len(got) != 1 {
		t.Errorf("entry points %v", got)
	}
}

func TestAVariadicJvmMainIsRecognised(t *testing.T) {
	got := classify(map[string]string{
		"App.java": "public class App { public static void main(String... args) {} }",
	})
	if len(got) != 1 {
		t.Errorf("entry points %v", got)
	}
}

func TestARustMainIsAnEntryPoint(t *testing.T) {
	got := classify(map[string]string{
		"src/main.rs": "fn main() {\n    println!(\"hi\");\n}\n",
		"src/lib.rs":  "pub fn add(a: i32) -> i32 { a }\n",
	})
	if !slices.Contains(got, "src/main.rs") || len(got) != 1 {
		t.Errorf("entry points %v", got)
	}
}

// ── declarations ─────────────────────────────────────────────────────────────────────────────────

func TestAPackageJsonMainIsHonouredWhenTheFileExists(t *testing.T) {
	got := classify(map[string]string{
		"package.json":   `{"main": "./dist/server.js"}`,
		"dist/server.js": "console.log('x')\n",
	})
	if !slices.Contains(got, "dist/server.js") {
		t.Errorf("entry points %v", got)
	}
}

func TestADeclarationNamingAnAbsentFileIsDropped(t *testing.T) {
	// A `main` field left behind by a rename would otherwise put a nonexistent path in the inventory.
	got := classify(map[string]string{
		"package.json": `{"main": "dist/server.js"}`,
	})
	if len(got) != 0 {
		t.Errorf("a declaration for a missing file was honoured: %v", got)
	}
}

func TestABinObjectIsRead(t *testing.T) {
	got := classify(map[string]string{
		"package.json": `{"bin": {"forgeops": "cli/index.js", "other": "cli/other.js"}}`,
		"cli/index.js": "#!/usr/bin/env node\n",
		"cli/other.js": "#!/usr/bin/env node\n",
	})
	if len(got) != 2 {
		t.Errorf("entry points %v", got)
	}
}

func TestABinStringIsRead(t *testing.T) {
	got := classify(map[string]string{
		"package.json": `{"bin": "cli.js"}`,
		"cli.js":       "#!/usr/bin/env node\n",
	})
	if !slices.Contains(got, "cli.js") {
		t.Errorf("entry points %v", got)
	}
}

func TestAStartScriptContributesTheFileItNames(t *testing.T) {
	got := classify(map[string]string{
		"package.json":    `{"scripts": {"start": "node server/index.js --port 3000"}}`,
		"server/index.js": "// server\n",
	})
	if !slices.Contains(got, "server/index.js") {
		t.Errorf("entry points %v", got)
	}
}

func TestAStartScriptThatRunsABundlerContributesNothing(t *testing.T) {
	got := classify(map[string]string{
		"package.json": `{"scripts": {"start": "next start"}}`,
	})
	if len(got) != 0 {
		t.Errorf("entry points %v", got)
	}
}

func TestAPyprojectConsoleScriptResolvesToTheFileThatDefinesIt(t *testing.T) {
	// The manifest names a MODULE, not a path. Emitting `src.cli:main` into a field that holds paths
	// would produce an entry point no tool could open.
	got := classify(map[string]string{
		"pyproject.toml": "[project.scripts]\nforgeops = \"src.cli:main\"\n",
		"src/cli.py":     "def main():\n    pass\n",
	})
	if !slices.Contains(got, "src/cli.py") {
		t.Errorf("entry points %v", got)
	}
	// The two candidate layouts that do not exist must not appear.
	for _, absent := range []string{"src/cli/__init__.py", "src/cli/__main__.py", "src.cli"} {
		if slices.Contains(got, absent) {
			t.Errorf("%q was emitted: %v", absent, got)
		}
	}
}

func TestACargoBinPathIsRead(t *testing.T) {
	got := classify(map[string]string{
		"Cargo.toml":      "[[bin]]\nname = \"tool\"\npath = \"src/bin/tool.rs\"\n",
		"src/bin/tool.rs": "// no fn main, but declared\n",
	})
	if !slices.Contains(got, "src/bin/tool.rs") {
		t.Errorf("entry points %v", got)
	}
}

func TestAProcfileIsItselfAnEntryPointDescription(t *testing.T) {
	got := classify(map[string]string{"Procfile": "web: granian --interface asgi src.main:app\n"})
	if !slices.Contains(got, "Procfile") {
		t.Errorf("entry points %v", got)
	}
}

// ── the entry points the old rule invented ───────────────────────────────────────────────────────

func TestAFixtureIsNotAnEntryPoint(t *testing.T) {
	// Each of these matched the five-filename rule and was reported as a way to start the application.
	got := classify(map[string]string{
		"testdata/main.go":               goMain,
		"node_modules/express/server.js": "module.exports = {}\n",
		"examples/quickstart/main.go":    goMain,
		"tests/fixtures/app.py":          "if __name__ == \"__main__\":\n    pass\n",
		"vendor/github.com/x/y/main.go":  goMain,
	})
	if len(got) != 0 {
		t.Errorf("fixtures were reported as entry points: %v", got)
	}
}

func TestAppPyAndServerJsAloneAreNotEntryPoints(t *testing.T) {
	// Both are ordinary module names, and accepting them on the filename is what made a fixture look
	// like a service. A real `app.py` with a `__main__` guard still qualifies, by the structural rule.
	got := classify(map[string]string{
		"app.py":    "from flask import Flask\napp = Flask(__name__)\n",
		"server.js": "const express = require('express')\n",
		"index.ts":  "export const x = 1\n",
	})
	if len(got) != 0 {
		t.Errorf("entry points %v", got)
	}
}

func TestAGoTestFileIsNeverAnEntryPoint(t *testing.T) {
	got := classify(map[string]string{"main_test.go": "package main\n\nfunc main() {}\n"})
	if len(got) != 0 {
		t.Errorf("entry points %v", got)
	}
}

func TestAManifestUnderNodeModulesDeclaresNothing(t *testing.T) {
	got := classify(map[string]string{
		"node_modules/left-pad/package.json": `{"main": "index.js"}`,
		"node_modules/left-pad/index.js":     "module.exports = {}\n",
	})
	if len(got) != 0 {
		t.Errorf("a dependency's entry point was reported: %v", got)
	}
}

func TestConventionalNamesAreAccepted(t *testing.T) {
	got := classify(map[string]string{
		"manage.py":   "import django\n",
		"src/wsgi.py": "application = None\n",
		"src/asgi.py": "application = None\n",
	})
	if len(got) != 3 {
		t.Errorf("entry points %v", got)
	}
}

func TestTheResultIsSortedAndDeduplicated(t *testing.T) {
	files := map[string]string{
		"cmd/b/main.go": goMain,
		"cmd/a/main.go": goMain,
		"package.json":  `{"main": "cmd/a/main.go"}`,
	}
	got := classify(files)
	if !slices.IsSorted(got) {
		t.Errorf("not sorted: %v", got)
	}
	// `cmd/a/main.go` is both structural and declared; it appears once.
	count := 0
	for _, p := range got {
		if p == "cmd/a/main.go" {
			count++
		}
	}
	if count != 1 {
		t.Errorf("%q appears %d times: %v", "cmd/a/main.go", count, got)
	}
}

func TestTheTwoExclusionStrengthsDiffer(t *testing.T) {
	// The split exists because one set could not express both rules, and this test is what established
	// that: `dist/` must not yield an INFERRED entry point, yet `"main": "./dist/server.js"` is the most
	// common way a Node project DECLARES one.
	if excludedFromEntryPoints("dist/server.js") {
		t.Error("a declared path under dist/ is excluded outright, so a package.json main cannot be honoured")
	}
	if !excludedFromInference("dist/server.js") {
		t.Error("a main.go under dist/ would be inferred as an entry point, but it is a build artefact")
	}

	// A dependency's code is excluded at BOTH strengths: its manifest describes the dependency.
	for _, foreign := range []string{"node_modules/x/index.js", "a/b/vendor/c/main.go", ".venv/lib/x.py"} {
		if !excludedFromEntryPoints(foreign) {
			t.Errorf("%q is not excluded outright", foreign)
		}
	}

	// Directories are checked, not filenames. A file NAMED `test.go` at the root is not excluded.
	if excludedFromInference("test.go") {
		t.Error("a root file was excluded on its own name")
	}
	if !excludedFromInference("test/main.go") {
		t.Error("a file under test/ was not excluded from inference")
	}
	if excludedFromInference("main.go") {
		t.Error("a bare filename was excluded")
	}
}

// ── configuration classification ─────────────────────────────────────────────────────────────────

func TestAManifestIsNotAlsoAConfigFile(t *testing.T) {
	// The previous rule was `.yaml || .json || startswith(".")`, so `package.json` was counted as both a
	// manifest and a config file and the two lists disagreed with the file count.
	if classifyConfigFile("package.json", true) {
		t.Error("a manifest was classified as configuration")
	}
}

func TestRecognisedConfigNamesAreClassified(t *testing.T) {
	for _, path := range []string{
		"tsconfig.json", ".editorconfig", ".pre-commit-config.yaml", "setup.cfg",
		"vite.config.ts", "alembic.ini", "backend/pytest.ini", "deploy/nginx.conf",
	} {
		if !classifyConfigFile(path, false) {
			t.Errorf("%q was not classified as configuration", path)
		}
	}
}

func TestConfigDirectoriesQualifyTheirContents(t *testing.T) {
	for _, path := range []string{
		"config/database.yaml", ".github/workflows/ci.yml", "k8s/deployment.yaml",
		"charts/app/values.yaml", "terraform/backend.tf.yaml",
	} {
		if !classifyConfigFile(path, false) {
			t.Errorf("%q was not classified as configuration", path)
		}
	}
}

func TestDataBuriedInSourceIsNotConfiguration(t *testing.T) {
	// A `.yaml` at the root is configuration; one under `src/` is data.
	if !classifyConfigFile("settings.yaml", false) {
		t.Error("a root yaml was not classified as configuration")
	}
	for _, path := range []string{
		"src/locales/en.json", "internal/testdata/case.yaml", "web/public/manifest.json",
	} {
		if classifyConfigFile(path, false) {
			t.Errorf("%q was classified as configuration", path)
		}
	}
}

func TestANonConfigExtensionIsNeverConfiguration(t *testing.T) {
	for _, path := range []string{"config/README.md", "config/script.py", "config/logo.png"} {
		if classifyConfigFile(path, false) {
			t.Errorf("%q was classified as configuration", path)
		}
	}
}

func TestTheInventoryUsesTheNewClassifiersEndToEnd(t *testing.T) {
	// The wiring, not the rules: an inventory built by a real walk must carry what the classifier found.
	dir := t.TempDir()
	writeFile(t, dir, "go.mod", "module example.com/demo\n\ngo 1.24\n\nrequire github.com/gin-gonic/gin v1.10.0\n")
	writeFile(t, dir, "cmd/server/serve.go", goMain)
	writeFile(t, dir, "testdata/main.go", goMain)
	writeFile(t, dir, "tsconfig.json", "{}\n")
	writeFile(t, dir, "src/locales/en.json", "{}\n")

	inv, err := NewFilteredScanner(DefaultMaxFileSize, "").ScanDirectory(dir)
	if err != nil {
		t.Fatalf("ScanDirectory: %v", err)
	}
	if !slices.Contains(inv.EntryPoints, "cmd/server/serve.go") {
		t.Errorf("entry points %v", inv.EntryPoints)
	}
	if slices.Contains(inv.EntryPoints, "testdata/main.go") {
		t.Errorf("a fixture reached the inventory: %v", inv.EntryPoints)
	}
	if !slices.Contains(inv.ConfigFiles, "tsconfig.json") {
		t.Errorf("config files %v", inv.ConfigFiles)
	}
	if slices.Contains(inv.ConfigFiles, "src/locales/en.json") {
		t.Errorf("data was classified as configuration: %v", inv.ConfigFiles)
	}
	// FR-10 travels on the same inventory.
	var names []string
	for _, f := range inv.Frameworks {
		names = append(names, f.Name)
	}
	if !slices.Contains(names, "Gin") {
		t.Errorf("frameworks %v", names)
	}
	if !slices.Contains(inv.Manifests, "go.mod") {
		t.Errorf("manifests %v", inv.Manifests)
	}
}

func TestTheInventoryIsStableAcrossRuns(t *testing.T) {
	// `inventory_hash` is determinism evidence, so the lists it is built from must not depend on map
	// iteration order.
	dir := t.TempDir()
	writeFile(t, dir, "package.json", `{"dependencies": {"express": "^4.18.2", "react": "^18.0.0"}}`)
	writeFile(t, dir, "pnpm-lock.yaml", "lockfileVersion: 9.0\n")
	writeFile(t, dir, "cmd/a/main.go", goMain)
	writeFile(t, dir, "cmd/b/main.go", goMain)

	scanner := NewFilteredScanner(DefaultMaxFileSize, "")
	first, err := scanner.ScanDirectory(dir)
	if err != nil {
		t.Fatalf("ScanDirectory: %v", err)
	}
	for range 6 {
		next, err := scanner.ScanDirectory(dir)
		if err != nil {
			t.Fatalf("ScanDirectory: %v", err)
		}
		if strings.Join(next.EntryPoints, ",") != strings.Join(first.EntryPoints, ",") {
			t.Fatalf("entry points differ: %v then %v", first.EntryPoints, next.EntryPoints)
		}
		if strings.Join(next.ConfigFiles, ",") != strings.Join(first.ConfigFiles, ",") {
			t.Fatalf("config files differ: %v then %v", first.ConfigFiles, next.ConfigFiles)
		}
		if strings.Join(next.PackageManagers, ",") != strings.Join(first.PackageManagers, ",") {
			t.Fatalf("package managers differ")
		}
		if len(next.Frameworks) != len(first.Frameworks) {
			t.Fatalf("framework count differs")
		}
		for i := range first.Frameworks {
			if next.Frameworks[i].Name != first.Frameworks[i].Name {
				t.Fatalf("framework order differs: %v then %v", first.Frameworks, next.Frameworks)
			}
		}
	}
}
