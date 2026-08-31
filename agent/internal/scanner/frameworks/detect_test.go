// SPDX-License-Identifier: Apache-2.0
package frameworks

import (
	"strings"
	"testing"
)

// The tests are written around the false positives a substring search produces, because that is the
// difference between this package and the `strings.Contains` version it would have been.

type fakeTree struct {
	files map[string]string
}

func (t fakeTree) ReadFile(relPath string) ([]byte, bool) {
	content, ok := t.files[relPath]
	return []byte(content), ok
}

func (t fakeTree) Exists(relPath string) bool {
	_, ok := t.files[relPath]
	return ok
}

func detect(t *testing.T, files map[string]string) Report {
	t.Helper()
	var manifests []string
	for path := range files {
		switch {
		case strings.HasSuffix(path, "package.json"), strings.HasSuffix(path, "pyproject.toml"),
			strings.HasSuffix(path, "requirements.txt"), strings.HasSuffix(path, "go.mod"),
			strings.HasSuffix(path, "pom.xml"), strings.HasSuffix(path, "Cargo.toml"),
			strings.HasSuffix(path, "Gemfile"), strings.HasSuffix(path, "composer.json"),
			strings.HasSuffix(path, "build.gradle"), strings.HasSuffix(path, "Dockerfile"),
			strings.HasSuffix(path, "docker-compose.yml"), strings.HasSuffix(path, "Pipfile"):
			manifests = append(manifests, path)
		}
	}
	return Detect(fakeTree{files: files}, manifests)
}

func has(report Report, name string) bool {
	for _, f := range report.Findings {
		if f.Name == name {
			return true
		}
	}
	return false
}

func finding(t *testing.T, report Report, name string) Finding {
	t.Helper()
	for _, f := range report.Findings {
		if f.Name == name {
			return f
		}
	}
	t.Fatalf("%q was not detected; findings: %v", name, report.Names())
	return Finding{}
}

// ── the false positives a text search would produce ──────────────────────────────────────────────

func TestNodeDependenciesAreReadFromTheMapsNotTheText(t *testing.T) {
	// Every one of these mentions a framework name in a field that is not a dependency. A search over
	// the file text would report React, Express and Vue for a project that uses none of them.
	report := detect(t, map[string]string{
		"package.json": `{
			"name": "react-lookalike",
			"description": "A tool for migrating away from express and vue",
			"homepage": "https://github.com/acme/react-helpers",
			"keywords": ["react", "vue", "express"],
			"scripts": {"build": "echo not really webpack"},
			"dependencies": {"lodash": "^4.17.21"}
		}`,
	})
	for _, absent := range []string{"React", "Express", "Vue", "webpack"} {
		if has(report, absent) {
			t.Errorf("%q was detected from prose rather than a dependency", absent)
		}
	}
}

func TestAnExactDependencyNameIsRequired(t *testing.T) {
	// `strings.Contains(deps, "react")` fires on all three of these.
	report := detect(t, map[string]string{
		"package.json": `{"dependencies": {
			"@types/react": "^18.0.0",
			"react-native-web-stub": "1.0.0",
			"preact": "^10.0.0"
		}}`,
	})
	if has(report, "React") {
		t.Error("React was detected from a lookalike dependency name")
	}
}

func TestADeclaredDependencyIsDetectedWithItsConstraint(t *testing.T) {
	report := detect(t, map[string]string{
		"package.json": `{"dependencies": {"express": "^4.18.2", "next": "14.2.3"}}`,
	})
	express := finding(t, report, "Express")
	if express.Confidence != Declared {
		t.Errorf("confidence %q, want declared", express.Confidence)
	}
	// The constraint verbatim, not a resolved version: resolving would mean running the package
	// manager against the operator's tree.
	if express.Version != "^4.18.2" {
		t.Errorf("version %q, want the declared constraint", express.Version)
	}
	if express.Evidence != "package.json" {
		t.Errorf("evidence %q", express.Evidence)
	}
	if express.Kind != KindWeb {
		t.Errorf("kind %q, want web", express.Kind)
	}
	if finding(t, report, "Next.js").Kind != KindFrontend {
		t.Error("Next.js is not classified as frontend")
	}
}

