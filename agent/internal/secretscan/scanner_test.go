package secretscan_test

import (
	"context"
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
