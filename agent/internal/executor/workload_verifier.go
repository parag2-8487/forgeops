package executor

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"os/exec"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/models"
)

// WorkloadVerificationResult stores the outcome of workload health verification.
type WorkloadVerificationResult struct {
	Healthy         bool          `json:"healthy"`
	WorkloadType    string        `json:"workload_type"`
	TargetEndpoint  string        `json:"target_endpoint"`
	Latency         time.Duration `json:"latency"`
	Message         string        `json:"message"`
	CapturedLogs    string        `json:"captured_logs,omitempty"`
}

// VerifyWorkloadHealth executes workload-specific verification without forcing HTTP on background workers.
func VerifyWorkloadHealth(ctx context.Context, workDir string, bp *models.ProjectBlueprint, host string) (*WorkloadVerificationResult, error) {
	if host == "" {
		host = "localhost"
	}

	switch bp.WorkloadType {
	case models.WorkloadTypeBackgroundWorker:
		// Observe container process for 10 seconds to verify sustained execution
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		timeout := time.After(10 * time.Second)

		for {
			select {
			case <-timeout:
				return &WorkloadVerificationResult{
					Healthy:      true,
					WorkloadType: string(bp.WorkloadType),
					Message:      "Background worker verified: container remained continuously running without crash.",
				}, nil
			case <-ticker.C:
				psCmd := exec.CommandContext(ctx, "docker", "compose", "ps", "--format", "{{.State}}")
				psCmd.Dir = workDir
				out, err := psCmd.Output()
				state := strings.ToLower(strings.TrimSpace(string(out)))
				if err != nil || strings.Contains(state, "exited") || strings.Contains(state, "dead") {
					return &WorkloadVerificationResult{
						Healthy:      false,
						WorkloadType: string(bp.WorkloadType),
						Message:      fmt.Sprintf("Worker container stopped running unexpectedly: %s", state),
					}, fmt.Errorf("worker terminated unexpectedly: %s", state)
				}
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}

	case models.WorkloadTypeTCPService:
		port := 8080
		if bp.Network.ListenPort != nil {
			port = *bp.Network.ListenPort
		}
		target := fmt.Sprintf("%s:%d", host, port)
		timeout := time.After(25 * time.Second)
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-timeout:
				return &WorkloadVerificationResult{
					Healthy:        false,
					WorkloadType:   string(bp.WorkloadType),
					TargetEndpoint: target,
					Message:        fmt.Sprintf("TCP health check timed out connecting to %s", target),
				}, fmt.Errorf("TCP connection timeout to %s", target)
			case <-ticker.C:
				start := time.Now()
				conn, err := net.DialTimeout("tcp", target, 2*time.Second)
				if err == nil {
					conn.Close()
					return &WorkloadVerificationResult{
						Healthy:        true,
						WorkloadType:   string(bp.WorkloadType),
						TargetEndpoint: target,
						Latency:        time.Since(start),
						Message:        fmt.Sprintf("TCP socket connected successfully to %s", target),
					}, nil
				}
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}

	default: // WebService and StaticSPA: HTTP endpoint polling
		port := 3000
		if bp.Network.ListenPort != nil {
			port = *bp.Network.ListenPort
		}
		path := bp.Network.HealthCheckPath
		if path == "" {
			path = "/"
		}
		url := fmt.Sprintf("http://%s:%d%s", host, port, path)

		client := &http.Client{Timeout: 3 * time.Second}
		timeout := time.After(30 * time.Second)
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-timeout:
				return &WorkloadVerificationResult{
					Healthy:        false,
					WorkloadType:   string(bp.WorkloadType),
					TargetEndpoint: url,
					Message:        fmt.Sprintf("HTTP health check timed out polling %s", url),
				}, fmt.Errorf("HTTP health check timeout for %s", url)
			case <-ticker.C:
				start := time.Now()
				req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
				resp, err := client.Do(req)
				if err == nil {
					status := resp.StatusCode
					_ = resp.Body.Close()
					if status >= 200 && status < 400 {
						return &WorkloadVerificationResult{
							Healthy:        true,
							WorkloadType:   string(bp.WorkloadType),
							TargetEndpoint: url,
							Latency:        time.Since(start),
							Message:        fmt.Sprintf("HTTP health check succeeded (status %d)", status),
						}, nil
					}
				}
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
	}
}
