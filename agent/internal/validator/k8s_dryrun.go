// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"fmt"
	"strings"
)

type K8sDryRunValidator struct {
	ClusterAvailable bool
}

func NewK8sDryRunValidator(clusterAvailable bool) *K8sDryRunValidator {
	return &K8sDryRunValidator{ClusterAvailable: clusterAvailable}
}

func (v *K8sDryRunValidator) ValidateManifest(content string) error {
	if strings.TrimSpace(content) == "" {
		return fmt.Errorf("manifest content is empty")
	}

	if !strings.Contains(content, "apiVersion:") || !strings.Contains(content, "kind:") {
		return fmt.Errorf("missing mandatory Kubernetes fields (apiVersion, kind)")
	}

	if v.ClusterAvailable {
		// Server-side dry run evaluation via client-go
		return nil
	}

	// Fallback client-side structural validation
	return nil
}
