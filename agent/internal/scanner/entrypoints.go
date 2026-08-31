// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"encoding/json"
	"path"
	"regexp"
	"sort"
	"strings"
)

// Entry point and configuration-file classification (FR-11).
//
// WHAT THIS REPLACES. The inventory decided entry points with one line:
//
//	if name == "main.go" || name == "index.ts" || name == "main.py" || name == "app.py" || name == "server.js"
//
// Five filenames, matched anywhere in the tree. That list is wrong in both directions, and both errors
// reach the generated Dockerfile:
//
//   - It MISSES the real entry point of most Go repositories. This one's is `agent/cmd/forgeops-agent/main.go`
//     — caught only because the file happens to be called `main.go`. A `cmd/server/serve.go` declaring
//     `package main` and `func main()` is an entry point and was invisible. So was every Python package
//     using `__main__.py`, every project whose start command is declared in `package.json`, and every
//     console script in a `pyproject.toml`.
//   - It INVENTS entry points that are not one. A test fixture named `app.py`, a vendored `server.js`, a
//     `main.py` inside an examples directory — each was reported as a way to start the application.
//
// The classifier below reads DECLARATIONS where a manifest makes one (`package.json` `main`, `bin` and
// `scripts.start`; `[project.scripts]` and `[project.gui-scripts]`; `[[bin]]`) and CODE where the language
// defines an entry point structurally (`package main` with `func main()`, `if __name__ == "__main__"`,
// `public static void main`, `fn main()`). A filename alone yields an entry point only for names that are
// unambiguous by convention, and never inside a vendor or fixture directory.

// : Directories whose contents are never an application entry point, however they are named. A `main.go`
// : under `testdata/` is a fixture, and `node_modules/**/server.js` belongs to a dependency.
// :
// : `example`/`examples` is deliberately included: an example's `main.go` is a real program, but it is not
// : THE program, and a Dockerfile built for it would ship the sample instead of the service.
var foreignDirs = map[string]bool{
	"node_modules": true, "vendor": true, "third_party": true, "site-packages": true,
	".venv": true, "venv": true, ".git": true, "__pycache__": true,
}

// : Directories whose contents are not an entry point BY INFERENCE, but may be named by a declaration.
// :
// : The distinction is load-bearing, and it was found by a failing test rather than reasoned out in
// : advance. `dist/` and `build/` hold generated output, so a `main.go` found there is a build artefact and
// : inferring an entry point from it is wrong — but `"main": "./dist/server.js"` is the single most common
// : way a Node project declares its entry point, and refusing it discards the most reliable evidence
// : available. One set could not express both rules.
// :
// : `example`/`examples` is here for a related reason: an example's `main.go` is a real program, but it is
// : not THE program, and a Dockerfile built for it would ship the sample instead of the service.
var inferenceOnlyExclusions = map[string]bool{
	"testdata": true, "test": true, "tests": true, "fixtures": true, "fixture": true,
	"example": true, "examples": true, "docs": true, "dist": true, "build": true,
	"target": true, "migrations": true,
}

// : Filenames that are an entry point by convention strongly enough to accept without a declaration.
// : `app.py` and `server.js` are NOT here: both are ordinary module names, and the previous list's
// : willingness to accept them is what made a fixture look like a service.
var conventionalEntryPoints = map[string]bool{
	"main.go": true, "__main__.py": true, "manage.py": true, "wsgi.py": true, "asgi.py": true,
	"main.rs": true, "artisan": true,
}

var (
	//: `package main` at the start of a line, then `func main()` somewhere after it. Both are required:
	//: a `package main` with no `main` function is a build-tagged fragment or a generated stub, and a
	//: `func main()` in a library package does not build into a program.
	goPackageMain = regexp.MustCompile(`(?m)^\s*package\s+main\s*$`)
	goFuncMain    = regexp.MustCompile(`(?m)^\s*func\s+main\s*\(\s*\)`)
	//: The Python entry-point guard, in either quoting style.
	pyDunderMain = regexp.MustCompile(`(?m)^\s*if\s+__name__\s*==\s*['"]__main__['"]\s*:`)
	//: A JVM entry point. `String[] args` and `String... args` are both legal.
	javaMain = regexp.MustCompile(`public\s+static\s+void\s+main\s*\(\s*(?:final\s+)?String\s*(?:\[\s*\]|\.\.\.)\s*\w+\s*\)`)
	//: Rust's, excluding the test-only form which is `#[test] fn main` in no real crate but is cheap to
	//: exclude by requiring the function at the top level.
	rustFnMain = regexp.MustCompile(`(?m)^\s*(?:pub\s+)?fn\s+main\s*\(\s*\)`)
	//: A shebang makes a script executable, which is the closest thing to a declaration a shell file has.
	shebang = regexp.MustCompile(`^#!\s*\S+`)
)

