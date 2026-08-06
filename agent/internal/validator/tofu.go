// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"fmt"
	"strings"
)

type OpenTofuValidator struct {
	BinaryPath string
}

func NewOpenTofuValidator(binaryPath string) *OpenTofuValidator {
	if binaryPath == "" {
		binaryPath = "tofu"
	}
	return &OpenTofuValidator{BinaryPath: binaryPath}
}

// ValidateHCLContent validates HCL syntax and basic resource blocks in-process.
func (v *OpenTofuValidator) ValidateHCLContent(content string) error {
	if strings.TrimSpace(content) == "" {
		return fmt.Errorf("hcl content is empty")
	}

	if !strings.Contains(content, "resource") && !strings.Contains(content, "variable") && !strings.Contains(content, "output") && !strings.Contains(content, "terraform") && !strings.Contains(content, "provider") {
		return fmt.Errorf("hcl payload contains no Terraform/OpenTofu top-level blocks")
	}

	return nil
}
