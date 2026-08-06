// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"testing"
)

func TestValidateYAMLOrJSON_ValidJSON(t *testing.T) {
	jsonStr := `{"apiVersion": "v1", "kind": "ConfigMap"}`
	if err := ValidateYAMLOrJSON(jsonStr, []string{"apiVersion", "kind"}); err != nil {
		t.Fatalf("expected valid JSON, got err: %v", err)
	}
}

func TestValidateYAMLOrJSON_ValidYAML(t *testing.T) {
	yamlStr := "apiVersion: v1\nkind: ConfigMap"
	if err := ValidateYAMLOrJSON(yamlStr, []string{"apiVersion", "kind"}); err != nil {
		t.Fatalf("expected valid YAML, got err: %v", err)
	}
}

func TestValidateYAMLOrJSON_MissingKey(t *testing.T) {
	jsonStr := `{"kind": "ConfigMap"}`
	if err := ValidateYAMLOrJSON(jsonStr, []string{"apiVersion"}); err == nil {
		t.Fatalf("expected error for missing apiVersion, got nil")
	}
}