func TestDevAndPeerDependenciesCount(t *testing.T) {
	report := detect(t, map[string]string{
		"package.json": `{
			"devDependencies": {"vitest": "^1.0.0"},
			"peerDependencies": {"react": "^18.0.0"}
		}`,
	})
	if !has(report, "Vitest") || !has(report, "React") {
		t.Errorf("findings %v", report.Names())
	}
}

func TestAMalformedManifestYieldsNothingRatherThanAGuess(t *testing.T) {
	// A guess drawn from a file the runtime itself cannot parse is not evidence.
	report := detect(t, map[string]string{
		"package.json": `{"dependencies": {"express": "^4.18.2"`, // truncated
	})
	if has(report, "Express") {
		t.Error("a framework was reported from an unparsable manifest")
	}
}

func TestCorepackPackageManagerIsRead(t *testing.T) {
	report := detect(t, map[string]string{
		"package.json": `{"packageManager": "pnpm@9.1.0", "dependencies": {}}`,
	})
	if !has(report, "pnpm") {
		t.Errorf("findings %v", report.Names())
	}
}

// ── Python ───────────────────────────────────────────────────────────────────────────────────────

func TestPyProjectReadsOnlyDependencySections(t *testing.T) {
	// `flask` appears in a ruff per-file-ignore and `django` in a pytest setting. A whole-file search
	// reports both.
	report := detect(t, map[string]string{
		"pyproject.toml": `[project]
name = "demo"
dependencies = ["fastapi>=0.110", "granian==1.6.0"]

[tool.ruff.lint.per-file-ignores]
"flask_compat.py" = ["E501"]

[tool.pytest.ini_options]
env = ["DJANGO_SETTINGS_MODULE=django.conf"]
`,
	})
	if !has(report, "FastAPI") || !has(report, "Granian") {
		t.Errorf("declared dependencies missed: %v", report.Names())
	}
	for _, absent := range []string{"Flask", "Django"} {
		if has(report, absent) {
			t.Errorf("%q was detected from a tool configuration table", absent)
		}
	}
}

func TestRequirementsCommentsAreNotDependencies(t *testing.T) {
	report := detect(t, map[string]string{
		"requirements.txt": "# django was removed in the 2.0 migration\nflask==3.0.0\n",
	})
	if has(report, "Django") {
		t.Error("a commented-out dependency was detected")
	}
	if !has(report, "Flask") {
		t.Errorf("findings %v", report.Names())
	}
}

func TestUnderscoresAndCaseAreNormalised(t *testing.T) {
	report := detect(t, map[string]string{
		"requirements.txt": "SQLAlchemy==2.0.0\nPy_Test==8.0.0\npytest==8.0.0\n",
	})
	if !has(report, "SQLAlchemy") {
		t.Errorf("a differently-cased distribution was missed: %v", report.Names())
	}
}

func TestTheBuildBackendIsDeclaredNotGuessed(t *testing.T) {
	report := detect(t, map[string]string{
		"pyproject.toml": "[build-system]\nrequires = [\"hatchling\"]\nbuild-backend = \"hatchling.build\"\n",
	})
	if !has(report, "Hatch") {
		t.Errorf("findings %v", report.Names())
	}
}

// ── Go ───────────────────────────────────────────────────────────────────────────────────────────

func TestGoModuleMajorVersionSuffixesResolve(t *testing.T) {
	// `github.com/labstack/echo/v4` is Echo. An exact comparison misses every module past v1, which is
	// most of them.
	report := detect(t, map[string]string{
		"go.mod": `module example.com/demo

go 1.24

require (
	github.com/labstack/echo/v4 v4.12.0
	github.com/stretchr/testify v1.9.0
)
`,
	})
	echo := finding(t, report, "Echo")
	if echo.Version != "v4.12.0" {
		t.Errorf("version %q", echo.Version)
	}
	if !has(report, "testify") {
		t.Errorf("findings %v", report.Names())
	}
}

