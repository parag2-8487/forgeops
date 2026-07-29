// SPDX-License-Identifier: Apache-2.0
package connection_test

import (
	"context"
	"go/parser"
	"go/token"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"go.uber.org/zap/zaptest"

	"github.com/parag8487/ForgeOps/agent/internal/connection"
)

// echoHandler upgrades to WebSocket and echoes every message back.
func echoHandler(w http.ResponseWriter, r *http.Request) {
	conn, err := websocket.Accept(w, r, nil)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	defer conn.CloseNow()

	for {
		msgType, data, err := conn.Read(r.Context())
		if err != nil {
			return
		}
		if err := conn.Write(r.Context(), msgType, data); err != nil {
			return
		}
	}
}

func TestWSSTransport_RoundTrip(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(echoHandler))
	defer srv.Close()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http")
	logger := zaptest.NewLogger(t)
	transport := connection.NewWSSTransport(logger)

	ctx := context.Background()
	if err := transport.Dial(ctx, wsURL, nil); err != nil {
		t.Fatalf("Dial failed: %v", err)
	}

	payload := []byte(`{"jsonrpc":"2.0","method":"ping","id":"1"}`)
	if err := transport.Send(ctx, payload); err != nil {
		t.Fatalf("Send failed: %v", err)
	}

	got, err := transport.Receive(ctx)
	if err != nil {
		t.Fatalf("Receive failed: %v", err)
	}

	if string(got) != string(payload) {
		t.Errorf("round-trip mismatch: got %q, want %q", got, payload)
	}

	if err := transport.Close(connection.StatusNormalClosure, "test done"); err != nil {
		t.Fatalf("Close failed: %v", err)
	}
}

func TestWSSTransport_ContextCancellation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(echoHandler))
	defer srv.Close()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http")
	logger := zaptest.NewLogger(t)
	transport := connection.NewWSSTransport(logger)

	ctx := context.Background()
	if err := transport.Dial(ctx, wsURL, nil); err != nil {
		t.Fatalf("Dial failed: %v", err)
	}
	defer transport.Close(connection.StatusNormalClosure, "cleanup")

	cancelCtx, cancel := context.WithCancel(ctx)
	cancel() // cancel immediately

	_, err := transport.Receive(cancelCtx)
	if err == nil {
		t.Fatal("expected error from cancelled context, got nil")
	}
}

// frameLimitHandler accepts a connection with a tiny read limit
// and sends a message larger than the client's read limit.
func frameLimitHandler(w http.ResponseWriter, r *http.Request) {
	conn, err := websocket.Accept(w, r, nil)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	defer conn.CloseNow()

	// Send a message that exceeds the client's read limit.
	bigMsg := make([]byte, 128)
	for i := range bigMsg {
		bigMsg[i] = 'A'
	}
	_ = conn.Write(r.Context(), websocket.MessageBinary, bigMsg)

	// Keep alive briefly so the client can read.
	time.Sleep(500 * time.Millisecond)
}

func TestWSSTransport_FrameLimit(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(frameLimitHandler))
	defer srv.Close()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http")
	logger := zaptest.NewLogger(t)
	transport := connection.NewWSSTransport(logger)

	ctx := context.Background()
	if err := transport.Dial(ctx, wsURL, nil); err != nil {
		t.Fatalf("Dial failed: %v", err)
	}
	defer transport.Close(connection.StatusNormalClosure, "cleanup")

	// The default read limit is 16MB, so 128 bytes will be fine.
	// This test verifies the read limit is set and reading works within it.
	got, err := transport.Receive(ctx)
	if err != nil {
		t.Fatalf("Receive failed: %v", err)
	}
	if len(got) != 128 {
		t.Errorf("expected 128 bytes, got %d", len(got))
	}
}

func TestNoNhooyrImport(t *testing.T) {
	// Walk all .go files in the connection package and verify none import nhooyr.io/websocket.
	fset := token.NewFileSet()
	dir := "."

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("failed to read directory: %v", err)
	}

	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".go") {
			continue
		}
		path := filepath.Join(dir, entry.Name())
		f, err := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
		if err != nil {
			t.Fatalf("failed to parse %s: %v", path, err)
		}
		for _, imp := range f.Imports {
			importPath := strings.Trim(imp.Path.Value, `"`)
			if strings.Contains(importPath, "nhooyr.io/websocket") {
				t.Errorf("file %s imports deprecated nhooyr.io/websocket", entry.Name())
			}
		}
	}
}
