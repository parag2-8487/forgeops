// SPDX-License-Identifier: Apache-2.0
package k8s_test

import (
	"context"
	"errors"
	"testing"

	"go.uber.org/zap/zaptest"

	"github.com/parag8487/ForgeOps/agent/internal/k8s"
)

// fakeDiscovery implements k8s.DiscoveryClient for tests.
type fakeDiscovery struct {
	version string
	err     error
}

func (f *fakeDiscovery) ServerVersion() (string, error) {
	return f.version, f.err
}

func TestProbe_Healthy(t *testing.T) {
	dc := &fakeDiscovery{version: "v1.31.4"}
	logger := zaptest.NewLogger(t)
	probe := k8s.NewWithDiscovery(logger, dc)

	result := probe.Check(context.Background())
	if result.Status != "healthy" {
		t.Errorf("expected healthy, got %q", result.Status)
	}
	if result.ServerVersion != "v1.31.4" {
		t.Errorf("expected v1.31.4, got %q", result.ServerVersion)
	}
	if result.Error != nil {
		t.Errorf("unexpected error: %v", result.Error)
	}
}

func TestProbe_Unavailable(t *testing.T) {
	dc := &fakeDiscovery{err: errors.New("connection refused")}
	logger := zaptest.NewLogger(t)
	probe := k8s.NewWithDiscovery(logger, dc)

	result := probe.Check(context.Background())
	if result.Status != "unavailable" {
		t.Errorf("expected unavailable, got %q", result.Status)
	}
	if result.Error == nil {
		t.Error("expected error, got nil")
	}
}

func TestProbe_ContextCancellation(t *testing.T) {
	dc := &fakeDiscovery{version: "v1.31.4"}
	logger := zaptest.NewLogger(t)
	probe := k8s.NewWithDiscovery(logger, dc)

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	result := probe.Check(ctx)
	if result.Status != "unavailable" {
		t.Errorf("expected unavailable, got %q", result.Status)
	}
	if !errors.Is(result.Error, context.Canceled) {
		t.Errorf("expected context.Canceled, got %v", result.Error)
	}
}
