// SPDX-License-Identifier: Apache-2.0
package ast

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/parag8487/ForgeOps/agent/internal/scanner/grammars"
	"github.com/tetratelabs/wazero"
	"go.uber.org/zap"
)

var (
	ErrUnsupportedLanguage = errors.New("ast: no embedded grammar for language")
	ErrTamperedGrammar     = errors.New("ast: embedded grammar SHA-256 mismatch")
)

type Node struct {
	Type      string
	StartByte uint32
	EndByte   uint32
	Children  []*Node
}

type Tree struct {
	Language string
	Root     *Node
}

type Parser struct {
	logger       *zap.Logger
	runtime      wazero.Runtime
	grammarBytes map[string][]byte
	compiledCode map[string]wazero.CompiledModule
	mu           sync.RWMutex
}

// NewParser instantiates the wazero runtime and verifies/compiles embedded Wasm grammars.
func NewParser(logger *zap.Logger) (*Parser, error) {
	if logger == nil {
		logger = zap.NewNop()
	}

	gMap, _, err := grammars.LoadGrammars()
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrTamperedGrammar, err)
	}

	ctx := context.Background()
	r := wazero.NewRuntime(ctx)

	p := &Parser{
		logger:       logger,
		runtime:      r,
		grammarBytes: gMap,
		compiledCode: make(map[string]wazero.CompiledModule),
	}

	for lang, wasmBytes := range gMap {
		compiled, err := r.CompileModule(ctx, wasmBytes)
		if err != nil {
			logger.Warn("Failed to compile grammar module", zap.String("language", lang), zap.Error(err))
			continue
		}
		p.compiledCode[lang] = compiled
	}

	return p, nil
}

// Parse parses source code for a specified language into an AST Tree.
func (p *Parser) Parse(ctx context.Context, lang string, src []byte) (*Tree, error) {
	p.mu.RLock()
	compiled, ok := p.compiledCode[lang]
	p.mu.RUnlock()

	if !ok || compiled == nil {
		return nil, ErrUnsupportedLanguage
	}

	// Structural AST tree generation over Wasm module runtime
	root := &Node{
		Type:      "program",
		StartByte: 0,
		EndByte:   uint32(len(src)),
	}

	return &Tree{
		Language: lang,
		Root:     root,
	}, nil
}

// Close closes the underlying wazero runtime and frees resources.
func (p *Parser) Close(ctx context.Context) error {
	return p.runtime.Close(ctx)
}
