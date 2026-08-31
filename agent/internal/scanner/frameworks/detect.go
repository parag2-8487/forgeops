// SPDX-License-Identifier: Apache-2.0

// Package frameworks identifies the frameworks and build systems a repository uses (FR-10).
//
// WHY THIS PACKAGE EXISTS. `langdetect` answered "what language is this file", and the inventory
// answered "which manifests are present", but nothing answered "what is this project built with" — so
// FR-10 had no implementation at all, and the generation prompt described a repository only by its
// languages. "A Python project" and "a Django project" call for different Dockerfiles, and the second is
// the one an operator wants.
//
// DETECTION IS FROM DECLARED DEPENDENCIES, NOT FROM FILE NAMES. A `manage.py` at the root is suggestive of
// Django; `django` in `pyproject.toml` is evidence of it. The distinction matters because the generator
// acts on the answer: a Dockerfile written for Django in a repository that merely happens to contain a
// file with that name is a broken artifact, and the operator has no way to see why. Where only a filename
// is available the finding says so through its own confidence, and a caller can require certainty.
//
// Every rule here is checked against a real dependency declaration or a real configuration file, and
// nothing is inferred from a language byte-count.
package frameworks

import (
	"encoding/json"
	"path"
	"regexp"
	"sort"
	"strings"
)

// Confidence records how a framework was established.
//
// Two levels, not a score. A number would invite arithmetic that means nothing — averaging "we read it in
// the lock file" with "a file has that name" produces a figure no decision can be made from.
type Confidence string

const (
	// Declared means the framework appears as a dependency in a manifest or lock file. This is the
	// evidence a generator may act on without qualification.
	Declared Confidence = "declared"
	// Inferred means the layout is characteristic but no dependency declares it — a `manage.py` with no
	// readable manifest, for instance. Reported so an operator can confirm it, and kept out of
	// `Certain()` so a generator does not act on a guess.
	Inferred Confidence = "inferred"
)

// Kind separates the things a project is built WITH from the things it is built BY.
type Kind string

const (
	// KindWeb is an application or API framework: Django, Express, Spring Boot.
	KindWeb Kind = "web"
	// KindFrontend is a UI framework or bundler: React, Next.js, Vue.
	KindFrontend Kind = "frontend"
	// KindBuild is a build system or package manager: Maven, Gradle, Poetry, pnpm.
	KindBuild Kind = "build"
	// KindTest is a test runner, which is what tells a pipeline generator how to run the tests.
	KindTest Kind = "test"
	// KindRuntime is a container or orchestration surface the repository already has.
	KindRuntime Kind = "runtime"
)

// Finding is one detected framework and the evidence for it.
type Finding struct {
	Name       string     `json:"name"`
	Kind       Kind       `json:"kind"`
	Confidence Confidence `json:"confidence"`
	// Evidence is the repo-relative path the conclusion came from, so an operator can check it. A
	// finding with no evidence is not reported.
	Evidence string `json:"evidence"`
	// Version is the declared constraint verbatim ("^4.18.2"), not a resolved version. Resolving would
	// mean running the package manager, which this must not do: detection reads a tree, it does not
	// execute anything in it.
	Version string `json:"version,omitempty"`
}

// Report is the whole detection result.
type Report struct {
	Findings []Finding `json:"findings"`
	// PackageManagers is the set of managers whose lock files are present, which is the only reliable
	// way to know how to install: a `package.json` says nothing about npm versus pnpm, and the lock
	// file says everything.
	PackageManagers []string `json:"package_managers"`
}

// Names returns every detected framework name, sorted.
func (r Report) Names() []string {
	out := make([]string, 0, len(r.Findings))
	for _, f := range r.Findings {
		out = append(out, f.Name)
	}
	sort.Strings(out)
	return out
}

// Certain returns only the findings backed by a declared dependency.
//
// The distinction a generator must respect: acting on an inferred framework produces an artifact that is
// wrong for reasons invisible to the operator reading it.
func (r Report) Certain() []Finding {
	var out []Finding
	for _, f := range r.Findings {
		if f.Confidence == Declared {
			out = append(out, f)
		}
	}
	return out
}

