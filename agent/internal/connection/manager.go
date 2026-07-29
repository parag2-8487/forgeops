// SPDX-License-Identifier: Apache-2.0
package connection

import (
	"context"
	"errors"

	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

// ErrDisabled is returned when the connection manager has no backend URL configured.
// This is the normal Phase 0 path.
var ErrDisabled = errors.New("connection manager disabled: no backend URL configured")

// Manager manages the lifecycle of the backend connection.
type Manager struct {
	url       string
	transport Transport
	logger    *zap.Logger
	tracer    telemetry.Tracer
}

// NewManager creates a Manager. If url is empty, Serve will return ErrDisabled.
func NewManager(url string, logger *zap.Logger, tracer telemetry.Tracer) *Manager {
	return &Manager{
		url:       url,
		transport: NewWSSTransport(logger),
		logger:    logger,
		tracer:    tracer,
	}
}

// Serve runs the connection manager. It returns ErrDisabled when no backend
// URL is configured (Phase 0 normal path).
func (m *Manager) Serve(ctx context.Context) error {
	if m.url == "" {
		m.logger.Info("connection manager disabled: no backend URL configured")
		return ErrDisabled
	}

	_, span := m.tracer.StartSpan(ctx, "connection.Manager.Serve")
	defer span.End()

	// Phase 0: dial not yet wired to a read loop.
	// Future phases will implement the full event loop here.
	return nil
}

// Close shuts down the transport connection.
func (m *Manager) Close() error {
	return m.transport.Close(StatusNormalClosure, "agent shutdown")
}
