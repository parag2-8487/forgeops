// SPDX-License-Identifier: Apache-2.0
package ast

import (
	"context"
	"errors"
	"testing"

	"go.uber.org/zap"
)

func TestNewParser(t *testing.T) {
	logger := zap.NewNop()
	p, err := NewParser(logger)
	if err != nil {
		t.Fatalf("NewParser failed: %v", err)
	}
	defer p.Close(context.Background())

	if p == nil {
		t.Fatal("expected non-nil Parser")
	}
}

func TestParseSupportedLanguages(t *testing.T) {
	p, err := NewParser(zap.NewNop())
	if err != nil {
		t.Fatalf("NewParser failed: %v", err)
	}
	ctx := context.Background()
	defer p.Close(ctx)

	languages := []string{
		"javascript", "typescript", "tsx", "python", "go",
		"rust", "java", "kotlin", "ruby", "php", "csharp", "yaml",
	}

	for _, lang := range languages {
		tree, err := p.Parse(ctx, lang, []byte("console.log('hello world');"))
		if err != nil {
			t.Errorf("Parse failed for language %s: %v", lang, err)
			continue
		}
		if tree.Language != lang {
			t.Errorf("expected language %s, got %s", lang, tree.Language)
		}
		if tree.Root == nil {
			t.Errorf("expected root node for %s", lang)
		}
	}
}

func TestParseUnsupportedLanguage(t *testing.T) {
	p, err := NewParser(zap.NewNop())
	if err != nil {
		t.Fatalf("NewParser failed: %v", err)
	}
	ctx := context.Background()
	defer p.Close(ctx)

	_, err = p.Parse(ctx, "unsupported_lang", []byte("some code"))
	if !errors.Is(err, ErrUnsupportedLanguage) {
		t.Errorf("expected ErrUnsupportedLanguage, got %v", err)
	}
}

func BenchmarkParseThroughput(b *testing.B) {
	p, err := NewParser(zap.NewNop())
	if err != nil {
		b.Fatalf("NewParser failed: %v", err)
	}
	ctx := context.Background()
	defer p.Close(ctx)

	src := []byte("package main\n\nfunc main() {\n\tprintln(\"hello world\")\n}\n")
	b.ResetTimer()
	b.SetBytes(int64(len(src)))

	for i := 0; i < b.N; i++ {
		_, _ = p.Parse(ctx, "go", src)
	}
}
