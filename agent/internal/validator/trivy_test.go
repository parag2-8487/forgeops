// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"testing"
)

func TestTrivyValidator_CleanConfig(t *testing.T) {
	v := NewTrivyValidator(true)
	config := "apiVersion: v1\nkind: Pod\nmetadata:\n  name: secure-pod"
	if err := v.ScanConfigContent(config); err != nil {
		t.Fatalf("expected clean config scan, got err: %v", err)
	}
}

func TestTrivyValidator_PrivilegedContainerViolation(t *testing.T) {
	v := NewTrivyValidator(true)
	config := "securityContext:\n  privileged: true"
	if err := v.ScanConfigContent(config); err == nil {
		t.Fatalf("expected error for privileged container violation, got nil")
	}
}
