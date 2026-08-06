// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"testing"
)

func TestK8sDryRunValidator_Valid(t *testing.T) {
	v := NewK8sDryRunValidator(false)
	manifest := "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: prod"
	if err := v.ValidateManifest(manifest); err != nil {
		t.Fatalf("expected valid manifest, got err: %v", err)
	}
}

func TestK8sDryRunValidator_MissingFields(t *testing.T) {
	v := NewK8sDryRunValidator(false)
	manifest := "metadata:\n  name: prod"
	if err := v.ValidateManifest(manifest); err == nil {
		t.Fatalf("expected error for missing apiVersion/kind, got nil")
	}
}