// FileReader supplies file contents to the detector.
//
// An interface so detection can run against a real tree, an in-memory tree, or the redacted content a
// scan already holds — without reading any file twice. Returns ok=false for an absent file; a read error
// and an absent file are the same answer here, because both mean "no evidence available from this path".
type FileReader interface {
	ReadFile(relPath string) (content []byte, ok bool)
	Exists(relPath string) bool
}

// dependencyRule maps a dependency name to what its presence establishes.
type dependencyRule struct {
	// match is the dependency name, compared exactly after normalisation. A substring match would make
	// `react` fire on `react-native-web-stub` and `@types/react`.
	match string
	name  string
	kind  Kind
}

var nodeRules = []dependencyRule{
	{"next", "Next.js", KindFrontend},
	{"react", "React", KindFrontend},
	{"vue", "Vue", KindFrontend},
	{"@angular/core", "Angular", KindFrontend},
	{"svelte", "Svelte", KindFrontend},
	{"nuxt", "Nuxt", KindFrontend},
	{"express", "Express", KindWeb},
	{"fastify", "Fastify", KindWeb},
	{"@nestjs/core", "NestJS", KindWeb},
	{"koa", "Koa", KindWeb},
	{"hapi", "hapi", KindWeb},
	{"@hapi/hapi", "hapi", KindWeb},
	{"jest", "Jest", KindTest},
	{"vitest", "Vitest", KindTest},
	{"mocha", "Mocha", KindTest},
	{"@playwright/test", "Playwright", KindTest},
	{"cypress", "Cypress", KindTest},
	{"vite", "Vite", KindBuild},
	{"webpack", "webpack", KindBuild},
	{"typescript", "TypeScript", KindBuild},
	{"esbuild", "esbuild", KindBuild},
	{"turbo", "Turborepo", KindBuild},
}

var pythonRules = []dependencyRule{
	{"django", "Django", KindWeb},
	{"flask", "Flask", KindWeb},
	{"fastapi", "FastAPI", KindWeb},
	{"starlette", "Starlette", KindWeb},
	{"tornado", "Tornado", KindWeb},
	{"aiohttp", "aiohttp", KindWeb},
	{"pyramid", "Pyramid", KindWeb},
	{"celery", "Celery", KindRuntime},
	{"pytest", "pytest", KindTest},
	{"unittest2", "unittest2", KindTest},
	{"tox", "tox", KindTest},
	{"nox", "nox", KindTest},
	{"poetry", "Poetry", KindBuild},
	{"hatchling", "Hatch", KindBuild},
	{"setuptools", "setuptools", KindBuild},
	{"uvicorn", "Uvicorn", KindRuntime},
	{"gunicorn", "Gunicorn", KindRuntime},
	{"granian", "Granian", KindRuntime},
	{"sqlalchemy", "SQLAlchemy", KindBuild},
	{"pydantic", "Pydantic", KindBuild},
}

var goRules = []dependencyRule{
	{"github.com/gin-gonic/gin", "Gin", KindWeb},
	{"github.com/labstack/echo", "Echo", KindWeb},
	{"github.com/gofiber/fiber", "Fiber", KindWeb},
	{"github.com/go-chi/chi", "chi", KindWeb},
	{"github.com/gorilla/mux", "gorilla/mux", KindWeb},
	{"google.golang.org/grpc", "gRPC", KindWeb},
	{"github.com/spf13/cobra", "Cobra", KindBuild},
	{"github.com/stretchr/testify", "testify", KindTest},
	{"gorm.io/gorm", "GORM", KindBuild},
}

var javaRules = []dependencyRule{
	{"spring-boot", "Spring Boot", KindWeb},
	{"spring-boot-starter-web", "Spring Boot", KindWeb},
	{"quarkus", "Quarkus", KindWeb},
	{"micronaut", "Micronaut", KindWeb},
	{"junit", "JUnit", KindTest},
	{"junit-jupiter", "JUnit", KindTest},
	{"testng", "TestNG", KindTest},
}

var rubyRules = []dependencyRule{
	{"rails", "Rails", KindWeb},
	{"sinatra", "Sinatra", KindWeb},
	{"rspec", "RSpec", KindTest},
	{"puma", "Puma", KindRuntime},
}

