// SPDX-License-Identifier: Apache-2.0

package app_test

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The redacting logger is the ONLY logger (design §7.2, §14.5, Q-24).
//
// Why an AST assertion rather than a comment
// ------------------------------------------
// `logging.New` produces an unfiltered logger. Any subsystem that used it would log
// whatever it was given — a git remote carrying a token, an error wrapping a DSN, a
// validator echoing file content — and nothing would object. "Remember to use the
// redacting constructor" is a convention, and Phase 0 demonstrated at length what
// conventions are worth without a mechanism (D-23).
//
// So the mechanism is this test: no file under agent/internal/** may call
// `logging.New`, and `internal/app` must call `logging.NewRedacted`. A new subsystem
// that reaches for the unfiltered constructor fails the build.
//
// `logging.New` itself is deliberately left in place rather than deleted. It is the
// implementation `NewRedacted` is defined against, its own package tests it directly,
// and removing an exported function to enforce a policy is a blunter instrument than
// asserting the policy.

func agentInternalDir(t *testing.T) string {
	t.Helper()
	// This file lives at agent/internal/app/, so internal/ is two levels up.
	dir, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatalf("resolving agent root: %v", err)
	}
	return filepath.Join(dir, "internal")
}

// loggingCalls returns every `logging.<Name>(` call site under root, as
// "relative/path.go:line".
func loggingCalls(t *testing.T, root string, name string) []string {
	t.Helper()

	var found []string
	fset := token.NewFileSet()

	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if d.Name() == "testdata" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		// The logging package's own tests must exercise both constructors; the rule is
		// about CONSUMERS choosing the unfiltered one.
		if filepath.Base(filepath.Dir(path)) == "logging" {
			return nil
		}
		// This file names the function in string literals, not in calls, but skipping
		// it keeps the assertion about production wiring.
		if strings.HasSuffix(path, "app_wiring_test.go") {
			return nil
		}

		file, perr := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if perr != nil {
			t.Fatalf("parsing %s: %v", path, perr)
		}
		ast.Inspect(file, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok || sel.Sel.Name != name {
				return true
			}
			pkg, ok := sel.X.(*ast.Ident)
			if !ok || pkg.Name != "logging" {
				return true
			}
			rel, _ := filepath.Rel(root, path)
			found = append(found, filepath.ToSlash(rel)+":"+fset.Position(call.Pos()).String()[len(path)+1:])
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatalf("walking %s: %v", root, err)
	}
	return found
}

func TestWiring_NoUnfilteredLoggerIsReachable(t *testing.T) {
	t.Parallel()

	root := agentInternalDir(t)
	offenders := loggingCalls(t, root, "New")
	if len(offenders) > 0 {
		t.Fatalf(
			"logging.New (unfiltered) is called from %v.\n"+
				"Use logging.NewRedacted so a value the caller did not think about cannot be "+
				"written verbatim (design §7.2, §14.5, Q-24).",
			offenders,
		)
	}
}

func TestWiring_AppConstructsTheRedactingLogger(t *testing.T) {
	t.Parallel()

	root := agentInternalDir(t)
	calls := loggingCalls(t, root, "NewRedacted")
	if len(calls) == 0 {
		t.Fatal(
			"no call to logging.NewRedacted was found under agent/internal/**. " +
				"If the constructor was renamed, this assertion must be updated in the same " +
				"change — otherwise the previous test passes vacuously, because nothing calls " +
				"logging.New either.",
		)
	}

	// Specifically the app composition root, so the guarantee holds for the process
	// the user actually runs rather than for some test helper.
	inApp := false
	for _, call := range calls {
		if strings.HasPrefix(call, "app/") {
			inApp = true
		}
	}
	if !inApp {
		t.Errorf("internal/app does not construct the redacting logger; calls found at %v", calls)
	}
}
