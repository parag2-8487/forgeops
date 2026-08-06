// SPDX-License-Identifier: Apache-2.0
package identity

import (
	"testing"
)

func TestGenerateWorkloadSPIFFEID_Valid(t *testing.T) {
	p := NewSPIFFEIdentityProvider("cluster.local")
	id, err := p.GenerateWorkloadSPIFFEID("prod", "backend-svc")
	if err != nil {
		t.Fatalf("expected valid SPIFFE ID, got err: %v", err)
	}
	expected := "spiffe://cluster.local/ns/prod/sa/backend-svc"
	if id != expected {
		t.Fatalf("expected %s, got %s", expected, id)
	}
}

func TestGenerateWorkloadSPIFFEID_Invalid(t *testing.T) {
	p := NewSPIFFEIdentityProvider("")
	if _, err := p.GenerateWorkloadSPIFFEID("", "svc"); err == nil {
		t.Fatalf("expected error for empty namespace, got nil")
	}
}