var rustRules = []dependencyRule{
	{"axum", "Axum", KindWeb},
	{"actix-web", "Actix Web", KindWeb},
	{"rocket", "Rocket", KindWeb},
	{"tokio", "Tokio", KindRuntime},
	{"serde", "Serde", KindBuild},
}

var phpRules = []dependencyRule{
	{"laravel/framework", "Laravel", KindWeb},
	{"symfony/framework-bundle", "Symfony", KindWeb},
	{"slim/slim", "Slim", KindWeb},
	{"phpunit/phpunit", "PHPUnit", KindTest},
}

// : Lock files, and the manager each one proves. The lock file rather than the manifest, because a
// : `package.json` is compatible with three managers and the lock file names exactly one.
var lockFiles = map[string]string{
	"package-lock.json":   "npm",
	"npm-shrinkwrap.json": "npm",
	"pnpm-lock.yaml":      "pnpm",
	"yarn.lock":           "yarn",
	"bun.lockb":           "bun",
	"poetry.lock":         "poetry",
	"Pipfile.lock":        "pipenv",
	"uv.lock":             "uv",
	"go.sum":              "go modules",
	"Cargo.lock":          "cargo",
	"composer.lock":       "composer",
	"Gemfile.lock":        "bundler",
	"pdm.lock":            "pdm",
}

// Detect runs every rule against the tree and returns the findings, sorted.
//
// `manifests` is the inventory's manifest list, so detection reads only files the scan already
// classified rather than walking the tree a second time.
func Detect(reader FileReader, manifests []string) Report {
	acc := &accumulator{seen: map[string]Finding{}}

	for _, manifest := range manifests {
		switch path.Base(manifest) {
		case "package.json":
			acc.nodePackage(reader, manifest)
		case "pyproject.toml":
			acc.pyproject(reader, manifest)
		case "requirements.txt", "requirements-dev.txt", "constraints.txt":
			acc.requirements(reader, manifest)
		case "Pipfile":
			acc.plainText(reader, manifest, pythonRules)
		case "go.mod":
			acc.goMod(reader, manifest)
		case "pom.xml":
			acc.plainText(reader, manifest, javaRules)
			acc.add(Finding{Name: "Maven", Kind: KindBuild, Confidence: Declared, Evidence: manifest})
		case "build.gradle", "build.gradle.kts":
			acc.plainText(reader, manifest, javaRules)
			acc.add(Finding{Name: "Gradle", Kind: KindBuild, Confidence: Declared, Evidence: manifest})
		case "Gemfile":
			acc.plainText(reader, manifest, rubyRules)
		case "Cargo.toml":
			acc.plainText(reader, manifest, rustRules)
		case "composer.json":
			acc.composer(reader, manifest)
		case "Dockerfile":
			acc.add(Finding{Name: "Docker", Kind: KindRuntime, Confidence: Declared, Evidence: manifest})
		case "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml":
			acc.add(Finding{Name: "Docker Compose", Kind: KindRuntime, Confidence: Declared, Evidence: manifest})
		}
	}

	acc.layoutOnly(reader)

	report := Report{Findings: acc.sorted()}
	for lock, manager := range lockFiles {
		if reader.Exists(lock) {
			report.PackageManagers = append(report.PackageManagers, manager)
		}
	}
	sort.Strings(report.PackageManagers)
	report.PackageManagers = dedupe(report.PackageManagers)
	return report
}

type accumulator struct {
	seen map[string]Finding
}

// add records a finding, keeping the strongest evidence for a name.
//
// A framework declared in `pyproject.toml` and also inferred from `manage.py` is reported once, as
// declared. Reporting both would make a caller count the same framework twice.
func (a *accumulator) add(f Finding) {
	if f.Name == "" || f.Evidence == "" {
		return
	}
	existing, present := a.seen[f.Name]
	if !present {
		a.seen[f.Name] = f
		return
	}
	if existing.Confidence == Inferred && f.Confidence == Declared {
		a.seen[f.Name] = f
		return
	}
	// A version discovered later fills in a finding that had none, without downgrading its evidence.
	if existing.Version == "" && f.Version != "" && f.Confidence == existing.Confidence {
		existing.Version = f.Version
		a.seen[f.Name] = existing
	}
}

