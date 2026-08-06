// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"fmt"
	"strings"
)

type TrivySeverity string

const (
	SeverityCritical TrivySeverity = "CRITICAL"
	SeverityHigh     TrivySeverity = "HIGH"
	SeverityMedium   TrivySeverity = "MEDIUM"
	SeverityLow      TrivySeverity = "LOW"
)

type TrivyValidator struct {
	FailClosed bool
}

func NewTrivyValidator(failClosed bool) *TrivyValidator {
	return &TrivyValidator{FailClosed: failClosed}
}

// ScanConfigContent checks configuration text for critical security misconfigurations.
func (t *TrivyValidator) ScanConfigContent(content string) error {
	if strings.TrimSpace(content) == "" {
		return fmt.Errorf("config content is empty")
	}

	if strings.Contains(content, "privileged: true") {
		return fmt.Errorf("security violation [CRITICAL]: privileged container execution detected")
	}
	if strings.Contains(content, "0.0.0.0/0") && strings.Contains(content, "ingress") {
		return fmt.Errorf("security violation [HIGH]: open wildcard ingress CIDR 0.0.0.0/0 detected")
	}

	return nil
}
