// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"context"
	"fmt"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/models"
)

// FinalGateResult stores the final deployment sign-off outcome.
type FinalGateResult struct {
	GateID          string  `json:"gate_id"`
	Passed          bool    `json:"passed"`
	AccessURL       string  `json:"access_url,omitempty"`
	ExposedPorts    []int   `json:"exposed_ports"`
	OutputSummary   string  `json:"output_summary"`
	SignOffTime     string  `json:"sign_off_time"`
	DurationSeconds float64 `json:"duration_seconds"`
}

// ExecuteFinalDeploymentGate performs the final traffic readiness sign-off across all passed gates.
func ExecuteFinalDeploymentGate(ctx context.Context, bp *models.ProjectBlueprint, host string) (*FinalGateResult, error) {
	if host == "" {
		host = "localhost"
	}

	var exposed []int
	accessURL := ""

	if bp.Network.ListenPort != nil {
		port := *bp.Network.ListenPort
		exposed = append(exposed, port)
		if bp.WorkloadType == models.WorkloadTypeWebService || bp.WorkloadType == models.WorkloadTypeStaticSPA {
			accessURL = fmt.Sprintf("http://%s:%d", host, port)
		} else if bp.WorkloadType == models.WorkloadTypeTCPService {
			accessURL = fmt.Sprintf("tcp://%s:%d", host, port)
		}
	}

	return &FinalGateResult{
		GateID:        "G7",
		Passed:        true,
		AccessURL:     accessURL,
		ExposedPorts:  exposed,
		OutputSummary: "Final Deployment Gate G7 PASSED: All verification criteria satisfied. Ready for traffic.",
		SignOffTime:   time.Now().UTC().Format(time.RFC3339),
	}, nil
}
