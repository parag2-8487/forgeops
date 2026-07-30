// SPDX-License-Identifier: Apache-2.0
//
// ifacecheck asserts that every concrete type which satisfies a project
// interface carries a compile-time `var _ Iface = (*Impl)(nil)` assertion in a
// test file (design.md §0.4.2, §0.4 clause 2, §9).
//
// Why this exists
// ---------------
// Phase 0's D-23 defect was a caller/callee signature disagreement that no tool
// could see, because collaborators arrive by injection. Go's answer is the
// compile-time assertion `var _ Iface = (*Impl)(nil)`: it cannot rot, because the
// compiler rechecks it on every build. But it can be *absent*, and an absent
// assertion is indistinguishable from a satisfied one until the day someone
// changes a method signature and only the injection site breaks — at runtime.
// This program closes that gap by finding implementations that have no assertion.
//
// Why type checking rather than grep
// ----------------------------------
// "Structurally satisfies" is a statement about method sets, not about text. A
// grep over method names would both miss implementations (a renamed method with a
// matching signature) and invent them (matching names with different signatures),
// and a check that reports work you do not have to do gets ignored. So this uses
// Go's own type checker via `go list -export` data, which is exactly what the
// compiler saw.
//
// Why its own module with no dependencies
// ---------------------------------------
// `golang.org/x/tools/go/packages` would be the conventional loader, but adding a
// tool dependency to `agent/go.mod` puts it in the shipped module graph that D-1's
// guard and the SBOM both police. `go list -deps -export` plus stdlib
// `go/importer` needs nothing outside the standard library, so this module has an
// empty require list and no go.sum at all.
//
// Usage
//
//	go run ./scripts/go/ifacecheck -dir agent -pkgs ./internal/...
//
// Exit status
//
//	0  every implementation has an assertion
//	1  an implementation is missing one, OR no interfaces were discovered
//
// The empty-set failure is deliberate and is the same guard §0.4.4 and §0.4.5 put
// on the mandatory selection and the mutation harness: a checker that silently
// discovers nothing passes forever.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"go/ast"
	"go/importer"
	"go/parser"
	"go/token"
	"go/types"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

type listedPackage struct {
	ImportPath string
	Export     string
	Standard   bool
}

// assertion is one `var _ Iface = (*Impl)(nil)` found in a test file.
type assertion struct {
	iface string // simple name, e.g. "Transport"
	impl  string // simple name, e.g. "WssTransport"
	file  string
}

// finding is one implementation with no assertion.
type finding struct {
	ifacePkg  string
	ifaceName string
	implPkg   string
	implName  string
}

func (f finding) String() string {
	return fmt.Sprintf(
		"%s.%s is satisfied by %s.%s but no contract_test.go asserts it; add:\n\tvar _ %s = (*%s)(nil)",
		short(f.ifacePkg), f.ifaceName, short(f.implPkg), f.implName, f.ifaceName, f.implName,
	)
}

func short(importPath string) string {
	if i := strings.LastIndex(importPath, "/"); i >= 0 {
		return importPath[i+1:]
	}
	return importPath
}

func main() {
	dir := flag.String("dir", "agent", "module directory to analyse")
	pattern := flag.String("pkgs", "./internal/...", "package pattern within -dir")
	flag.Parse()

	if err := run(*dir, *pattern, os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "check-go-interface-assertions: %v\n", err)
		os.Exit(1)
	}
}

