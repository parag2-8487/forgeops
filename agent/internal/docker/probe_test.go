// SPDX-License-Identifier: Apache-2.0
package docker_test

import (
	"context"
	"errors"
	"testing"

	"github.com/docker/docker/api/types"
	"go.uber.org/zap/zaptest"

	"github.com/parag8487/ForgeOps/agent/internal/docker"
)

// fakeClient implements docker.DockerClient for tests.
type fakeClient struct {
	pingResp    types.Ping
	pingErr     error
	versionResp types.Version
	versionErr  error
}

func (f *fakeClient) Ping(_ context.Context) (types.Ping, error) {
	return f.pingResp, f.pingErr
}

func (f *fakeClient) ServerVersion(_ context.Context) (types.Version, error) {
	return f.versionResp, f.versionErr
}

func TestProbe_Healthy(t *testing.T) {
	fc := &fakeClient{
		pingResp:    types.Ping{OSType: "linux"},
		versionResp: types.Version{Version: "26.1.5", Os: "linux", Arch: "amd64"},
	}
	logger := zaptest.NewLogger(t)
	probe := docker.NewWithClient(logger, fc)

	result := probe.Check(context.Background())
	if result.Status != "healthy" {
		t.Errorf("expected healthy, got %q", result.Status)
	}
	if result.ServerVersion != "26.1.5" {
		t.Errorf("expected 26.1.5, got %q", result.ServerVersion)
	}
	if result.OS != "linux" {
		t.Errorf("expected linux, got %q", result.OS)
	}
	if result.Arch != "amd64" {
		t.Errorf("expected amd64, got %q", result.Arch)
	}
	if result.Error != nil {
		t.Errorf("unexpected error: %v", result.Error)
	}
}

func TestProbe_Unavailable(t *testing.T) {
	fc := &fakeClient{
		pingErr: errors.New("connection refused"),
	}
	logger := zaptest.NewLogger(t)
	probe := docker.NewWithClient(logger, fc)

	result := probe.Check(context.Background())
	if result.Status != "unavailable" {
		t.Errorf("expected unavailable, got %q", result.Status)
	}
	if result.Error == nil {
		t.Error("expected error, got nil")
	}
}

func TestProbe_PermissionDenied(t *testing.T) {
	fc := &fakeClient{
		pingErr: errors.New("permission denied while trying to connect to the Docker daemon socket"),
	}
	logger := zaptest.NewLogger(t)
	probe := docker.NewWithClient(logger, fc)

	result := probe.Check(context.Background())
	if result.Status != "unavailable" {
		t.Errorf("expected unavailable, got %q", result.Status)
	}
	if result.Error == nil {
		t.Error("expected error, got nil")
	}
}

func TestProbe_ContextCancellation(t *testing.T) {
	fc := &fakeClient{
		pingErr: context.Canceled,
	}
	logger := zaptest.NewLogger(t)
	probe := docker.NewWithClient(logger, fc)

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
