// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"testing"
)

func TestValidateComposeContent_Valid(t *testing.T) {
	yamlContent := `
version: '3.8'
services:
  web:
    image: nginx:latest
    ports:
      - "80:80"
`
	if err := ValidateComposeContent(yamlContent); err != nil {
		t.Fatalf("expected valid compose, got err: %v", err)
	}
}

func TestValidateComposeContent_NoServices(t *testing.T) {
	yamlContent := `
version: '3.8'
`
	if err := ValidateComposeContent(yamlContent); err == nil {
		t.Fatalf("expected error for missing services, got nil")
	}
}
