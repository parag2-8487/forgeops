// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"encoding/json"
	"fmt"
	"strings"

	"gopkg.in/yaml.v3"
)

// ValidateYAMLOrJSON validates raw content against expected key requirements.
func ValidateYAMLOrJSON(content string, requiredKeys []string) error {
	if strings.TrimSpace(content) == "" {
		return fmt.Errorf("content is empty")
	}

	var data map[string]interface{}
	// Attempt JSON unmarshaling first
	if err := json.Unmarshal([]byte(content), &data); err != nil {
		// Fallback to YAML unmarshaling
		if errYaml := yaml.Unmarshal([]byte(content), &data); errYaml != nil {
			return fmt.Errorf("content is neither valid JSON nor YAML: json_err=%v, yaml_err=%v", err, errYaml)
		}
	}

	for _, k := range requiredKeys {
		if _, exists := data[k]; !exists {
			return fmt.Errorf("missing required key %q in payload schema", k)
		}
	}

	return nil
}