func TestAGoModuleThatMerelySharesAPrefixIsNotAMatch(t *testing.T) {
	report := detect(t, map[string]string{
		"go.mod": "module x\n\nrequire github.com/gin-gonic/gin-contrib-stub v1.0.0\n",
	})
	if has(report, "Gin") {
		t.Error("a module sharing a prefix was matched")
	}
}

// ── word boundaries in the text formats ──────────────────────────────────────────────────────────

func TestPomArtifactIdsMatchOnWordBoundaries(t *testing.T) {
	report := detect(t, map[string]string{
		"pom.xml": "<dependency><artifactId>spring-boot-starter-web</artifactId></dependency>",
	})
	if !has(report, "Spring Boot") {
		t.Errorf("findings %v", report.Names())
	}
	if !has(report, "Maven") {
		t.Error("a pom.xml did not establish Maven")
	}
}

func TestASurroundedWordIsNotAMatch(t *testing.T) {
	// `not-junit-related` must not match `junit`.
	report := detect(t, map[string]string{
		"pom.xml": "<artifactId>not-junit-related</artifactId>",
	})
	if has(report, "JUnit") {
		t.Error("junit matched inside a longer identifier")
	}
}

func TestContainsWordBoundaries(t *testing.T) {
	cases := map[string]bool{
		"junit":              true,
		"<name>junit</name>": true,
		"junit-jupiter":      false,
		"not-junit":          false,
		"myjunit":            false,
		"junit.version":      false,
		"a junit b":          true,
		"":                   false,
	}
	for haystack, want := range cases {
		if got := containsWord(haystack, "junit"); got != want {
			t.Errorf("containsWord(%q, junit) = %v, want %v", haystack, got, want)
		}
	}
}

// ── confidence ───────────────────────────────────────────────────────────────────────────────────

func TestALayoutSignalIsInferredNotDeclared(t *testing.T) {
	// A generator acting on this would write a Dockerfile for Django in a repository that merely has a
	// file with that name.
	report := Detect(fakeTree{files: map[string]string{"manage.py": "#!/usr/bin/env python\n"}}, nil)
	django := finding(t, report, "Django")
	if django.Confidence != Inferred {
		t.Errorf("confidence %q, want inferred", django.Confidence)
	}
	if len(report.Certain()) != 0 {
		t.Errorf("Certain() returned an inferred finding: %v", report.Certain())
	}
}

func TestADeclarationUpgradesAnInferredFinding(t *testing.T) {
	report := detect(t, map[string]string{
		"manage.py":        "#!/usr/bin/env python\n",
		"requirements.txt": "django==5.0.0\n",
	})
	django := finding(t, report, "Django")
	if django.Confidence != Declared {
		t.Errorf("confidence %q, want declared once a manifest names it", django.Confidence)
	}
	// Reported ONCE. Two findings for one framework would make a caller count it twice.
	count := 0
	for _, f := range report.Findings {
		if f.Name == "Django" {
			count++
		}
	}
	if count != 1 {
		t.Errorf("Django appears %d times", count)
	}
}

func TestAFindingWithNoEvidenceIsNotReported(t *testing.T) {
	acc := &accumulator{seen: map[string]Finding{}}
	acc.add(Finding{Name: "Ghost", Kind: KindWeb, Confidence: Declared})
	acc.add(Finding{Name: "", Kind: KindWeb, Confidence: Declared, Evidence: "x"})
	if len(acc.sorted()) != 0 {
		t.Errorf("a finding with no evidence or no name was kept: %v", acc.sorted())
	}
}

// ── package managers ─────────────────────────────────────────────────────────────────────────────

