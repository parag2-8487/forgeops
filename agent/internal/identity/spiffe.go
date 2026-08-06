// SPDX-License-Identifier: Apache-2.0
package identity

import (
	"fmt"
	"strings"
)

type SPIFFEIdentityProvider struct {
	TrustDomain string
}

func NewSPIFFEIdentityProvider(trustDomain string) *SPIFFEIdentityProvider {
	if trustDomain == "" {
		trustDomain = "cluster.local"
	}
	return &SPIFFEIdentityProvider{TrustDomain: trustDomain}
}

// GenerateWorkloadSPIFFEID returns structured SPIFFE ID for workload.
func (p *SPIFFEIdentityProvider) GenerateWorkloadSPIFFEID(namespace, workload string) (string, error) {
	if strings.TrimSpace(namespace) == "" || strings.TrimSpace(workload) == "" {
		return "", fmt.Errorf("namespace and workload name must not be empty")
	}

	spiffeID := fmt.Sprintf("spiffe://%s/ns/%s/sa/%s", p.TrustDomain, namespace, workload)
	return spiffeID, nil
}