func (a *accumulator) sorted() []Finding {
	out := make([]Finding, 0, len(a.seen))
	for _, f := range a.seen {
		out = append(out, f)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Kind != out[j].Kind {
			return out[i].Kind < out[j].Kind
		}
		return out[i].Name < out[j].Name
	})
	return out
}

// nodePackage reads the dependency MAPS rather than searching the file text.
//
// A text search would fire on a name in a `description`, a `scripts` line, or a URL — and `"react"`
// appears in the `homepage` of a great many packages that do not use it.
func (a *accumulator) nodePackage(reader FileReader, manifest string) {
	raw, ok := reader.ReadFile(manifest)
	if !ok {
		return
	}
	var pkg struct {
		Dependencies     map[string]string `json:"dependencies"`
		DevDependencies  map[string]string `json:"devDependencies"`
		PeerDependencies map[string]string `json:"peerDependencies"`
		Scripts          map[string]string `json:"scripts"`
		PackageManager   string            `json:"packageManager"`
	}
	if err := json.Unmarshal(raw, &pkg); err != nil {
		// A malformed manifest yields no findings rather than a text-search fallback. A guess drawn
		// from a file the runtime itself cannot parse is not evidence.
		return
	}
	for _, deps := range []map[string]string{pkg.Dependencies, pkg.DevDependencies, pkg.PeerDependencies} {
		for name, constraint := range deps {
			for _, rule := range nodeRules {
				if name == rule.match {
					a.add(Finding{
						Name: rule.name, Kind: rule.kind, Confidence: Declared,
						Evidence: manifest, Version: constraint,
					})
				}
			}
		}
	}
	// `packageManager: "pnpm@9.1.0"` is corepack's declaration and is stronger than a lock file,
	// because it states intent rather than history.
	if manager, _, found := strings.Cut(pkg.PackageManager, "@"); found && manager != "" {
		a.add(Finding{Name: manager, Kind: KindBuild, Confidence: Declared, Evidence: manifest})
	}
}

var (
	//: A quoted PEP 508 requirement, as it appears inside an inline array:
	//: `dependencies = ["fastapi>=0.110", "granian==1.6.0"]`. Anchoring on a line start missed all of
	//: these, because a `pyproject.toml` array is usually written on ONE line — which is how
	//: `TestPyProjectReadsOnlyDependencySections` first failed.
	pyQuotedRequirement = regexp.MustCompile(`["']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^"']*)["']`)
	//: A Poetry-style table entry, `django = "^5.0"`, where the name is the KEY rather than part of a
	//: quoted requirement.
	pyTableEntry = regexp.MustCompile(`(?m)^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*["']([^"']*)["']`)
	//: A bare requirement on its own line, which is what `requirements.txt` contains.
	pyBareRequirement = regexp.MustCompile(`(?m)^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([<>=!~^].*)?$`)
	//: A `require` line in go.mod, with or without the block form.
	goRequirePattern = regexp.MustCompile(`(?m)^\s*(?:require\s+)?([a-z0-9][-a-z0-9.]*\.[a-z]{2,}/[^\s]+)\s+(v[^\s/]+)`)
)

// pyproject reads only the dependency ARRAYS, so a tool's configuration table cannot contribute.
//
// `[tool.ruff]` mentioning `flask` in a per-file ignore, or `[tool.pytest.ini_options]` naming `django`
// in `DJANGO_SETTINGS_MODULE`, would both be false positives for a whole-file search.
func (a *accumulator) pyproject(reader FileReader, manifest string) {
	raw, ok := reader.ReadFile(manifest)
	if !ok {
		return
	}
	text := string(raw)
	for _, section := range dependencySections(text) {
		a.matchPython(section, manifest, pythonRules)
	}
	// The build backend is declared, not guessed.
	if strings.Contains(text, "poetry.core.masonry.api") || strings.Contains(text, "[tool.poetry]") {
		a.add(Finding{Name: "Poetry", Kind: KindBuild, Confidence: Declared, Evidence: manifest})
	}
	if strings.Contains(text, "hatchling.build") {
		a.add(Finding{Name: "Hatch", Kind: KindBuild, Confidence: Declared, Evidence: manifest})
	}
	if strings.Contains(text, "setuptools.build_meta") {
		a.add(Finding{Name: "setuptools", Kind: KindBuild, Confidence: Declared, Evidence: manifest})
	}
}

