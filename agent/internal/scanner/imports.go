// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"path"
	"regexp"
	"strings"
)

// ImportRef is one import as it is written in the file, before resolution.
//
// The raw specifier is carried through to the backend unchanged because
// `file_dependencies.raw_specifier` is the key an UNRESOLVED edge is stored under: a later
// scan that adds the missing file can resolve the same specifier without re-reading the
// importer (see backend/src/analysis/models.py on FileDependency).
type ImportRef struct {
	// Kind is the vocabulary `file_dependencies.kind` accepts: import|require|include|use.
	Kind      string
	Specifier string
}

var (
	goImportLine  = regexp.MustCompile(`^\s*(?:import\s+)?(?:[\w.]+\s+)?"([^"]+)"\s*$`)
	pyImport      = regexp.MustCompile(`^\s*import\s+([\w.]+)`)
	pyFromImport  = regexp.MustCompile(`^\s*from\s+([.\w]+)\s+import\s+(.+)$`)
	jsImportFrom  = regexp.MustCompile(`(?:^|\s)from\s+['"]([^'"]+)['"]`)
	jsBareImport  = regexp.MustCompile(`^\s*import\s+['"]([^'"]+)['"]`)
	jsRequire     = regexp.MustCompile(`require\(\s*['"]([^'"]+)['"]\s*\)`)
	rustUse       = regexp.MustCompile(`^\s*use\s+([\w:]+)`)
	cInclude      = regexp.MustCompile(`^\s*#include\s*[<"]([^>"]+)[>"]`)
	javaImport    = regexp.MustCompile(`^\s*import\s+(?:static\s+)?([\w.*]+)\s*;`)
	terraformLine = regexp.MustCompile(`^\s*source\s*=\s*"([^"]+)"`)
)

// ExtractImports returns every import specifier in src for the given language.
//
// Duplicates are collapsed, because `uq_file_deps_from_specifier` makes one importer plus
// one specifier a single row: emitting the same specifier twice would turn a routine
// re-import into a constraint violation on the ingest path.
func ExtractImports(language string, src []byte) []ImportRef {
	lines := strings.Split(string(src), "\n")
	seen := make(map[string]bool)
	var out []ImportRef

	add := func(kind, spec string) {
		spec = strings.TrimSpace(spec)
		if spec == "" || seen[spec] {
			return
		}
		seen[spec] = true
		out = append(out, ImportRef{Kind: kind, Specifier: spec})
	}

	switch language {
	case "go":
		inBlock := false
		for _, line := range lines {
			trimmed := strings.TrimSpace(line)
			switch {
			case strings.HasPrefix(trimmed, "import ("):
				inBlock = true
			case inBlock && trimmed == ")":
				inBlock = false
			case inBlock, strings.HasPrefix(trimmed, "import "):
				if m := goImportLine.FindStringSubmatch(line); m != nil {
					add("import", m[1])
				}
			}
		}
	case "python":
		for _, line := range lines {
			if m := pyFromImport.FindStringSubmatch(line); m != nil {
				add("import", m[1])
				// `from app import helpers` names a MODULE when `app/helpers.py`
				// exists, and a function when it does not. Both forms are emitted —
				// the dotted one is what resolves to a file, the base one is what
				// resolves when the package has an `__init__.py`. Emitting only the
				// base would lose every intra-package edge in a repository that uses
				// `from pkg import module`, which is most of them.
				for _, name := range strings.Split(m[2], ",") {
					name = strings.TrimSpace(name)
					if before, _, found := strings.Cut(name, " as "); found {
						name = strings.TrimSpace(before)
					}
					if name == "" || name == "*" || !isIdentifier(name) {
						continue
					}
					add("import", strings.TrimSuffix(m[1], ".")+"."+name)
				}
				continue
			}
			if m := pyImport.FindStringSubmatch(line); m != nil {
				add("import", m[1])
			}
		}
	case "javascript", "typescript", "tsx":
		for _, line := range lines {
			if m := jsBareImport.FindStringSubmatch(line); m != nil {
				add("import", m[1])
			}
			if m := jsImportFrom.FindStringSubmatch(line); m != nil {
				add("import", m[1])
			}
			for _, m := range jsRequire.FindAllStringSubmatch(line, -1) {
				add("require", m[1])
			}
		}
	case "rust":
		for _, line := range lines {
			if m := rustUse.FindStringSubmatch(line); m != nil {
				add("use", m[1])
			}
		}
	case "java", "kotlin":
		for _, line := range lines {
			if m := javaImport.FindStringSubmatch(line); m != nil {
				add("import", m[1])
			}
		}
	case "hcl":
		for _, line := range lines {
			if m := terraformLine.FindStringSubmatch(line); m != nil {
				add("include", m[1])
			}
		}
	case "csharp":
		for _, line := range lines {
			if m := javaImport.FindStringSubmatch(strings.Replace(line, "using ", "import ", 1)); m != nil {
				add("use", m[1])
			}
		}
	default:
		for _, line := range lines {
			if m := cInclude.FindStringSubmatch(line); m != nil {
				add("include", m[1])
			}
		}
	}
	return out
}