func run(dir, pattern string, out io.Writer) error {
	exports, targets, err := loadExports(dir, pattern)
	if err != nil {
		return err
	}

	fset := token.NewFileSet()
	imp := importer.ForCompiler(fset, "gc", func(path string) (io.ReadCloser, error) {
		file, ok := exports[path]
		if !ok || file == "" {
			return nil, fmt.Errorf("no export data for %q", path)
		}
		return os.Open(file)
	})

	loaded := make(map[string]*types.Package, len(targets))
	for _, path := range targets {
		pkg, err := imp.Import(path)
		if err != nil {
			return fmt.Errorf("importing %s: %w", path, err)
		}
		loaded[path] = pkg
	}

	ifaces, concretes := classify(loaded, targets)
	if len(ifaces) == 0 {
		return errors.New("no exported interfaces were discovered; the check would pass vacuously")
	}

	assertions, err := collectAssertions(dir)
	if err != nil {
		return err
	}
	asserted := make(map[[2]string]bool, len(assertions))
	for _, a := range assertions {
		asserted[[2]string{a.iface, a.impl}] = true
	}

	var findings []finding
	for _, iface := range ifaces {
		for _, impl := range concretes {
			if !satisfies(impl.typ, iface.iface) {
				continue
			}
			if asserted[[2]string{iface.name, impl.name}] {
				continue
			}
			findings = append(findings, finding{
				ifacePkg: iface.pkg, ifaceName: iface.name,
				implPkg: impl.pkg, implName: impl.name,
			})
		}
	}

	sort.Slice(findings, func(i, j int) bool {
		a, b := findings[i], findings[j]
		if a.ifacePkg != b.ifacePkg {
			return a.ifacePkg < b.ifacePkg
		}
		if a.ifaceName != b.ifaceName {
			return a.ifaceName < b.ifaceName
		}
		return a.implPkg+a.implName < b.implPkg+b.implName
	})

	fmt.Fprintf(out, "ifacecheck: %d exported interfaces, %d candidate types, %d assertions found\n",
		len(ifaces), len(concretes), len(assertions))

	if len(findings) > 0 {
		for _, f := range findings {
			fmt.Fprintf(out, "MISSING: %s\n", f)
		}
		return fmt.Errorf("%d implementation(s) without a compile-time interface assertion", len(findings))
	}

	fmt.Fprintln(out, "ifacecheck: every implementation carries its assertion")
	return nil
}

func loadExports(dir, pattern string) (map[string]string, []string, error) {
	cmd := exec.Command("go", "list", "-deps", "-export", "-json=ImportPath,Export,Standard", pattern)
	cmd.Dir = dir
	cmd.Stderr = os.Stderr
	stdout, err := cmd.Output()
	if err != nil {
		return nil, nil, fmt.Errorf("go list in %s: %w", dir, err)
	}

	modulePath, err := modulePath(dir)
	if err != nil {
		return nil, nil, err
	}
	// Only the module's own internal packages are targets; their dependencies are
	// loaded for type information but never audited.
	prefix := modulePath + "/" + strings.TrimSuffix(strings.TrimPrefix(pattern, "./"), "...")

	exports := map[string]string{}
	var targets []string
	dec := json.NewDecoder(strings.NewReader(string(stdout)))
	for {
		var pkg listedPackage
		if err := dec.Decode(&pkg); err == io.EOF {
			break
		} else if err != nil {
			return nil, nil, fmt.Errorf("decoding go list output: %w", err)
		}
		exports[pkg.ImportPath] = pkg.Export
		if strings.HasPrefix(pkg.ImportPath, prefix) && pkg.Export != "" {
			targets = append(targets, pkg.ImportPath)
		}
	}
	sort.Strings(targets)
	if len(targets) == 0 {
		return nil, nil, fmt.Errorf("no packages matched %q in %s", pattern, dir)
	}
	return exports, targets, nil
}

func modulePath(dir string) (string, error) {
	cmd := exec.Command("go", "list", "-m")
	cmd.Dir = dir
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("go list -m in %s: %w", dir, err)
	}
	return strings.TrimSpace(string(out)), nil
}

type namedIface struct {
	pkg   string
	name  string
	iface *types.Interface
}

type namedConcrete struct {
	pkg  string
	name string
	typ  types.Type
}

func classify(loaded map[string]*types.Package, order []string) ([]namedIface, []namedConcrete) {
	var ifaces []namedIface
	var concretes []namedConcrete

	for _, path := range order {
		pkg := loaded[path]
		scope := pkg.Scope()
		for _, name := range scope.Names() {
			obj := scope.Lookup(name)
			tn, ok := obj.(*types.TypeName)
			if !ok || !obj.Exported() {
				continue
			}
			named, ok := tn.Type().(*types.Named)
			if !ok {
				continue
			}
			switch under := named.Underlying().(type) {
			case *types.Interface:
				// An empty interface is satisfied by everything, so auditing it
				// would produce noise proportional to the codebase.
				if under.NumMethods() > 0 {
					ifaces = append(ifaces, namedIface{pkg: path, name: name, iface: under})
				}
			default:
				concretes = append(concretes, namedConcrete{pkg: path, name: name, typ: named})
			}
		}
	}
	return ifaces, concretes
}

