// SPDX-License-Identifier: Apache-2.0
package mcp

import (
	"context"
	"io"
	"os"
	"time"

	"github.com/mark3labs/mcp-go/server"
	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/fileops"
	"github.com/parag8487/ForgeOps/agent/internal/iac"
	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

// Deps holds the injected dependencies for the MCP server.
type Deps struct {
	Logger *zap.Logger
	Tracer telemetry.Tracer
	Tofu   iac.Runner
	Files  fileops.Ops
}

// Server wraps the mcp-go server with ForgeOps handlers.
type Server struct {
	deps    Deps
	version string
	srv     *server.MCPServer
	started time.Time
	stdin   io.Reader
	stdout  io.Writer
}

// NewServer builds an MCP server exposing the Phase 0 tool set.
func NewServer(d Deps, version string) *Server {
	s := &Server{
		deps:    d,
		version: version,
		started: time.Now(),
		stdin:   os.Stdin,
		stdout:  os.Stdout,
	}

	mcpServer := server.NewMCPServer(
		"forgeops-agent",
		version,
		server.WithToolCapabilities(true),
	)

	s.srv = mcpServer
	s.registerTools()
	return s
}

// SetIO overrides the default stdin/stdout for testing.
func (s *Server) SetIO(in io.Reader, out io.Writer) {
	s.stdin = in
	s.stdout = out
}

// Serve runs the MCP server on stdio or HTTP/SSE depending on transport config.
// Returns nil when ctx is cancelled.
func (s *Server) Serve(ctx context.Context) error {
	// Phase 0: stdio transport only
	return s.serveStdio(ctx)
}

// Close is a no-op for the stdio transport. Needed for the App.Close() contract.
func (s *Server) Close() error {
	return nil
}

func (s *Server) serveStdio(ctx context.Context) error {
	stdio := server.NewStdioServer(s.srv)
	errCh := make(chan error, 1)
	go func() {
		errCh <- stdio.Listen(ctx, s.stdin, s.stdout)
	}()
	select {
	case <-ctx.Done():
		return nil
	case err := <-errCh:
		return err
	}
}

// MCPServer returns the underlying mcp-go server for testing.
func (s *Server) MCPServer() *server.MCPServer {
	return s.srv
}