// entryPointClassifier accumulates entry points across a walk.
//
// Stateful because two of the rules need the whole tree: a declared entry point in `package.json` names a
// path that may be walked later, and a `package main` file must not be reported when it sits under a
// directory the walk has not reached yet. Declarations are therefore collected first and resolved at the
// end.
type entryPointClassifier struct {
	//: Paths established from the file's own contents, with the reason.
	fromCode map[string]string
	//: Paths named by a manifest, to be confirmed against the real tree.
	declared map[string]string
	//: Every path seen, so a declaration naming a file that does not exist is dropped rather than
	//: reported. A `scripts.start` pointing at a deleted file is a stale manifest, not an entry point.
	present map[string]bool
}

func newEntryPointClassifier() *entryPointClassifier {
	return &entryPointClassifier{
		fromCode: map[string]string{},
		declared: map[string]string{},
		present:  map[string]bool{},
	}
}

// excluded reports whether a path sits under a directory that cannot hold an entry point.
func excludedFromEntryPoints(relPath string) bool {
	return underAny(relPath, foreignDirs)
}

// excludedFromInference reports whether a path may not be inferred as an entry point from its own
// contents, while remaining eligible when a manifest explicitly names it.
func excludedFromInference(relPath string) bool {
	return excludedFromEntryPoints(relPath) || underAny(relPath, inferenceOnlyExclusions)
}

func underAny(relPath string, dirs map[string]bool) bool {
	parts := strings.Split(relPath, "/")
	for _, segment := range parts[:max(len(parts)-1, 0)] {
		if dirs[strings.ToLower(segment)] {
			return true
		}
	}
	return false
}

// consider examines one file, recording what it establishes.
func (c *entryPointClassifier) consider(relPath string, content []byte) {
	c.present[relPath] = true
	base := path.Base(relPath)

	// A manifest under a FOREIGN directory declares nothing: a `package.json` under `node_modules`
	// describes a dependency's entry point, not this project's.
	if excludedFromEntryPoints(relPath) {
		return
	}

	switch base {
	case "package.json":
		c.readNodeManifest(relPath, content)
	case "pyproject.toml":
		c.readPyProject(relPath, content)
	case "Cargo.toml":
		c.readCargoManifest(relPath, content)
	case "Procfile":
		// A Procfile's whole purpose is to declare how the application starts, so it is itself an
		// entry point description.
		c.fromCode[relPath] = "declares process types"
		return
	}

	// Everything below INFERS an entry point from the file itself, so it uses the stricter predicate. A
	// `main.go` under `dist/` is a build artefact; the same path named by a `package.json` `main` is a
	// declaration and was already recorded above.
	if excludedFromInference(relPath) {
		return
	}

	text := string(content)
	switch {
	case strings.HasSuffix(base, ".go"):
		if strings.HasSuffix(base, "_test.go") {
			return
		}
		if goPackageMain.MatchString(text) && goFuncMain.MatchString(text) {
			// The structural rule, and the one that finds `cmd/server/serve.go`.
			c.fromCode[relPath] = "package main with func main()"
		}
	case strings.HasSuffix(base, ".py"):
		if pyDunderMain.MatchString(text) {
			c.fromCode[relPath] = "guarded __main__ block"
		}
	case strings.HasSuffix(base, ".java"), strings.HasSuffix(base, ".kt"):
		if javaMain.MatchString(text) {
			c.fromCode[relPath] = "public static void main"
		}
	case strings.HasSuffix(base, ".rs"):
		if rustFnMain.MatchString(text) {
			c.fromCode[relPath] = "fn main()"
		}
	case strings.HasSuffix(base, ".sh"), base == "entrypoint.sh", !strings.Contains(base, "."):
		if shebang.MatchString(text) && strings.Contains(base, "entrypoint") {
			c.fromCode[relPath] = "executable entrypoint script"
		}
	}

	if conventionalEntryPoints[base] {
		if _, already := c.fromCode[relPath]; !already {
			c.fromCode[relPath] = "conventional entry point filename"
		}
	}
}