// resolver turns a specifier into a slash-separated repository-relative path, or "" when
// the specifier points outside the scanned tree.
//
// Only in-tree resolution is attempted. An import of `net/http` or `react` genuinely has
// no file in this project, and recording it as unresolved is the correct answer — the
// alternative, dropping it, would lose the evidence that a third-party dependency exists.
type resolver struct {
	// known is every scanned path, slash-separated, used to test candidate resolutions.
	known map[string]bool
	// goModulePath is the module path from go.mod, when the tree has one. A Go import is
	// in-tree only if it is prefixed by this.
	goModulePath string
	// dirPackageFile maps a directory to the file that represents its Go package, so an
	// import of a PACKAGE can be recorded as an edge to a FILE — which is what
	// `file_dependencies.to_file_id` requires.
	dirPackageFile map[string]string
}

var jsExtensions = []string{".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

// Resolve returns the in-tree path a specifier refers to, or "" when there is none.
func (r *resolver) Resolve(language, fromPath, specifier string) string {
	fromDir := path.Dir(toSlash(fromPath))
	if fromDir == "." {
		fromDir = ""
	}

	switch language {
	case "go":
		if r.goModulePath == "" || !strings.HasPrefix(specifier, r.goModulePath) {
			return ""
		}
		dir := strings.TrimPrefix(strings.TrimPrefix(specifier, r.goModulePath), "/")
		if file, ok := r.dirPackageFile[dir]; ok {
			return file
		}
		return ""
	case "python":
		if strings.HasPrefix(specifier, ".") {
			// Relative import: leading dots walk up from the importer's package.
			up := len(specifier) - len(strings.TrimLeft(specifier, "."))
			base := fromDir
			for i := 1; i < up; i++ {
				base = path.Dir(base)
				if base == "." {
					base = ""
				}
			}
			tail := strings.ReplaceAll(strings.TrimLeft(specifier, "."), ".", "/")
			return r.firstKnown(join(base, tail+".py"), join(base, tail+"/__init__.py"))
		}
		module := strings.ReplaceAll(specifier, ".", "/")
		// Tried both from the repository root and from the importer's own directory,
		// because a project may or may not put its package on sys.path at the root.
		return r.firstKnown(
			module+".py", module+"/__init__.py",
			join(fromDir, module+".py"), join(fromDir, module+"/__init__.py"),
		)
	case "javascript", "typescript", "tsx":
		if !strings.HasPrefix(specifier, ".") {
			return ""
		}
		base := path.Clean(join(fromDir, specifier))
		candidates := []string{base}
		for _, ext := range jsExtensions {
			candidates = append(candidates, base+ext, base+"/index"+ext)
		}
		return r.firstKnown(candidates...)
	default:
		if strings.HasPrefix(specifier, ".") || strings.HasPrefix(specifier, "/") {
			return r.firstKnown(path.Clean(join(fromDir, specifier)))
		}
		return ""
	}
}

func (r *resolver) firstKnown(candidates ...string) string {
	for _, c := range candidates {
		if c != "" && r.known[c] {
			return c
		}
	}
	return ""
}

func join(base, tail string) string {
	if base == "" {
		return path.Clean(tail)
	}
	return path.Clean(base + "/" + tail)
}

func toSlash(p string) string { return strings.ReplaceAll(p, "\\", "/") }

// isIdentifier keeps `from x import (` continuations and comments out of the specifier
// set: only a bare name can be a module, and anything else would be recorded as an
// unresolved dependency that never existed.
func isIdentifier(s string) bool {
	if s == "" {
		return false
	}
	for i, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r == '_':
		case r >= '0' && r <= '9' && i > 0:
		default:
			return false
		}
	}
	return true
}
