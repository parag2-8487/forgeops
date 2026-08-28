// SPDX-License-Identifier: Apache-2.0
package secretscan_test

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/secretscan"
)

func TestInterfacesAndStructs(t *testing.T) {
	finding := secretscan.Finding{
		Kind:        "aws-access-key",
		Path:        "/path/to/file",
		Line:        10,
		Fingerprint: "deadbeef",
		Entropy:     4.5,
	}
	if finding.Kind != "aws-access-key" {
		t.Errorf("expected kind aws-access-key, got %s", finding.Kind)
	}

	chunk := secretscan.Chunk{
		Text: "some content",
	}
	if chunk.Text != "some content" {
		t.Errorf("expected chunk text")
	}

	var rc secretscan.RedactedChunk
	_ = rc // Just asserting the type exists
}

func TestScannerFindsCredentials(t *testing.T) {
	content, err := os.ReadFile("testdata/synthetic_credentials.txt")
	if err != nil {
		t.Fatalf("failed to read test file: %v", err)
	}

	scanner, err := secretscan.NewScanner()
	if err != nil {
		t.Fatalf("failed to create scanner: %v", err)
	}

	findings, err := scanner.Scan(context.Background(), "synthetic_credentials.txt", content)
	if err != nil {
		t.Fatalf("Scan failed: %v", err)
	}

	if len(findings) == 0 {
		t.Fatalf("expected to find secrets, got none")
	}

	rawValues := []string{"AKIAIOSFODNN7EXAMPLE", "ghp_123456789012345678901234567890123456"}
	for _, finding := range findings {
		for _, raw := range rawValues {
			if strings.Contains(finding.Fingerprint, raw) || strings.Contains(finding.Kind, raw) || strings.Contains(finding.Path, raw) {
				t.Errorf("finding metadata contains raw secret value: %s", raw)
			}
		}
		if finding.Kind == "" {
			t.Errorf("expected finding to have a Kind")
		}
		if finding.Fingerprint == "" {
			t.Errorf("expected finding to have a Fingerprint")
		}
	}
}

func TestRedact(t *testing.T) {
	scanner, err := secretscan.NewScanner()
	if err != nil {
		t.Fatalf("failed to create scanner: %v", err)
	}

	rawText := "my token is ghp_123456789012345678901234567890123456 here"
	findings, err := scanner.Scan(context.Background(), "file.txt", []byte(rawText))
	if err != nil {
		t.Fatalf("Scan failed: %v", err)
	}

	chunk := secretscan.Chunk{Text: rawText}
	redacted := scanner.Redact(context.Background(), chunk, findings)

	if strings.Contains(redacted.Text(), "ghp_123456789012345678901234567890123456") {
		t.Errorf("Redacted chunk still contains the raw secret!")
	}
	if !strings.Contains(redacted.Text(), "FORGEOPS_REDACTED:") {
		t.Errorf("Redacted chunk missing redaction marker, got: %s", redacted.Text())
	}
}

func TestRedactJSON(t *testing.T) {
	scanner, _ := secretscan.NewScanner()
	rawText := `{"detail": "Error matching token ghp_123456789012345678901234567890123456"}`
	chunk := secretscan.Chunk{Text: rawText}
	redacted := scanner.Redact(context.Background(), chunk, nil)

	if strings.Contains(redacted.Text(), "ghp_123456789012345678901234567890123456") {
		t.Errorf("Redacted chunk still contains the raw secret! got: %s", redacted.Text())
	}
}

type mockScanner struct{}

func (m *mockScanner) Scan(ctx context.Context, path string, content []byte) ([]secretscan.Finding, error) {
	return nil, nil
}

func (m *mockScanner) Redact(ctx context.Context, c secretscan.Chunk, findings []secretscan.Finding) secretscan.RedactedChunk {
	return secretscan.RedactedChunk{}
}

func TestScannerInterface(t *testing.T) {
	var s secretscan.Scanner = &mockScanner{}
	_ = s
}
