// SPDX-License-Identifier: Apache-2.0

// Package symbols extracts top-level and nested declarations from source text.
//
// It exists because `internal/scanner/ast` cannot yet answer the question. That package
// compiles the embedded Wasm grammars and then returns a single synthetic `program` node
// covering the whole file, so a chunk built from it would carry no symbol at all — and the
// backend's `embeddings.symbol` / `parent_symbol` / `kind` columns are what turn a chunk
// from "1200 characters of something" into "the body of Repo.Save, lines 40-78"
// (see backend/src/analysis/models.py).
//
// The alternative to this package was to emit chunks with `symbol = nil` and call the
// index done, which would leave `/analysis/codebase/symbols` permanently empty, or to
// invent symbol names, which is worse than empty. What is extracted here is derived from
// the bytes on disk by declaration-line matching: shallower than a parse tree, but every
// name, kind and line range it reports is present in the file. Where a language has no
// matcher, `Extract` returns nothing rather than guessing — an honest gap is recoverable,
// a fabricated symbol is not.
package symbols

import (
	"regexp"
	"strings"
)

// Kind values are exactly the vocabulary `embeddings.kind` documents
// (function|class|module|block), plus `method` and `type` which are distinguishable from
// the declaration line and would otherwise be flattened into `function`.
const (
	KindFunction = "function"
	KindMethod   = "method"
	KindClass    = "class"
	KindType     = "type"
	KindModule   = "module"
	KindBlock    = "block"
)

// Declaration is one named region of a file, with 1-based inclusive line bounds.
type Declaration struct {
	Name      string
	Parent    string
	Kind      string
	Signature string
	StartLine int
	EndLine   int
}

// Languages that have a matcher. Reported so callers can tell "no declarations in this
// file" from "this language is not understood" — the two need different handling and
// conflating them is how a silent coverage hole appears.
var supported = map[string]bool{
	"go":         true,
	"python":     true,
	"javascript": true,
	"typescript": true,
	"tsx":        true,
}

// Supported reports whether Extract can produce declarations for a language.
func Supported(language string) bool { return supported[language] }

var (
	// Receiver group is optional, so `func f()` and `func (r *Repo) Save()` are one
	// pattern; the receiver TYPE becomes the parent symbol, which is what makes
	// `Repo.Save` expressible.
	goFunc = regexp.MustCompile(`^func\s+(?:\(\s*\w*\s*\*?(\w+)\s*\)\s*)?(\w+)\s*\(`)
	goType = regexp.MustCompile(`^type\s+(\w+)\s+`)

	pyDef   = regexp.MustCompile(`^(\s*)(?:async\s+)?def\s+(\w+)\s*\(`)
	pyClass = regexp.MustCompile(`^(\s*)class\s+(\w+)\s*[(:]`)

	jsFunc  = regexp.MustCompile(`^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*[(<]`)
	jsClass = regexp.MustCompile(`^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)`)
	// `const handler = async (req) => {` and `const handler = function () {`. Arrow
	// bindings are the dominant shape in the frontend, so omitting them would mean most
	// TypeScript files reported no symbols.
	jsArrow = regexp.MustCompile(`^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*(?::[^=]*)?=>`)
	jsIface = regexp.MustCompile(`^(?:export\s+)?(?:interface|type|enum)\s+(\w+)`)
)

// Extract returns the declarations of src, in file order.
//
// Nothing is returned for an unsupported language, and no declaration is returned whose
// name is absent from the source line it was matched on.
func Extract(language string, src []byte) []Declaration {
	if !supported[language] {
		return nil
	}
	lines := strings.Split(string(src), "\n")
	switch language {
	case "go":
		return extractGo(lines)
	case "python":
		return extractPython(lines)
	default:
		return extractJS(lines)
	}
}

func extractGo(lines []string) []Declaration {
	var out []Declaration
	for i, line := range lines {
		switch {
		case goFunc.MatchString(line):
			m := goFunc.FindStringSubmatch(line)
			kind := KindFunction
			if m[1] != "" {
				kind = KindMethod
			}
			out = append(out, Declaration{
				Name:      m[2],
				Parent:    m[1],
				Kind:      kind,
				Signature: signature(line),
				StartLine: i + 1,
				EndLine:   braceEnd(lines, i),
			})
		case goType.MatchString(line):
			m := goType.FindStringSubmatch(line)
			out = append(out, Declaration{
				Name:      m[1],
				Kind:      KindType,
				Signature: signature(line),
				StartLine: i + 1,
				EndLine:   braceEnd(lines, i),
			})
		}
	}
	return out
}

