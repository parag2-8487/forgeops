// SPDX-License-Identifier: Apache-2.0
package selfupdate

import (
	"crypto/ed25519"
	"crypto/rand"
	"testing"
)

func generateTestKeyPair() (ed25519.PublicKey, ed25519.PrivateKey) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		panic(err)
	}
	return pub, priv
}

func TestVerifySignature_Valid(t *testing.T) {
	pub, priv := generateTestKeyPair()

	content := []byte("forgeops-agent binary content v1.0.0")
	signature := ed25519.Sign(priv, content)

	err := VerifySignature(content, signature, pub)
	if err != nil {
		t.Fatalf("expected valid signature to pass, got: %v", err)
	}
}

func TestVerifySignature_TamperedContent(t *testing.T) {
	pub, priv := generateTestKeyPair()

	content := []byte("forgeops-agent binary content v1.0.0")
	signature := ed25519.Sign(priv, content)

	// Tamper with content
	tampered := []byte("forgeops-agent binary content v1.0.0-TAMPERED")
	err := VerifySignature(tampered, signature, pub)
	if err == nil {
		t.Fatal("expected tampered content to fail verification")
	}
	if err != ErrInvalidSignature {
		t.Errorf("expected ErrInvalidSignature, got: %v", err)
	}
}

func TestVerifySignature_WrongKey(t *testing.T) {
	_, priv := generateTestKeyPair()
	wrongPub, _ := generateTestKeyPair()

	content := []byte("forgeops-agent binary content v1.0.0")
	signature := ed25519.Sign(priv, content)

	err := VerifySignature(content, signature, wrongPub)
	if err == nil {
		t.Fatal("expected wrong key to fail verification")
	}
	if err != ErrInvalidSignature {
		t.Errorf("expected ErrInvalidSignature, got: %v", err)
	}
}

func TestVerifySignature_InvalidKeyLength(t *testing.T) {
	content := []byte("test")
	signature := []byte("fake-sig")
	shortKey := ed25519.PublicKey([]byte("too-short"))

	err := VerifySignature(content, signature, shortKey)
	if err == nil {
		t.Fatal("expected invalid key to fail")
	}
	if err != ErrInvalidKey {
		t.Errorf("expected ErrInvalidKey, got: %v", err)
	}
}

func TestExerciseSelfupdate(t *testing.T) {
	// Prove the minio/selfupdate dependency is genuinely linked
	v := exerciseSelfupdate()
	if v == nil {
		t.Error("expected non-nil selfupdate.Verifier")
	}
}
