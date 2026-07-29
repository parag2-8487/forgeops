// SPDX-License-Identifier: Apache-2.0
package connection

import (
	"context"
	"net/http"

	"github.com/coder/websocket"
)

// StatusCode re-exports websocket status codes for consumer convenience.
type StatusCode = websocket.StatusCode

// Common websocket close status codes.
const (
	StatusNormalClosure   StatusCode = websocket.StatusNormalClosure
	StatusGoingAway       StatusCode = websocket.StatusGoingAway
	StatusInternalError   StatusCode = websocket.StatusInternalError
	StatusPolicyViolation StatusCode = websocket.StatusPolicyViolation
)

// Transport is the low-level connection abstraction for the agent.
type Transport interface {
	// Dial opens a connection to the given URL with optional headers.
	Dial(ctx context.Context, url string, hdr http.Header) error

	// Send writes a complete message payload.
	Send(ctx context.Context, payload []byte) error

	// Receive reads the next complete message payload.
	Receive(ctx context.Context) ([]byte, error)

	// Close terminates the connection with the given status code and reason.
	Close(code StatusCode, reason string) error
}