func TestThePackageManagerComesFromTheLockFile(t *testing.T) {
	// A `package.json` alone is compatible with four managers.
	report := Detect(fakeTree{files: map[string]string{
		"package.json":   `{"dependencies": {}}`,
		"pnpm-lock.yaml": "lockfileVersion: 9.0\n",
	}}, []string{"package.json"})
	if len(report.PackageManagers) != 1 || report.PackageManagers[0] != "pnpm" {
		t.Errorf("package managers %v", report.PackageManagers)
	}
}

func TestTwoLockFilesForOneManagerAreReportedOnce(t *testing.T) {
	report := Detect(fakeTree{files: map[string]string{
		"package-lock.json":   "{}",
		"npm-shrinkwrap.json": "{}",
	}}, nil)
	if len(report.PackageManagers) != 1 {
		t.Errorf("package managers %v", report.PackageManagers)
	}
}

func TestNoLockFileMeansNoPackageManagerClaim(t *testing.T) {
	report := detect(t, map[string]string{"package.json": `{"dependencies": {}}`})
	if len(report.PackageManagers) != 0 {
		t.Errorf("a manager was claimed with no lock file: %v", report.PackageManagers)
	}
}

// ── ordering and shape ───────────────────────────────────────────────────────────────────────────

func TestFindingsAreSortedSoTwoScansAgree(t *testing.T) {
	files := map[string]string{
		"package.json": `{"dependencies": {"express": "1", "react": "1", "vitest": "1", "vite": "1"}}`,
	}
	first := detect(t, files).Findings
	for range 8 {
		// Map iteration order is randomised in Go, so an unsorted result would differ between runs and
		// make the inventory hash unstable.
		next := detect(t, files).Findings
		if len(next) != len(first) {
			t.Fatalf("length changed between runs: %d then %d", len(first), len(next))
		}
		for i := range first {
			if next[i].Name != first[i].Name {
				t.Fatalf("order changed: %v then %v", first, next)
			}
		}
	}
}

func TestAGithubWorkflowDirectoryAloneIsNotEvidence(t *testing.T) {
	// Left behind by tooling that never added a workflow.
	report := Detect(fakeTree{files: map[string]string{".github/workflows/": ""}}, nil)
	if has(report, "GitHub Actions") {
		t.Error("an empty workflow directory was reported as GitHub Actions")
	}
	withWorkflow := Detect(fakeTree{files: map[string]string{".github/workflows/ci.yml": "name: ci\n"}}, nil)
	if !has(withWorkflow, "GitHub Actions") {
		t.Errorf("a real workflow was missed: %v", withWorkflow.Names())
	}
}

func TestAnEmptyTreeProducesNoFindings(t *testing.T) {
	report := Detect(fakeTree{files: map[string]string{}}, nil)
	if len(report.Findings) != 0 || len(report.PackageManagers) != 0 {
		t.Errorf("an empty tree produced %v / %v", report.Findings, report.PackageManagers)
	}
	if len(report.Names()) != 0 {
		t.Error("Names() on an empty report is not empty")
	}
}

func TestComposerAndGemfileAndCargoAreRead(t *testing.T) {
	report := detect(t, map[string]string{
		"composer.json": `{"require": {"laravel/framework": "^11.0"}}`,
		"Gemfile":       "gem 'rails', '~> 7.1'\n",
		"Cargo.toml":    "[dependencies]\naxum = \"0.7\"\n",
	})
	for _, want := range []string{"Laravel", "Rails", "Axum"} {
		if !has(report, want) {
			t.Errorf("%q missed: %v", want, report.Names())
		}
	}
}

func TestDockerAndComposeAreRuntimeFindings(t *testing.T) {
	report := detect(t, map[string]string{
		"Dockerfile":         "FROM alpine:3.20\n",
		"docker-compose.yml": "services: {}\n",
	})
	if finding(t, report, "Docker").Kind != KindRuntime {
		t.Error("Docker is not classified as runtime")
	}
	if !has(report, "Docker Compose") {
		t.Errorf("findings %v", report.Names())
	}
}
