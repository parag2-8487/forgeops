// SPDX-License-Identifier: Apache-2.0
package secretscan

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"

	"github.com/zricethezav/gitleaks/v8/detect"
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

// Finding represents an individual secret detection.
type Finding struct {
	Kind        string
	Path        string
	Line        int
	Fingerprint string
	Entropy     float32
}

// RedactedChunk wraps text where all detected secret literals are replaced by markers.
type RedactedChunk struct {
	text string
}

func (r RedactedChunk) String() string {
	return r.text
}

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
	//lint:ignore SA1019 detect.Fragment is required by gitleaks v8 DetectContext API
	fragment := detect.Fragment{
		Raw:      string(content),
		Bytes:    content,
		FilePath: path,
	}

	reports := s.detector.DetectContext(ctx, fragment)
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
	if findings != nil && len(findings) == 0 {
		return RedactedChunk{text: c.Text}
	}

	//lint:ignore SA1019 detect.Fragment is required by gitleaks v8 DetectContext API
	fragment := detect.Fragment{
		Raw:      c.Text,
		Bytes:    []byte(c.Text),
		FilePath: "chunk",
	}

	reports := s.detector.DetectContext(ctx, fragment)

	redactedText := c.Text

	for _, r := range reports {
		mac := hmac.New(sha256.New, []byte("forgeops-default-pepper"))
		mac.Write([]byte(r.Secret))
		hashHex := hex.EncodeToString(mac.Sum(nil))[:8]

		marker := fmt.Sprintf("FORGEOPS_REDACTED:%s:%s", r.RuleID, hashHex)
		redactedText = strings.ReplaceAll(redactedText, r.Secret, marker)
	}

	return RedactedChunk{text: redactedText}
}

// Redact is a package-level function to construct RedactedChunk for global chokepoints.
// It initializes a default scanner to redact the given text. For performance, use Scanner.Redact.
func Redact(ctx context.Context, c Chunk, findings []Finding) (RedactedChunk, error) {
	scanner, err := NewScanner()
	if err != nil {
		return RedactedChunk{text: c.Text}, err
	}
	return scanner.Redact(ctx, c, findings), nil
}