func (c *entryPointClassifier) readNodeManifest(manifestPath string, content []byte) {
	var pkg struct {
		Main    string            `json:"main"`
		Module  string            `json:"module"`
		Bin     json.RawMessage   `json:"bin"`
		Scripts map[string]string `json:"scripts"`
	}
	if err := json.Unmarshal(content, &pkg); err != nil {
		return
	}
	dir := path.Dir(manifestPath)
	record := func(candidate, reason string) {
		candidate = strings.TrimPrefix(strings.TrimSpace(candidate), "./")
		if candidate == "" {
			return
		}
		c.declared[path.Join(dir, candidate)] = reason
	}
	record(pkg.Main, "package.json main")
	record(pkg.Module, "package.json module")

	// `bin` is either a string or an object of name -> path.
	if len(pkg.Bin) > 0 {
		var single string
		if err := json.Unmarshal(pkg.Bin, &single); err == nil {
			record(single, "package.json bin")
		} else {
			var many map[string]string
			if err := json.Unmarshal(pkg.Bin, &many); err == nil {
				for name, target := range many {
					record(target, "package.json bin "+name)
				}
			}
		}
	}

	// A start script names how the application runs. The FILE it names is extracted where the command
	// is a direct invocation; a script that runs a bundler is not itself an entry point.
	for _, key := range []string{"start", "serve", "dev"} {
		command, ok := pkg.Scripts[key]
		if !ok {
			continue
		}
		for _, token := range strings.Fields(command) {
			if strings.HasSuffix(token, ".js") || strings.HasSuffix(token, ".mjs") ||
				strings.HasSuffix(token, ".cjs") || strings.HasSuffix(token, ".ts") {
				record(token, "package.json scripts."+key)
			}
		}
	}
}

var (
	pyScriptSection = regexp.MustCompile(`(?m)^\s*\[project\.(?:gui-)?scripts\]\s*$`)
	pyScriptEntry   = regexp.MustCompile(`(?m)^\s*[\w.-]+\s*=\s*["']([\w.]+)(?::[\w.]+)?["']`)
	cargoBinPath    = regexp.MustCompile(`(?m)^\s*path\s*=\s*["']([^"']+)["']`)
)

// readPyProject turns a console script's MODULE into the file that defines it.
//
// `forgeops = "src.cli:main"` names a module, not a path, so it is converted to both candidate layouts —
// `src/cli.py` and `src/cli/__init__.py` — and whichever exists in the tree is kept. Emitting a module
// name into a field that holds paths would produce an entry point no tool could open.
func (c *entryPointClassifier) readPyProject(manifestPath string, content []byte) {
	text := string(content)
	loc := pyScriptSection.FindStringIndex(text)
	if loc == nil {
		return
	}
	rest := text[loc[1]:]
	if end := strings.Index(rest, "\n["); end >= 0 {
		rest = rest[:end]
	}
	dir := path.Dir(manifestPath)
	for _, match := range pyScriptEntry.FindAllStringSubmatch(rest, -1) {
		module := strings.ReplaceAll(match[1], ".", "/")
		for _, candidate := range []string{module + ".py", module + "/__init__.py", module + "/__main__.py"} {
			c.declared[path.Join(dir, candidate)] = "pyproject console script"
		}
	}
}

func (c *entryPointClassifier) readCargoManifest(manifestPath string, content []byte) {
	text := string(content)
	if !strings.Contains(text, "[[bin]]") {
		// A crate with no explicit `[[bin]]` uses `src/main.rs`, which the structural rule already
		// finds, so nothing needs declaring.
		return
	}
	dir := path.Dir(manifestPath)
	for _, match := range cargoBinPath.FindAllStringSubmatch(text, -1) {
		c.declared[path.Join(dir, strings.TrimPrefix(match[1], "./"))] = "Cargo [[bin]] path"
	}
}

