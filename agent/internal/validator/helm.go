// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"fmt"
	"strings"

	"gopkg.in/yaml.v3"
)

type ChartMetadata struct {
	Name        string `yaml:"name"`
	Version     string `yaml:"version"`
	AppVersion  string `yaml:"appVersion"`
	Description string `yaml:"description"`
}

type HelmValidator struct {
	Strict bool
}

func NewHelmValidator(strict bool) *HelmValidator {
	return &HelmValidator{Strict: strict}
}

// LintChartContent validates Helm Chart.yaml metadata syntax.
func (h *HelmValidator) LintChartContent(content string) error {
	if strings.TrimSpace(content) == "" {
		return fmt.Errorf("chart content is empty")
	}

	var meta ChartMetadata
	if err := yaml.Unmarshal([]byte(content), &meta); err != nil {
		return fmt.Errorf("invalid Chart.yaml syntax: %w", err)
	}

	if meta.Name == "" {
		return fmt.Errorf("missing mandatory 'name' field in Chart.yaml")
	}
	if meta.Version == "" {
		return fmt.Errorf("missing mandatory 'version' field in Chart.yaml")
	}

	return nil
}
