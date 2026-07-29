// SPDX-License-Identifier: Apache-2.0
package k8s

import (
	"context"

	"go.uber.org/zap"
	"k8s.io/client-go/discovery"
	"k8s.io/client-go/tools/clientcmd"
)

// ProbeResult holds the outcome of a Kubernetes cluster health check.
type ProbeResult struct {
	Status        string
	Context       string
	ServerVersion string
	Error         error
}

// DiscoveryClient is the minimal interface consumed by Probe for testability.
type DiscoveryClient interface {
	ServerVersion() (string, error)
}

// Probe performs a read-only health check against the current Kubernetes cluster.
type Probe struct {
	logger    *zap.Logger
	discovery DiscoveryClient
}

// New creates a Probe using the default kubeconfig discovery.
func New(logger *zap.Logger) *Probe {
	return &Probe{logger: logger}
}

// NewWithDiscovery creates a Probe with a provided DiscoveryClient (for testing).
func NewWithDiscovery(logger *zap.Logger, dc DiscoveryClient) *Probe {
	return &Probe{logger: logger, discovery: dc}
}

// Check determines the current Kubernetes context and server version.
func (p *Probe) Check(ctx context.Context) ProbeResult {
	// Check for context cancellation early.
	select {
	case <-ctx.Done():
		return ProbeResult{
			Status: "unavailable",
			Error:  ctx.Err(),
		}
	default:
	}

	// Determine current context name from kubeconfig.
	currentContext := p.currentContext()

	// Use injected discovery or build from kubeconfig.
	dc := p.discovery
	if dc == nil {
		realDC, err := p.buildDiscovery()
		if err != nil {
			p.logger.Debug("k8s discovery client creation failed", zap.Error(err))
			return ProbeResult{
				Status:  "unavailable",
				Context: currentContext,
				Error:   err,
			}
		}
		dc = &realDiscoveryAdapter{client: realDC}
	}

	ver, err := dc.ServerVersion()
	if err != nil {
		p.logger.Debug("k8s server version failed", zap.Error(err))
		return ProbeResult{
			Status:  "unavailable",
			Context: currentContext,
			Error:   err,
		}
	}

	return ProbeResult{
		Status:        "healthy",
		Context:       currentContext,
		ServerVersion: ver,
	}
}

// currentContext returns the active kubeconfig context name or empty string.
func (p *Probe) currentContext() string {
	rules := clientcmd.NewDefaultClientConfigLoadingRules()
	config, err := rules.Load()
	if err != nil {
		return ""
	}
	return config.CurrentContext
}

// buildDiscovery creates a real Kubernetes discovery client from kubeconfig.
func (p *Probe) buildDiscovery() (discovery.DiscoveryInterface, error) {
	rules := clientcmd.NewDefaultClientConfigLoadingRules()
	overrides := &clientcmd.ConfigOverrides{}
	kubeConfig := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(rules, overrides)

	restConfig, err := kubeConfig.ClientConfig()
	if err != nil {
		return nil, err
	}

	return discovery.NewDiscoveryClientForConfig(restConfig)
}

// realDiscoveryAdapter adapts the real discovery client to our DiscoveryClient interface.
type realDiscoveryAdapter struct {
	client discovery.DiscoveryInterface
}

func (a *realDiscoveryAdapter) ServerVersion() (string, error) {
	info, err := a.client.ServerVersion()
	if err != nil {
		return "", err
	}
	return info.GitVersion, nil
}
