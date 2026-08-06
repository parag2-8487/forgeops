// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"testing"
)

func TestHelmValidator_ValidChart(t *testing.T) {
	h := NewHelmValidator(true)
	chartYaml := `
name: my-app
version: 0.1.0
appVersion: "1.0"
description: Sample Helm Chart
`
	if err := h.LintChartContent(chartYaml); err != nil {
		t.Fatalf("expected valid chart metadata, got err: %v", err)
	}
}

func TestHelmValidator_MissingName(t *testing.T) {
	h := NewHelmValidator(true)
	chartYaml := `
version: 0.1.0
`
	if err := h.LintChartContent(chartYaml); err == nil {
		t.Fatalf("expected error for missing name, got nil")
	}
}