func extractPython(lines []string) []Declaration {
	var out []Declaration
	// Enclosing classes as a stack of (indent, name), so a method's parent is the class
	// that lexically contains it rather than the nearest class anywhere above it.
	type scope struct {
		indent int
		name   string
	}
	var stack []scope

	for i, line := range lines {
		var indentText, name, kind string
		switch {
		case pyClass.MatchString(line):
			m := pyClass.FindStringSubmatch(line)
			indentText, name, kind = m[1], m[2], KindClass
		case pyDef.MatchString(line):
			m := pyDef.FindStringSubmatch(line)
			indentText, name, kind = m[1], m[2], KindFunction
		default:
			continue
		}
		indent := len(strings.ReplaceAll(indentText, "\t", "    "))
		for len(stack) > 0 && stack[len(stack)-1].indent >= indent {
			stack = stack[:len(stack)-1]
		}
		parent := ""
		if len(stack) > 0 {
			parent = stack[len(stack)-1].name
		}
		if kind == KindFunction && parent != "" {
			kind = KindMethod
		}
		out = append(out, Declaration{
			Name:      name,
			Parent:    parent,
			Kind:      kind,
			Signature: signature(line),
			StartLine: i + 1,
			EndLine:   indentEnd(lines, i, indent),
		})
		if kind == KindClass {
			stack = append(stack, scope{indent: indent, name: name})
		}
	}
	return out
}

func extractJS(lines []string) []Declaration {
	var out []Declaration
	for i, line := range lines {
		trimmed := strings.TrimLeft(line, " \t")
		var name, kind string
		switch {
		case jsClass.MatchString(trimmed):
			name, kind = jsClass.FindStringSubmatch(trimmed)[1], KindClass
		case jsFunc.MatchString(trimmed):
			name, kind = jsFunc.FindStringSubmatch(trimmed)[1], KindFunction
		case jsArrow.MatchString(trimmed):
			name, kind = jsArrow.FindStringSubmatch(trimmed)[1], KindFunction
		case jsIface.MatchString(trimmed):
			name, kind = jsIface.FindStringSubmatch(trimmed)[1], KindType
		default:
			continue
		}
		out = append(out, Declaration{
			Name:      name,
			Kind:      kind,
			Signature: signature(line),
			StartLine: i + 1,
			EndLine:   braceEnd(lines, i),
		})
	}
	return out
}

// signature is the declaration line itself, trimmed and bounded.
//
// Bounded because `embeddings.signature` is TEXT but a minified line can be the whole
// file, and a "signature" that long is not a signature.
func signature(line string) string {
	s := strings.TrimSpace(line)
	s = strings.TrimSuffix(s, "{")
	s = strings.TrimSpace(s)
	if len(s) > 512 {
		return s[:512]
	}
	return s
}

// braceEnd returns the 1-based line on which the block opened at start closes.
//
// Counts braces rather than parsing, and deliberately ignores braces inside strings and
// comments — the failure mode of that simplification is an end line that is too late,
// which produces a chunk with extra context. The alternative failure mode, guessing
// `start+1`, silently truncates function bodies out of the index.
func braceEnd(lines []string, start int) int {
	depth := 0
	opened := false
	for i := start; i < len(lines); i++ {
		for _, r := range lines[i] {
			switch r {
			case '{':
				depth++
				opened = true
			case '}':
				depth--
			}
		}
		if opened && depth <= 0 {
			return i + 1
		}
		// A one-line declaration with no braces at all (`type ID string`,
		// `export type X = Y;`) ends where it starts.
		if !opened && i == start {
			return start + 1
		}
	}
	return len(lines)
}

// indentEnd returns the last line of a Python block introduced at `indent`.
//
// Blank lines are absorbed rather than terminating the block, because a blank line inside
// a function body is not the end of the function.
func indentEnd(lines []string, start int, indent int) int {
	end := start + 1
	for i := start + 1; i < len(lines); i++ {
		line := lines[i]
		if strings.TrimSpace(line) == "" {
			continue
		}
		expanded := strings.ReplaceAll(line, "\t", "    ")
		lead := len(expanded) - len(strings.TrimLeft(expanded, " "))
		if lead <= indent {
			return end
		}
		end = i + 1
	}
	return end
}
