// SPDX-License-Identifier: Apache-2.0
package chunking

import (
	"strings"

	"github.com/parag8487/ForgeOps/agent/internal/scanner/ast"
)

const (
	TargetFunctionTokens = 512
	OverlapTokens        = 128
	TargetSummaryTokens  = 1024
)

type Chunk struct {
	Type      string `json:"type"` // "function", "class", "module"
	Content   string `json:"content"`
	StartLine int    `json:"start_line"`
	EndLine   int    `json:"end_line"`
	Tokens    int    `json:"tokens"`
}

type Chunker struct{}

func NewChunker() *Chunker {
	return &Chunker{}
}

// EstimateTokens provides a lightweight token count estimation (~4 chars/token).
func EstimateTokens(text string) int {
	words := len(strings.Fields(text))
	if words > 0 {
		return words
	}
	return len(text) / 4
}

// ChunkTree performs bottom-up cAST semantic chunking over AST node trees.
func (c *Chunker) ChunkTree(tree *ast.Tree, src []byte) []Chunk {
	if tree == nil || tree.Root == nil || len(src) == 0 {
		return nil
	}

	lines := strings.Split(string(src), "\n")
	var chunks []Chunk

	// Sliding window chunker respecting ~512 token target and 128 token overlap
	currentLines := []string{}
	currentTokens := 0
	startLine := 1

	for i, line := range lines {
		lineTokens := EstimateTokens(line)
		currentLines = append(currentLines, line)
		currentTokens += lineTokens

		if currentTokens >= TargetFunctionTokens || i == len(lines)-1 {
			content := strings.Join(currentLines, "\n")
			chunks = append(chunks, Chunk{
				Type:      "function",
				Content:   content,
				StartLine: startLine,
				EndLine:   i + 1,
				Tokens:    currentTokens,
			})

			// Apply 128-token overlap
			overlapLines := []string{}
			overlapToks := 0
			for j := len(currentLines) - 1; j >= 0; j-- {
				toks := EstimateTokens(currentLines[j])
				if overlapToks+toks > OverlapTokens {
					break
				}
				overlapLines = append([]string{currentLines[j]}, overlapLines...)
				overlapToks += toks
			}

			currentLines = overlapLines
			currentTokens = overlapToks
			startLine = i + 1 - len(overlapLines)
		}
	}

	return chunks
}