func satisfies(t types.Type, iface *types.Interface) bool {
	if types.Implements(t, iface) {
		return true
	}
	return types.Implements(types.NewPointer(t), iface)
}

// collectAssertions parses every contract_test.go under dir and returns the
// `var _ Iface = (*Impl)(nil)` declarations it finds.
//
// Only `contract_test.go` counts, per design.md §0.4.2. That is a convention
// check as much as a correctness check: the point is that every package has one
// obvious, greppable place where its interface obligations are stated. Accepting
// an assertion anywhere in any test file would make "the convention is complete"
// unverifiable, which is the property this program exists to verify.
//
// Assertions inside a function body are honoured as well as top-level ones — the
// compiler checks both, so refusing to count one would make this program lie.
//
// Names are compared unqualified. That is a deliberate simplification: a
// same-named interface in two packages could in principle let one assertion
// satisfy the audit for both. The alternative — full resolution of test files,
// which requires type-checking test variants — costs far more than it buys, and
// the failure mode is a missing report rather than a false report.
func collectAssertions(dir string) ([]assertion, error) {
	var found []assertion
	fset := token.NewFileSet()

	err := filepath.WalkDir(dir, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case "testdata", "vendor", ".git", "bin", "dist":
				return filepath.SkipDir
			}
			return nil
		}
		if d.Name() != "contract_test.go" {
			return nil
		}
		file, perr := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if perr != nil {
			// A test file that does not parse is the Go build's problem, not this
			// check's; skipping keeps the two failures separate and legible.
			return nil
		}
		ast.Inspect(file, func(n ast.Node) bool {
			gd, ok := n.(*ast.GenDecl)
			if !ok || gd.Tok != token.VAR {
				return true
			}
			for _, spec := range gd.Specs {
				vs, ok := spec.(*ast.ValueSpec)
				if !ok || vs.Type == nil || len(vs.Names) != 1 || vs.Names[0].Name != "_" {
					continue
				}
				ifaceName := typeIdent(vs.Type)
				if ifaceName == "" || len(vs.Values) != 1 {
					continue
				}
				implName := implIdent(vs.Values[0])
				if implName == "" {
					continue
				}
				found = append(found, assertion{iface: ifaceName, impl: implName, file: path})
			}
			return true
		})
		return nil
	})
	return found, err
}

// typeIdent returns the simple name of `Iface` or `pkg.Iface`.
func typeIdent(expr ast.Expr) string {
	switch t := expr.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.SelectorExpr:
		return t.Sel.Name
	}
	return ""
}

// implIdent recognises `(*Impl)(nil)`, `(*pkg.Impl)(nil)`, `Impl{}` and `&Impl{}`.
func implIdent(expr ast.Expr) string {
	switch v := expr.(type) {
	case *ast.CallExpr:
		// (*Impl)(nil) parses as a call whose Fun is a parenthesised star expr.
		fun := v.Fun
		if p, ok := fun.(*ast.ParenExpr); ok {
			fun = p.X
		}
		if star, ok := fun.(*ast.StarExpr); ok {
			return typeIdent(star.X)
		}
		return typeIdent(fun)
	case *ast.CompositeLit:
		return typeIdent(v.Type)
	case *ast.UnaryExpr:
		if v.Op == token.AND {
			return implIdent(v.X)
		}
	}
	return ""
}

// parseAssertionForTest exposes the name extraction to main_test.go so every
// accepted assertion spelling is covered by a test rather than by hope. It parses
// one `var _ X = Y` declaration and returns the two simple names.
func parseAssertionForTest(decl string) (iface string, impl string) {
	src := "package p\n" + decl + "\n"
	file, err := parser.ParseFile(token.NewFileSet(), "assertion.go", src, parser.SkipObjectResolution)
	if err != nil {
		return "", ""
	}
	for _, d := range file.Decls {
		gd, ok := d.(*ast.GenDecl)
		if !ok || gd.Tok != token.VAR {
			continue
		}
		for _, spec := range gd.Specs {
			vs, ok := spec.(*ast.ValueSpec)
			if !ok || vs.Type == nil || len(vs.Values) != 1 {
				continue
			}
			return typeIdent(vs.Type), implIdent(vs.Values[0])
		}
	}
	return "", ""
}
