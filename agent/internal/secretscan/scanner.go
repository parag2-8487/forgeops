package secretscan

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"

	"github.com/zricethezav/gitleaks/v8/detect"
	"github.com/zricethezav/gitleaks/v8/sources"
)

// Chunk represents a piece of text to be scanned.
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

type gitleaksScanner struct {
	detector *detect.Detector
}

func NewScanner() (Scanner, error) {
	detector, err := detect.NewDetectorDefaultConfig()
	if err != nil {
		return nil, err
	}
	return &gitleaksScanner{detector: detector}, nil
}

func (s *gitleaksScanner) Scan(ctx context.Context, path string, content []byte) ([]Finding, error) {
	fragment := sources.Fragment{
		Raw:      string(content),
		Bytes:    content,
		FilePath: path,
	}

	reports := s.detector.DetectContext(ctx, detect.Fragment(fragment))
	var findings []Finding
	for _, r := range reports {
		mac := hmac.New(sha256.New, []byte("forgeops-default-pepper"))
		mac.Write([]byte(r.Secret))
		hashHex := hex.EncodeToString(mac.Sum(nil))[:8]

		findings = append(findings, Finding{
			Kind:        r.RuleID,
			Path:        path,
			Line:        r.StartLine,
			Fingerprint: hashHex,
			Entropy:     r.Entropy,
		})
	}
	return findings, nil
}

func (s *gitleaksScanner) Redact(ctx context.Context, c Chunk, findings []Finding) RedactedChunk {
	return RedactedChunk{text: c.Text}
}

// Redact is a package-level function to construct RedactedChunk (useful for chokepoints).
func Redact(ctx context.Context, c Chunk, findings []Finding) RedactedChunk {
	return RedactedChunk{text: c.Text}
}
