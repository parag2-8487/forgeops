package secretscan

import "context"

// Chunk represents a piece of text to be scanned.
// Note: This mirrors the Chunk type from the analysis package (which might not exist yet).
type Chunk struct {
	Index        int
	Text         string
	Kind         string
	Symbol       string
	ParentSymbol string
	Signature    string
	StartLine    int
	EndLine      int
	TokenCount   int
	Imports      []string
}

// Scanner detects and redacts secrets before any chunk leaves the machine.
type Scanner interface {
	Scan(ctx context.Context, path string, content []byte) ([]Finding, error)
	Redact(ctx context.Context, c Chunk, findings []Finding) RedactedChunk
}

type Finding struct {
	Kind        string
	Path        string
	Line        int
	Fingerprint string
	Entropy     float32
}

// RedactedChunk is the result of redaction. It has no exported fields that can be modified,
// ensuring the only constructor is secretscan.Redact.
type RedactedChunk struct {
	text string
}

// Text returns the redacted text.
func (r RedactedChunk) Text() string {
	return r.text
}
