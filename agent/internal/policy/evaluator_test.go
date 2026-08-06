package policy

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

func createTestBundle(t *testing.T, regoContent string) ([]byte, string) {
	t.Helper()

	var buf bytes.Buffer
	gw := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gw)

	// Add governance.rego
	hdr := &tar.Header{
		Name: "governance.rego",
		Mode: 0600,
		Size: int64(len(regoContent)),
	}
	if err := tw.WriteHeader(hdr); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write([]byte(regoContent)); err != nil {
		t.Fatal(err)
	}

	// Add .manifest to make it a valid OPA bundle
	manifest := `{"revision":"1","roots":["forgeops"]}`
	hdr2 := &tar.Header{
		Name: ".manifest",
		Mode: 0600,
		Size: int64(len(manifest)),
	}
	if err := tw.WriteHeader(hdr2); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write([]byte(manifest)); err != nil {
		t.Fatal(err)
	}

	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gw.Close(); err != nil {
		t.Fatal(err)
	}

	data := buf.Bytes()
	hash := sha256.Sum256(data)
	digest := hex.EncodeToString(hash[:])

	return data, digest
}

func TestEvaluator_NoBundle(t *testing.T) {
	e := NewEvaluator()
	ctx := context.Background()

	result, err := e.Evaluate(ctx, map[string]interface{}{}, "some-digest")
	if err != ErrNoBundle {
		t.Fatalf("expected ErrNoBundle, got %v", err)
	}
	if result["result"] != "deny" {
		t.Fatalf("expected deny on no bundle, got %q", result["result"])
	}
}

func TestEvaluator_LoadAndEvaluate(t *testing.T) {
	regoCode := `
package forgeops.governance
default decision = {"result": "deny"}
decision = {"result": "allow"} { input.action == "allow_me" }
decision = {"result": "require_approval"} { input.action == "approve_me" }
`
	bundleData, digest := createTestBundle(t, regoCode)

	e := NewEvaluator()
	ctx := context.Background()

	err := e.Load(ctx, bundleData)
	if err != nil {
		t.Fatalf("failed to load bundle: %v", err)
	}

	if e.BundleDigest() != digest {
		t.Fatalf("expected digest %q, got %q", digest, e.BundleDigest())
	}

	// Drift check
	result, err := e.Evaluate(ctx, map[string]interface{}{"action": "allow_me"}, "wrong-digest")
	if err != ErrDrift {
		t.Fatalf("expected ErrDrift on mismatched digest, got %v", err)
	}
	if result["result"] != "deny" {
		t.Fatalf("expected deny on drift, got %q", result["result"])
	}

	// Correct digest, allowed
	result, err = e.Evaluate(ctx, map[string]interface{}{"action": "allow_me"}, digest)
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	if result["result"] != "allow" {
		t.Fatalf("expected allow, got %q", result["result"])
	}

	// Correct digest, denied
	result, err = e.Evaluate(ctx, map[string]interface{}{"action": "deny_me"}, digest)
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	if result["result"] != "deny" {
		t.Fatalf("expected deny, got %q", result["result"])
	}

	// Correct digest, require_approval
	result, err = e.Evaluate(ctx, map[string]interface{}{"action": "approve_me"}, digest)
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	if result["result"] != "require_approval" {
		t.Fatalf("expected require_approval, got %q", result["result"])
	}
}

func TestEvaluator_FailedLoadPreservesPriorBundle(t *testing.T) {
	regoCode := `
package forgeops.governance
default decision = {"result": "deny"}
decision = {"result": "allow"} { input.action == "allow_me" }
`
	bundleData, digest := createTestBundle(t, regoCode)

	e := NewEvaluator()
	ctx := context.Background()

	if err := e.Load(ctx, bundleData); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}

	// Attempt to load invalid bundle
	invalidData := []byte("not a tarball")
	err := e.Load(ctx, invalidData)
	if err == nil {
		t.Fatalf("expected error loading invalid bundle")
	}

	// Prior bundle should still be active
	if e.BundleDigest() != digest {
		t.Fatalf("digest should remain %q, got %q", digest, e.BundleDigest())
	}

	result, err := e.Evaluate(ctx, map[string]interface{}{"action": "allow_me"}, digest)
	if err != nil {
		t.Fatalf("evaluation should still work, got err %v", err)
	}
	if result["result"] != "allow" {
		t.Fatalf("prior bundle should still evaluate correctly")
	}
}
