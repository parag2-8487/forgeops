// SPDX-License-Identifier: Apache-2.0
package chunking

import (
	"fmt"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/scanner/ast"
)

func TestChunker(t *testing.T) {
	chunker := NewChunker()

	// Generate sample source code (~1000 words to trigger multiple chunks)
	var sb strings.Builder
	for i := 1; i <= 1000; i++ {
		sb.WriteString(fmt.Sprintf("func Statement_%d() { println(\"line %d\") }\n", i, i))
	}
	src := []byte(sb.String())

	tree := &ast.Tree{
		Language: "go",
		Root:     &ast.Node{Type: "program"},
	}

	chunks := chunker.ChunkTree(tree, src)
	if len(chunks) == 0 {
		t.Fatalf("expected chunks to be generated, got 0")
	}

	for i, chunk := range chunks {
		if chunk.Tokens <= 0 {
			t.Errorf("chunk %d has invalid token count: %d", i, chunk.Tokens)
		}
		if chunk.StartLine > chunk.EndLine {
			t.Errorf("chunk %d has invalid line range: %d..%d", i, chunk.StartLine, chunk.EndLine)
		}
	}
}

func TestEstimateTokens(t *testing.T) {
	tokens := EstimateTokens("hello world foo bar")
	if tokens != 4 {
		t.Errorf("expected 4 tokens, got %d", tokens)
	}
}
