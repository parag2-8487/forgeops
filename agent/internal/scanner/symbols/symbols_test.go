// SPDX-License-Identifier: Apache-2.0
package symbols_test

import (
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/scanner/symbols"
)

func find(t *testing.T, decls []symbols.Declaration, name string) symbols.Declaration {
	t.Helper()
	for _, d := range decls {
		if d.Name == name {
			return d
		}
	}
	t.Fatalf("declaration %q not found in %+v", name, decls)
	return symbols.Declaration{}
}

func TestGoFunctionsMethodsAndTypes(t *testing.T) {
	src := []byte(`package repo

import "context"

type Repo struct {
	db string
}

func New(db string) *Repo {
	return &Repo{db: db}
}

func (r *Repo) Save(ctx context.Context) error {
	if ctx == nil {
		return nil
	}
	return nil
}
`)
	decls := symbols.Extract("go", src)

	repo := find(t, decls, "Repo")
	if repo.Kind != symbols.KindType {
		t.Errorf("Repo kind = %q, want %q", repo.Kind, symbols.KindType)
	}
	if repo.StartLine != 5 || repo.EndLine != 7 {
		t.Errorf("Repo lines = %d..%d, want 5..7", repo.StartLine, repo.EndLine)
	}

	newFn := find(t, decls, "New")
	if newFn.Kind != symbols.KindFunction || newFn.Parent != "" {
		t.Errorf("New = %+v, want a parentless function", newFn)
	}

	save := find(t, decls, "Save")
	if save.Kind != symbols.KindMethod {
		t.Errorf("Save kind = %q, want %q", save.Kind, symbols.KindMethod)
	}
	// The receiver TYPE is the parent, which is what makes `Repo.Save` expressible in
	// the index rather than a bare `Save` that could belong to anything.
	if save.Parent != "Repo" {
		t.Errorf("Save parent = %q, want Repo", save.Parent)
	}
	if save.EndLine <= save.StartLine {
		t.Errorf("Save lines = %d..%d, want a multi-line body", save.StartLine, save.EndLine)
	}
	if save.Signature == "" {
		t.Error("Save has no signature")
	}
}

func TestPythonClassMethodsCarryTheirClassAsParent(t *testing.T) {
	src := []byte(`import os


class Engine:
    """Doc."""

    def evaluate(self, data):
        if not data:
            return 0
        return 1

    def helper(self):
        return 2


def module_level():
    return Engine()
`)
	decls := symbols.Extract("python", src)

	engine := find(t, decls, "Engine")
	if engine.Kind != symbols.KindClass {
		t.Errorf("Engine kind = %q, want class", engine.Kind)
	}

	evaluate := find(t, decls, "evaluate")
	if evaluate.Parent != "Engine" || evaluate.Kind != symbols.KindMethod {
		t.Errorf("evaluate = %+v, want a method of Engine", evaluate)
	}
	// A blank line inside a body is not the end of the body.
	if evaluate.EndLine < evaluate.StartLine+3 {
		t.Errorf("evaluate lines = %d..%d, want the whole body", evaluate.StartLine, evaluate.EndLine)
	}

	moduleLevel := find(t, decls, "module_level")
	if moduleLevel.Parent != "" || moduleLevel.Kind != symbols.KindFunction {
		t.Errorf("module_level = %+v, want a parentless function", moduleLevel)
	}
}

func TestTypeScriptClassesFunctionsAndArrowBindings(t *testing.T) {
	src := []byte(`import React from "react";

export interface Props {
  id: string;
}

export class Widget {
  render() {
    return null;
  }
}

export const useThing = (id: string) => {
  return id;
};

export async function load(id: string) {
  return id;
}
`)
	decls := symbols.Extract("tsx", src)

	if w := find(t, decls, "Widget"); w.Kind != symbols.KindClass {
		t.Errorf("Widget kind = %q, want class", w.Kind)
	}
	if p := find(t, decls, "Props"); p.Kind != symbols.KindType {
		t.Errorf("Props kind = %q, want type", p.Kind)
	}
	// Arrow bindings are the dominant shape in the frontend; missing them would mean
	// most TypeScript files reported no symbols at all.
	if u := find(t, decls, "useThing"); u.Kind != symbols.KindFunction {
		t.Errorf("useThing kind = %q, want function", u.Kind)
	}
	if l := find(t, decls, "load"); l.Kind != symbols.KindFunction {
		t.Errorf("load kind = %q, want function", l.Kind)
	}
}

func TestAnUnsupportedLanguageReportsNothingRatherThanGuessing(t *testing.T) {
	if got := symbols.Extract("cobol", []byte("PROGRAM-ID. HELLO.")); got != nil {
		t.Errorf("Extract for an unsupported language = %+v, want nil", got)
	}
	if symbols.Supported("cobol") {
		t.Error("Supported(cobol) = true; the gap must be visible to callers")
	}
	if !symbols.Supported("go") {
		t.Error("Supported(go) = false")
	}
}

func TestNoDeclarationsInAFileWithNone(t *testing.T) {
	decls := symbols.Extract("go", []byte("package main\n\n// only a comment\n"))
	if len(decls) != 0 {
		t.Errorf("Extract = %+v, want none", decls)
	}
}