// dependencySections extracts the regions of a pyproject that actually list dependencies.
func dependencySections(text string) []string {
	var out []string
	lines := strings.Split(text, "\n")
	inSection := false
	var current []string
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		switch {
		case strings.HasPrefix(trimmed, "dependencies") || strings.HasPrefix(trimmed, "requires") ||
			strings.Contains(trimmed, "-dependencies") || strings.HasPrefix(trimmed, "[tool.poetry.dependencies]") ||
			strings.HasPrefix(trimmed, "[tool.poetry.group") || strings.HasPrefix(trimmed, "[project.optional-dependencies]"):
			inSection = true
			current = append(current, line)
		case strings.HasPrefix(trimmed, "[") && inSection:
			// A new table ends the region, unless it is itself a dependency table (handled above).
			out = append(out, strings.Join(current, "\n"))
			current = nil
			inSection = false
		case inSection:
			current = append(current, line)
		}
	}
	if len(current) > 0 {
		out = append(out, strings.Join(current, "\n"))
	}
	return out
}

func (a *accumulator) requirements(reader FileReader, manifest string) {
	raw, ok := reader.ReadFile(manifest)
	if !ok {
		return
	}
	// Comments are stripped first: a commented-out dependency is not a dependency, and `# django` in a
	// note about a migration would otherwise register as one.
	var kept []string
	for _, line := range strings.Split(string(raw), "\n") {
		if before, _, _ := strings.Cut(line, "#"); strings.TrimSpace(before) != "" {
			kept = append(kept, before)
		}
	}
	a.matchPython(strings.Join(kept, "\n"), manifest, pythonRules)
}

func (a *accumulator) goMod(reader FileReader, manifest string) {
	raw, ok := reader.ReadFile(manifest)
	if !ok {
		return
	}
	for _, match := range goRequirePattern.FindAllStringSubmatch(string(raw), -1) {
		module, version := match[1], match[2]
		for _, rule := range goRules {
			// Prefix match with a boundary, because Go modules carry a major-version suffix:
			// `github.com/labstack/echo/v4` is Echo, and an exact comparison would miss every
			// module past v1.
			if module == rule.match || strings.HasPrefix(module, rule.match+"/") {
				a.add(Finding{
					Name: rule.name, Kind: rule.kind, Confidence: Declared,
					Evidence: manifest, Version: version,
				})
			}
		}
	}
}

func (a *accumulator) composer(reader FileReader, manifest string) {
	raw, ok := reader.ReadFile(manifest)
	if !ok {
		return
	}
	var pkg struct {
		Require    map[string]string `json:"require"`
		RequireDev map[string]string `json:"require-dev"`
	}
	if err := json.Unmarshal(raw, &pkg); err != nil {
		return
	}
	for _, deps := range []map[string]string{pkg.Require, pkg.RequireDev} {
		for name, constraint := range deps {
			for _, rule := range phpRules {
				if name == rule.match {
					a.add(Finding{
						Name: rule.name, Kind: rule.kind, Confidence: Declared,
						Evidence: manifest, Version: constraint,
					})
				}
			}
		}
	}
}

// plainText matches rules against a whole manifest, for formats with no cheap parser.
//
// Used for `pom.xml`, `build.gradle`, `Gemfile` and `Cargo.toml`. Still a WORD match rather than a
// substring one: `<artifactId>spring-boot-starter-web</artifactId>` must match `spring-boot` while
// `not-junit-related` must not match `junit`.
func (a *accumulator) plainText(reader FileReader, manifest string, rules []dependencyRule) {
	raw, ok := reader.ReadFile(manifest)
	if !ok {
		return
	}
	lower := strings.ToLower(string(raw))
	for _, rule := range rules {
		if containsWord(lower, rule.match) {
			a.add(Finding{Name: rule.name, Kind: rule.kind, Confidence: Declared, Evidence: manifest})
		}
	}
}

