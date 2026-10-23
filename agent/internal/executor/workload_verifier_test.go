// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/models"
)

func TestVerifyWorkloadHealth_TCPService(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()

	_, portStr, err := net.SplitHostPort(listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		t.Fatal(err)
	}

	bp := &models.ProjectBlueprint{
		BlueprintID:  "test-tcp-bp",
		WorkloadType: models.WorkloadTypeTCPService,
		Network: models.NetworkContract{
			ListenPort: &port,
		},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	result, err := VerifyWorkloadHealth(ctx, t.TempDir(), bp, "127.0.0.1")
	if err != nil {
		t.Fatalf("expected healthy TCP verification, got error: %v", err)
	}
	if !result.Healthy {
		t.Errorf("expected result to be healthy")
	}
	if result.WorkloadType != string(models.WorkloadTypeTCPService) {
		t.Errorf("expected workload type %s, got %s", models.WorkloadTypeTCPService, result.WorkloadType)
	}
}

func TestVerifyWorkloadHealth_WebService(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("ok"))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer ts.Close()

	host, portStr, err := net.SplitHostPort(ts.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		t.Fatal(err)
	}

	bp := &models.ProjectBlueprint{
		BlueprintID:  "test-web-bp",
		WorkloadType: models.WorkloadTypeWebService,
		Network: models.NetworkContract{
			ListenPort:      &port,
			HealthCheckPath: "/healthz",
		},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	result, err := VerifyWorkloadHealth(ctx, t.TempDir(), bp, host)
	if err != nil {
		t.Fatalf("expected healthy WebService verification, got error: %v", err)
	}
	if !result.Healthy {
		t.Errorf("expected result to be healthy")
	}
	if result.WorkloadType != string(models.WorkloadTypeWebService) {
		t.Errorf("expected workload type %s, got %s", models.WorkloadTypeWebService, result.WorkloadType)
	}
}

func TestVerifyWorkloadHealth_CancelledContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // pre-cancel context

	bp := &models.ProjectBlueprint{
		BlueprintID:  "test-worker-bp",
		WorkloadType: models.WorkloadTypeBackgroundWorker,
	}

	_, err := VerifyWorkloadHealth(ctx, t.TempDir(), bp, "localhost")
	if err == nil {
		t.Errorf("expected context cancelled error, got nil")
	}
}
