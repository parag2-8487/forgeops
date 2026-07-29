// SPDX-License-Identifier: Apache-2.0
package connection

import (
	"context"
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
	mu     sync.Mutex
	conn   *websocket.Conn
}

// NewWSSTransport returns a new WSSTransport.
func NewWSSTransport(logger *zap.Logger) *WSSTransport {
	return &WSSTransport{logger: logger}
}

// Dial opens a WebSocket connection to the provided URL.
func (t *WSSTransport) Dial(ctx context.Context, url string, hdr http.Header) error {
	opts := &websocket.DialOptions{
		HTTPHeader: hdr,
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

// Send writes a binary message on the connection.
func (t *WSSTransport) Send(ctx context.Context, payload []byte) error {
	t.mu.Lock()
	conn := t.conn
	t.mu.Unlock()
	if conn == nil {
		return ErrDisabled
	}
	return conn.Write(ctx, websocket.MessageBinary, payload)
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