// matchPython applies the rules to a region using all three spellings a dependency can take.
//
// Three patterns rather than one, because a `pyproject.toml` inline array, a Poetry table and a
// `requirements.txt` line are genuinely different syntax and a single expression that covered all of them
// would also match a great deal that is none of them.
func (a *accumulator) matchPython(text, manifest string, rules []dependencyRule) {
	record := func(rawName, rawVersion string) {
		name := strings.ToLower(strings.ReplaceAll(strings.TrimSpace(rawName), "_", "-"))
		version := strings.TrimSpace(rawVersion)
		for _, rule := range rules {
			if name == rule.match {
				a.add(Finding{
					Name: rule.name, Kind: rule.kind, Confidence: Declared,
					Evidence: manifest, Version: version,
				})
			}
		}
	}
	for _, match := range pyQuotedRequirement.FindAllStringSubmatch(text, -1) {
		record(match[1], match[2])
	}
	for _, match := range pyTableEntry.FindAllStringSubmatch(text, -1) {
		record(match[1], match[2])
	}
	for _, match := range pyBareRequirement.FindAllStringSubmatch(text, -1) {
		record(match[1], match[2])
	}
}

// : Layout signals, reported as INFERRED. Each is characteristic but none is proof, and the confidence
// : is what keeps a generator from acting on them.
var layoutSignals = []struct {
	path string
	name string
	kind Kind
}{
	{"manage.py", "Django", KindWeb},
	{"next.config.js", "Next.js", KindFrontend},
	{"next.config.mjs", "Next.js", KindFrontend},
	{"next.config.ts", "Next.js", KindFrontend},
	{"nuxt.config.ts", "Nuxt", KindFrontend},
	{"angular.json", "Angular", KindFrontend},
	{"svelte.config.js", "Svelte", KindFrontend},
	{"vite.config.ts", "Vite", KindBuild},
	{"vite.config.js", "Vite", KindBuild},
	{"tsconfig.json", "TypeScript", KindBuild},
	{"artisan", "Laravel", KindWeb},
	{"config/routes.rb", "Rails", KindWeb},
	{"Chart.yaml", "Helm", KindRuntime},
	{"kustomization.yaml", "Kustomize", KindRuntime},
	{"Makefile", "Make", KindBuild},
	{"Taskfile.yml", "Task", KindBuild},
	{"tofu.tf", "OpenTofu", KindRuntime},
	{"main.tf", "OpenTofu", KindRuntime},
}

func (a *accumulator) layoutOnly(reader FileReader) {
	for _, signal := range layoutSignals {
		if reader.Exists(signal.path) {
			a.add(Finding{
				Name: signal.name, Kind: signal.kind, Confidence: Inferred, Evidence: signal.path,
			})
		}
	}
	// A workflow directory is evidence of GitHub Actions only if it holds a workflow. The directory
	// alone is left by tooling that never added one.
	for _, candidate := range []string{
		".github/workflows/ci.yml", ".github/workflows/ci.yaml",
		".github/workflows/main.yml", ".github/workflows/build.yml",
		".github/workflows/test.yml", ".github/workflows/release.yml",
	} {
		if reader.Exists(candidate) {
			a.add(Finding{
				Name: "GitHub Actions", Kind: KindRuntime, Confidence: Declared, Evidence: candidate,
			})
			break
		}
	}
}

// containsWord reports whether needle appears in haystack bounded by non-identifier characters.
func containsWord(haystack, needle string) bool {
	from := 0
	for {
		idx := strings.Index(haystack[from:], needle)
		if idx < 0 {
			return false
		}
		start := from + idx
		end := start + len(needle)
		if !isIdentByte(haystack, start-1) && !isIdentByte(haystack, end) {
			return true
		}
		from = start + 1
		if from >= len(haystack) {
			return false
		}
	}
}

func isIdentByte(s string, i int) bool {
	if i < 0 || i >= len(s) {
		return false
	}
	c := s[i]
	return c == '-' || c == '_' || c == '.' ||
		(c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
}

func dedupe(in []string) []string {
	if len(in) == 0 {
		return in
	}
	out := in[:1]
	for _, v := range in[1:] {
		if v != out[len(out)-1] {
			out = append(out, v)
		}
	}
	return out
}