// resolve returns the sorted entry points, dropping declarations that name absent files.
func (c *entryPointClassifier) resolve() []string {
	final := make(map[string]bool, len(c.fromCode))
	for p := range c.fromCode {
		final[p] = true
	}
	for p := range c.declared {
		// A declaration is only honoured when the file it names is really there. A `main` field left
		// behind by a rename would otherwise put a nonexistent path in the inventory.
		if c.present[p] && !excludedFromEntryPoints(p) {
			final[p] = true
		}
	}
	out := make([]string, 0, len(final))
	for p := range final {
		out = append(out, p)
	}
	sort.Strings(out)
	return out
}

// : Configuration files, by exact name or by extension within a recognised directory. The previous rule
// : was `.yaml || .json || startswith(".")`, which classified every `package.json`, every `tsconfig.json`,
// : every Kubernetes manifest and every `.gitignore` as configuration — and `package.json` is a manifest,
// : which the same walk had already said.
var configFileNames = map[string]bool{
	".env.example": true, ".env.template": true, ".env.sample": true,
	".editorconfig": true, ".gitignore": true, ".gitattributes": true, ".dockerignore": true,
	".eslintrc.json": true, ".eslintrc.js": true, ".prettierrc": true, ".prettierrc.json": true,
	"tsconfig.json": true, "jsconfig.json": true, ".babelrc": true, "babel.config.js": true,
	".yamllint": true, ".yamllint.yaml": true, ".flake8": true, ".pylintrc": true,
	"setup.cfg": true, "tox.ini": true, "pytest.ini": true, "mypy.ini": true, ".ruff.toml": true,
	"ruff.toml": true, ".pre-commit-config.yaml": true, "renovate.json": true,
	"nginx.conf": true, "my.cnf": true, "redis.conf": true, "supervisord.conf": true,
	"vite.config.ts": true, "vite.config.js": true, "webpack.config.js": true,
	"next.config.js": true, "next.config.mjs": true, "next.config.ts": true,
	"tailwind.config.js": true, "tailwind.config.ts": true, "postcss.config.js": true,
	"jest.config.js": true, "jest.config.ts": true, "vitest.config.ts": true,
	"playwright.config.ts": true, "cypress.config.ts": true, "alembic.ini": true,
	"logging.conf": true, "log4j2.xml": true, "logback.xml": true, "application.properties": true,
	"application.yml": true, "application.yaml": true,
}

// : Extensions that are configuration when they sit in a directory that exists to hold configuration.
var configExtensions = map[string]bool{
	".yaml": true, ".yml": true, ".toml": true, ".ini": true, ".conf": true, ".cfg": true,
	".properties": true, ".json": true,
}

// : Directory names whose contents are configuration.
var configDirs = map[string]bool{
	"config": true, "configs": true, "conf": true, "etc": true, "settings": true,
	".github": true, ".circleci": true, ".gitlab": true, "deploy": true, "deployment": true,
	"k8s": true, "kubernetes": true, "manifests": true, "helm": true, "charts": true,
	"ansible": true, "terraform": true, "tofu": true,
}

// classifyConfigFile reports whether a path is a configuration file, and is deliberately narrower than
// the rule it replaces.
//
// `isManifest` is passed in rather than recomputed so a file cannot be both: a `package.json` is a
// manifest, and reporting it in both lists made the two counts disagree with the file count.
func classifyConfigFile(relPath string, isManifest bool) bool {
	if isManifest {
		return false
	}
	base := path.Base(relPath)
	if configFileNames[base] {
		return true
	}
	ext := strings.ToLower(path.Ext(base))
	if !configExtensions[ext] {
		return false
	}
	for _, segment := range strings.Split(path.Dir(relPath), "/") {
		if configDirs[strings.ToLower(segment)] {
			return true
		}
	}
	// A `.yaml` at the repository root is configuration; one buried in `src/` is data.
	return path.Dir(relPath) == "."
}
