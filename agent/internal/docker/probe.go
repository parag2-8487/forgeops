// SPDX-License-Identifier: Apache-2.0
package docker

import (
	"context"

	"github.com/docker/docker/api/types"
	dockerclient "github.com/docker/docker/client"
	"go.uber.org/zap"
)

// ProbeResult holds the outcome of a Docker health check.
type ProbeResult struct {
	Status        string
	ServerVersion string
	OS            string
	Arch          string
	Error         error
}

// DockerClient is the minimal interface consumed by Probe, declared here
// for testability (consumer-declared interface).
type DockerClient interface {
	Ping(ctx context.Context) (types.Ping, error)
	ServerVersion(ctx context.Context) (types.Version, error)
}

// Probe performs a read-only health check against the Docker daemon.
type Probe struct {
	logger *zap.Logger
	client DockerClient
}

// New creates a Probe using the default Docker client from the environment.
func New(logger *zap.Logger) *Probe {
	cli, err := dockerclient.NewClientWithOpts(dockerclient.FromEnv, dockerclient.WithAPIVersionNegotiation())
	if err != nil {
		logger.Warn("failed to create docker client", zap.Error(err))
		return &Probe{logger: logger, client: nil}
	}
	return &Probe{logger: logger, client: cli}
}

// NewWithClient creates a Probe with a provided DockerClient (for testing).
func NewWithClient(logger *zap.Logger, client DockerClient) *Probe {
	return &Probe{logger: logger, client: client}
}

// Check calls Ping and ServerVersion to determine Docker daemon availability.
func (p *Probe) Check(ctx context.Context) ProbeResult {
	if p.client == nil {
		return ProbeResult{
			Status: "unavailable",
			Error:  errNoClient,
		}
	}

	ping, err := p.client.Ping(ctx)
	if err != nil {
		p.logger.Debug("docker ping failed", zap.Error(err))
		return ProbeResult{
			Status: "unavailable",
			Error:  err,
		}
	}

	ver, err := p.client.ServerVersion(ctx)
	if err != nil {
		p.logger.Debug("docker server version failed", zap.Error(err))
		return ProbeResult{
			Status: "degraded",
			OS:     ping.OSType,
			Error:  err,
		}
	}

	return ProbeResult{
		Status:        "healthy",
		ServerVersion: ver.Version,
		OS:            ver.Os,
		Arch:          ver.Arch,
	}
}

var errNoClient = &probeError{msg: "docker client not initialized"}

type probeError struct{ msg string }

func (e *probeError) Error() string { return e.msg }
