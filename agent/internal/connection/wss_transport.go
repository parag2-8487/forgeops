// SPDX-License-Identifier: Apache-2.0
package connection

import (
	"context"
	"crypto/tls"
	"net/http"
	"sync"

	"github.com/coder/websocket"
	"go.uber.org/zap"
)

// maxReadLimit is the maximum inbound message size (16 MiB).
const maxReadLimit = 16 * 1024 * 1024

// WSSTransport implements Transport over a WebSocket connection using
// github.com/coder/websocket.
type WSSTransport struct {
	logger *zap.Logger
	tls    *tls.Config
	mu     sync.Mutex
	conn   *websocket.Conn
}

// TransportOption configures a WSSTransport. Variadic and additive: Phase 0's
// `NewWSSTransport(logger)` call sites keep compiling unchanged, which is what "the Phase 0
// transport contract is consumed, not modified" (§10.3) has to mean in practice.
type TransportOption func(*WSSTransport)

// WithTLSConfig supplies the mTLS client configuration for the dial.
//
// The agent hub authenticates a peer with a client certificate AND a device token (§3.1), and
// the certificate half cannot be presented through a header. Phase 0's transport dialled with
// the default HTTP client, which offers no client certificate, so this is the seam the session
// manager needs rather than a new capability.
func WithTLSConfig(cfg *tls.Config) TransportOption {
	return func(t *WSSTransport) { t.tls = cfg }
}

// NewWSSTransport returns a new WSSTransport.
func NewWSSTransport(logger *zap.Logger, options ...TransportOption) *WSSTransport {
	t := &WSSTransport{logger: logger}
	for _, option := range options {
		option(t)
	}
	return t
}

// CloseStatusOf reports the WebSocket close code carried by err, or -1 when err is not a
// close error.
//
// Exported because the close code is protocol information, not transport trivia: §3.1 gives
// 4403 the meaning "this device is revoked", and the session manager has to act on it (wipe
// credentials) rather than treat it as one more failed connection.
func CloseStatusOf(err error) StatusCode {
	return websocket.CloseStatus(err)
}

// Dial opens a WebSocket connection to the provided URL.
func (t *WSSTransport) Dial(ctx context.Context, url string, hdr http.Header) error {
	opts := &websocket.DialOptions{
		HTTPHeader: hdr,
	}
	if t.tls != nil {
		opts.HTTPClient = &http.Client{
			Transport: &http.Transport{TLSClientConfig: t.tls},
		}
	}
	conn, _, err := websocket.Dial(ctx, url, opts)
	if err != nil {
		return err
	}
	conn.SetReadLimit(maxReadLimit)
	t.mu.Lock()
	t.conn = conn
	t.mu.Unlock()
	t.logger.Debug("websocket connected", zap.String("url", url))
	return nil
}

// Send writes one message on the connection.
//
// TEXT, not binary, and the change is a defect fix rather than a preference (finding 65). The
// backend hub reads frames with Starlette's `receive_json()`, which is text-mode: a binary
// frame makes it raise before it ever sees the JSON. Phase 0 sent binary and never dialled a
// live backend, so the two halves of one protocol had never met. Every payload this transport
// carries is UTF-8 JSON-RPC (§7.3), which is what a text frame is for.
func (t *WSSTransport) Send(ctx context.Context, payload []byte) error {
	t.mu.Lock()
	conn := t.conn
	t.mu.Unlock()
	if conn == nil {
		return ErrDisabled
	}
	return conn.Write(ctx, websocket.MessageText, payload)
}

// Receive reads the next message from the connection.
func (t *WSSTransport) Receive(ctx context.Context) ([]byte, error) {
	t.mu.Lock()
	conn := t.conn
	t.mu.Unlock()
	if conn == nil {
		return nil, ErrDisabled
	}
	_, data, err := conn.Read(ctx)
	return data, err
}

// Close gracefully closes the WebSocket connection.
func (t *WSSTransport) Close(code StatusCode, reason string) error {
	t.mu.Lock()
	conn := t.conn
	t.conn = nil
	t.mu.Unlock()
	if conn == nil {
		return nil
	}
	t.logger.Debug("websocket closing", zap.Int("code", int(code)), zap.String("reason", reason))
	return conn.Close(code, reason)
}
