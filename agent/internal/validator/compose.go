// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"fmt"
	"strings"

	"gopkg.in/yaml.v3"
)

type ComposeFile struct {
	Version  string                 `yaml:"version"`
	Services map[string]interface{} `yaml:"services"`
}

// ValidateComposeContent validates Docker Compose YAML in-process.
func ValidateComposeContent(content string) error {
	if strings.TrimSpace(content) == "" {
		return fmt.Errorf("compose file content is empty")
	}

	var cf ComposeFile
	if err := yaml.Unmarshal([]byte(content), &cf); err != nil {
		return fmt.Errorf("invalid YAML syntax: %w", err)
	}

	if len(cf.Services) == 0 {
		return fmt.Errorf("compose file must define at least one service under 'services'")
	}

	return nil
}
