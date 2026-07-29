// SPDX-License-Identifier: Apache-2.0
package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"testing"
	"time"

	"github.com/mark3labs/mcp-go/mcp"
	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/fileops"
	"github.com/parag8487/ForgeOps/agent/internal/iac"
	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

type fakeRunner struct {
	validateFn func(ctx context.Context, workdir string) (*iac.ValidateResult, error)
	planFn     func(ctx context.Context, workdir string, opts iac.PlanOptions) (*iac.PlanResult, error)
}

func (f *fakeRunner) Validate(ctx context.Context, workdir string) (*iac.ValidateResult, error) {
	if f.validateFn != nil {
		return f.validateFn(ctx, workdir)
	}
	return &iac.ValidateResult{ExitCode: 0, Diagnostics: json.RawMessage(`{"valid":true}`)}, nil
}

func (f *fakeRunner) Plan(ctx context.Context, workdir string, opts iac.PlanOptions) (*iac.PlanResult, error) {
	if f.planFn != nil {
		return f.planFn(ctx, workdir, opts)
	}
	return &iac.PlanResult{ExitCode: 2, HasChanges: true, PlanJSON: json.RawMessage(`{"changes":1}`)}, nil
}

type fakeOps struct{}

func (f *fakeOps) ApplyAtomic(ctx context.Context, root string, entries []fileops.WriteEntry) (*fileops.ApplyReport, error) {
	return nil, nil
}

func (f *fakeOps) UnifiedDiff(before, after, label string) string { return "" }

func newTestDeps() Deps {
	return Deps{
		Logger: zap.NewNop(),
		Tracer: telemetry.NoopTracer{},
		Tofu:   &fakeRunner{},
		Files:  &fakeOps{},
	}
}

func newTestDepsWithRunner(r *fakeRunner) Deps {
	return Deps{
		Logger: zap.NewNop(),
		Tracer: telemetry.NoopTracer{},
		Tofu:   r,
		Files:  &fakeOps{},
	}
}

func TestNewServer_Construction(t *testing.T) {
	s := NewServer(newTestDeps(), "1.0.0")
	if s == nil {
		t.Fatal("NewServer returned nil")
	}
	if s.version != "1.0.0" {
		t.Errorf("version: got %q, want 1.0.0", s.version)
	}
}

func TestServer_ServeContextCancellation(t *testing.T) {
	s := NewServer(newTestDeps(), "1.0.0")

	// Use a pipe so the stdio server has valid reader/writer
	pr, pw := io.Pipe()
	defer pr.Close()
	defer pw.Close()
	s.SetIO(pr, pw)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- s.Serve(ctx)
	}()

	// Give it a moment to start, then cancel
	time.Sleep(50 * time.Millisecond)
	cancel()

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Serve returned error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Serve did not return after context cancellation")
	}
}

func TestServer_Close_Idempotent(t *testing.T) {
	s := NewServer(newTestDeps(), "1.0.0")
	if err := s.Close(); err != nil {
		t.Fatalf("first Close: %v", err)
	}
	if err := s.Close(); err != nil {
		t.Fatalf("second Close: %v", err)
	}
}

// handleMessage is a test helper that sends a JSON-RPC request to the MCP server
// and returns the decoded response.
func handleMessage(t *testing.T, srv *Server, method string, params interface{}) mcp.JSONRPCMessage {
	t.Helper()
	reqMap := map[string]interface{}{
		"jsonrpc": "2.0",
		"id":      1,
		"method":  method,
	}
	if params != nil {
		reqMap["params"] = params
	}
	raw, err := json.Marshal(reqMap)
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}
	return srv.MCPServer().HandleMessage(context.Background(), raw)
}

// toolCallResponse mirrors the structure of a tool call result for test unmarshalling.
type toolCallResponse struct {
	Content []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"content"`
	IsError bool `json:"isError,omitempty"`
}

// callTool is a test helper that invokes a tool and returns the parsed response.
func callTool(t *testing.T, srv *Server, name string, args map[string]interface{}) *toolCallResponse {
	t.Helper()
	params := map[string]interface{}{
		"name": name,
	}
	if args != nil {
		params["arguments"] = args
	}
	resp := handleMessage(t, srv, "tools/call", params)
	jsonResp, ok := resp.(mcp.JSONRPCResponse)
	if !ok {
		t.Fatalf("expected JSONRPCResponse, got %T: %+v", resp, resp)
	}
	data, err := json.Marshal(jsonResp.Result)
	if err != nil {
		t.Fatalf("marshal result: %v", err)
	}
	var result toolCallResponse
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatalf("unmarshal tool response: %v\nraw: %s", err, string(data))
	}
	return &result
}

// getToolResultText extracts the text content from a tool call response.
func getToolResultText(t *testing.T, result *toolCallResponse) string {
	t.Helper()
	if len(result.Content) == 0 {
		t.Fatal("empty content in result")
	}
	return result.Content[0].Text
}

func TestToolList_ReturnsExactlyThreeTools(t *testing.T) {
	s := NewServer(newTestDeps(), "1.0.0")
	resp := handleMessage(t, s, "tools/list", map[string]interface{}{})
	jsonResp, ok := resp.(mcp.JSONRPCResponse)
	if !ok {
		t.Fatalf("expected JSONRPCResponse, got %T: %+v", resp, resp)
	}

	data, err := json.Marshal(jsonResp.Result)
	if err != nil {
		t.Fatalf("marshal result: %v", err)
	}
	var listResult mcp.ListToolsResult
	if err := json.Unmarshal(data, &listResult); err != nil {
		t.Fatalf("unmarshal ListToolsResult: %v", err)
	}

	if len(listResult.Tools) != 3 {
		t.Fatalf("expected 3 tools, got %d", len(listResult.Tools))
	}

	names := make(map[string]bool)
	for _, tool := range listResult.Tools {
		names[tool.Name] = true
	}
	expected := []string{"agent.health", "agent.tofu.validate", "agent.tofu.plan"}
	for _, name := range expected {
		if !names[name] {
			t.Errorf("tool %q not found in tool list", name)
		}
	}
}

func TestHealthTool_ReturnsValidJSON(t *testing.T) {
	s := NewServer(newTestDeps(), "2.3.4")
	result := callTool(t, s, "agent.health", nil)

	if result.IsError {
		t.Fatal("agent.health returned error")
	}

	text := getToolResultText(t, result)
	var hr healthResponse
	if err := json.Unmarshal([]byte(text), &hr); err != nil {
		t.Fatalf("invalid JSON from health: %v\ntext: %s", err, text)
	}

	if hr.Version != "2.3.4" {
		t.Errorf("version: got %q, want %q", hr.Version, "2.3.4")
	}
	if hr.Platform == "" {
		t.Error("platform is empty")
	}
	if hr.StartedAt == "" {
		t.Error("started_at is empty")
	}
	if hr.Uptime < 0 {
		t.Error("uptime is negative")
	}
}

func TestTofuValidate_ReturnsDiagnostics(t *testing.T) {
	s := NewServer(newTestDeps(), "1.0.0")
	result := callTool(t, s, "agent.tofu.validate", map[string]interface{}{
		"workdir": "/tmp/terraform",
	})

	if result.IsError {
		t.Fatal("agent.tofu.validate returned error")
	}

	text := getToolResultText(t, result)
	var vr validateResponse
	if err := json.Unmarshal([]byte(text), &vr); err != nil {
		t.Fatalf("invalid JSON: %v\ntext: %s", err, text)
	}

	if vr.ExitCode != 0 {
		t.Errorf("exit_code: got %d, want 0", vr.ExitCode)
	}
	if vr.Diagnostics == nil {
		t.Error("diagnostics is nil, expected non-nil")
	}
}

func TestTofuPlan_ReturnsHasChanges(t *testing.T) {
	s := NewServer(newTestDeps(), "1.0.0")
	result := callTool(t, s, "agent.tofu.plan", map[string]interface{}{
		"workdir": "/tmp/terraform",
	})

	if result.IsError {
		t.Fatal("agent.tofu.plan returned error")
	}

	text := getToolResultText(t, result)
	var pr planResponse
	if err := json.Unmarshal([]byte(text), &pr); err != nil {
		t.Fatalf("invalid JSON: %v\ntext: %s", err, text)
	}

	if !pr.HasChanges {
		t.Error("has_changes: got false, want true")
	}
	if pr.ExitCode != 2 {
		t.Errorf("exit_code: got %d, want 2", pr.ExitCode)
	}
	if pr.PlanJSON == nil {
		t.Error("plan_json is nil, expected non-nil")
	}
}

func TestTofuValidate_InvalidWorkdir_ReturnsError(t *testing.T) {
	runner := &fakeRunner{
		validateFn: func(_ context.Context, _ string) (*iac.ValidateResult, error) {
			return nil, iac.ErrTofuNotFound
		},
	}
	s := NewServer(newTestDepsWithRunner(runner), "1.0.0")
	result := callTool(t, s, "agent.tofu.validate", map[string]interface{}{
		"workdir": "/nonexistent",
	})

	if !result.IsError {
		t.Fatal("expected error result")
	}

	text := getToolResultText(t, result)
	var errResp map[string]string
	if err := json.Unmarshal([]byte(text), &errResp); err != nil {
		t.Fatalf("invalid error JSON: %v\ntext: %s", err, text)
	}
	if errResp["error"] == "" {
		t.Error("error field is empty")
	}
}

func TestTofuPlan_TofuNotFound_ReturnsError(t *testing.T) {
	runner := &fakeRunner{
		planFn: func(_ context.Context, _ string, _ iac.PlanOptions) (*iac.PlanResult, error) {
			return nil, iac.ErrTofuNotFound
		},
	}
	s := NewServer(newTestDepsWithRunner(runner), "1.0.0")
	result := callTool(t, s, "agent.tofu.plan", map[string]interface{}{
		"workdir": "/nonexistent",
	})

	if !result.IsError {
		t.Fatal("expected error result")
	}

	text := getToolResultText(t, result)
	var errResp map[string]string
	if err := json.Unmarshal([]byte(text), &errResp); err != nil {
		t.Fatalf("invalid error JSON: %v\ntext: %s", err, text)
	}
	if errResp["error"] == "" {
		t.Error("error field is empty")
	}
}

func TestTofuValidate_EmptyWorkdir_ReturnsError(t *testing.T) {
	s := NewServer(newTestDeps(), "1.0.0")
	result := callTool(t, s, "agent.tofu.validate", map[string]interface{}{})

	if !result.IsError {
		t.Fatal("expected error result for missing workdir")
	}
}

func TestTofuPlan_EmptyWorkdir_ReturnsError(t *testing.T) {
	s := NewServer(newTestDeps(), "1.0.0")
	result := callTool(t, s, "agent.tofu.plan", map[string]interface{}{})

	if !result.IsError {
		t.Fatal("expected error result for missing workdir")
	}
}

func TestTofuValidate_ContextCancellation(t *testing.T) {
	runner := &fakeRunner{
		validateFn: func(ctx context.Context, _ string) (*iac.ValidateResult, error) {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(5 * time.Second):
				return &iac.ValidateResult{ExitCode: 0}, nil
			}
		},
	}
	s := NewServer(newTestDepsWithRunner(runner), "1.0.0")

	ctx, cancel := context.WithCancel(context.Background())

	// Build the request manually to use our cancellable context
	reqMap := map[string]interface{}{
		"jsonrpc": "2.0",
		"id":      1,
		"method":  "tools/call",
		"params": map[string]interface{}{
			"name":      "agent.tofu.validate",
			"arguments": map[string]interface{}{"workdir": "/tmp/tf"},
		},
	}
	raw, _ := json.Marshal(reqMap)

	done := make(chan mcp.JSONRPCMessage, 1)
	go func() {
		done <- s.MCPServer().HandleMessage(ctx, raw)
	}()

	// Cancel after a brief delay
	time.Sleep(50 * time.Millisecond)
	cancel()

	select {
	case resp := <-done:
		// Verify we got a response (either error or result indicating cancellation)
		if resp == nil {
			t.Fatal("got nil response")
		}
		// The handler should have returned with a context error
		jsonResp, ok := resp.(mcp.JSONRPCResponse)
		if !ok {
			// Might be an error response; that's acceptable for cancellation
			return
		}
		data, _ := json.Marshal(jsonResp.Result)
		var result mcp.CallToolResult
		if err := json.Unmarshal(data, &result); err == nil && result.IsError {
			// Got an error result from context cancellation — correct
			return
		}
	case <-time.After(3 * time.Second):
		t.Fatal("tool did not abort after context cancellation")
	}
}

func TestTofuPlan_ContextCancellation(t *testing.T) {
	runner := &fakeRunner{
		planFn: func(ctx context.Context, _ string, _ iac.PlanOptions) (*iac.PlanResult, error) {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(5 * time.Second):
				return &iac.PlanResult{ExitCode: 0}, nil
			}
		},
	}
	s := NewServer(newTestDepsWithRunner(runner), "1.0.0")

	ctx, cancel := context.WithCancel(context.Background())

	reqMap := map[string]interface{}{
		"jsonrpc": "2.0",
		"id":      1,
		"method":  "tools/call",
		"params": map[string]interface{}{
			"name":      "agent.tofu.plan",
			"arguments": map[string]interface{}{"workdir": "/tmp/tf"},
		},
	}
	raw, _ := json.Marshal(reqMap)

	done := make(chan mcp.JSONRPCMessage, 1)
	go func() {
		done <- s.MCPServer().HandleMessage(ctx, raw)
	}()

	time.Sleep(50 * time.Millisecond)
	cancel()

	select {
	case resp := <-done:
		if resp == nil {
			t.Fatal("got nil response")
		}
		jsonResp, ok := resp.(mcp.JSONRPCResponse)
		if !ok {
			return
		}
		data, _ := json.Marshal(jsonResp.Result)
		var result mcp.CallToolResult
		if err := json.Unmarshal(data, &result); err == nil && result.IsError {
			return
		}
	case <-time.After(3 * time.Second):
		t.Fatal("tool did not abort after context cancellation")
	}
}

func TestTofuPlan_WithVars(t *testing.T) {
	var capturedOpts iac.PlanOptions
	runner := &fakeRunner{
		planFn: func(_ context.Context, _ string, opts iac.PlanOptions) (*iac.PlanResult, error) {
			capturedOpts = opts
			return &iac.PlanResult{ExitCode: 0, HasChanges: false}, nil
		},
	}
	s := NewServer(newTestDepsWithRunner(runner), "1.0.0")
	result := callTool(t, s, "agent.tofu.plan", map[string]interface{}{
		"workdir": "/tmp/tf",
		"vars":    map[string]interface{}{"region": "us-east-1", "count": "3"},
	})

	if result.IsError {
		t.Fatal("unexpected error")
	}
	if capturedOpts.Vars["region"] != "us-east-1" {
		t.Errorf("vars[region]: got %q, want us-east-1", capturedOpts.Vars["region"])
	}
	if capturedOpts.Vars["count"] != "3" {
		t.Errorf("vars[count]: got %q, want 3", capturedOpts.Vars["count"])
	}
}

func TestTofuValidate_GenericError(t *testing.T) {
	runner := &fakeRunner{
		validateFn: func(_ context.Context, _ string) (*iac.ValidateResult, error) {
			return nil, errors.New("permission denied")
		},
	}
	s := NewServer(newTestDepsWithRunner(runner), "1.0.0")
	result := callTool(t, s, "agent.tofu.validate", map[string]interface{}{
		"workdir": "/restricted",
	})

	if !result.IsError {
		t.Fatal("expected error result")
	}

	text := getToolResultText(t, result)
	var errResp map[string]string
	if err := json.Unmarshal([]byte(text), &errResp); err != nil {
		t.Fatalf("invalid error JSON: %v", err)
	}
	if errResp["error"] != "permission denied" {
		t.Errorf("error: got %q, want %q", errResp["error"], "permission denied")
	}
}

func TestToolList_Annotations(t *testing.T) {
	s := NewServer(newTestDeps(), "1.0.0")
	resp := handleMessage(t, s, "tools/list", map[string]interface{}{})
	jsonResp, ok := resp.(mcp.JSONRPCResponse)
	if !ok {
		t.Fatalf("expected JSONRPCResponse, got %T: %+v", resp, resp)
	}

	data, err := json.Marshal(jsonResp.Result)
	if err != nil {
		t.Fatalf("marshal result: %v", err)
	}
	var listResult mcp.ListToolsResult
	if err := json.Unmarshal(data, &listResult); err != nil {
		t.Fatalf("unmarshal ListToolsResult: %v", err)
	}

	// Expected blast_radius annotations per tool (encoded in descriptions)
	expected := map[string]string{
		"agent.health":        "read_only",
		"agent.tofu.validate": "read_only",
		"agent.tofu.plan":     "workspace",
	}

	for _, tool := range listResult.Tools {
		wantRadius, ok := expected[tool.Name]
		if !ok {
			t.Errorf("unexpected tool %q in list", tool.Name)
			continue
		}
		// Verify blast_radius annotation is encoded in description
		wantTag := "[blast_radius:" + wantRadius + "]"
		if !contains(tool.Description, wantTag) {
			t.Errorf("tool %q: description missing blast_radius tag %q; got %q",
				tool.Name, wantTag, tool.Description)
		}
		// Also verify programmatic annotations map
		ann, found := ToolAnnotationsFor(tool.Name)
		if !found {
			t.Errorf("ToolAnnotationsFor(%q) not found", tool.Name)
		} else if ann.BlastRadius != wantRadius {
			t.Errorf("ToolAnnotationsFor(%q).BlastRadius = %q, want %q",
				tool.Name, ann.BlastRadius, wantRadius)
		}
	}
}

func TestToolsCall_UnknownTool(t *testing.T) {
	s := NewServer(newTestDeps(), "1.0.0")

	params := map[string]interface{}{
		"name":      "agent.nonexistent.tool",
		"arguments": map[string]interface{}{},
	}
	resp := handleMessage(t, s, "tools/call", params)

	// mcp-go returns a JSONRPCError for unknown tools (INVALID_PARAMS = -32602)
	jsonErr, ok := resp.(mcp.JSONRPCError)
	if !ok {
		t.Fatalf("expected JSONRPCError for unknown tool, got %T: %+v", resp, resp)
	}
	if jsonErr.Error.Code != mcp.INVALID_PARAMS {
		t.Errorf("error code: got %d, want %d (INVALID_PARAMS)",
			jsonErr.Error.Code, mcp.INVALID_PARAMS)
	}
	if jsonErr.Error.Message == "" {
		t.Error("error message is empty")
	}
	wantSubstr := "not found"
	if !contains(jsonErr.Error.Message, wantSubstr) {
		t.Errorf("error message %q does not contain %q", jsonErr.Error.Message, wantSubstr)
	}
}

func TestToolsCall_CancellationAborts(t *testing.T) {
	// Use a channel to confirm the handler started and to detect early return
	started := make(chan struct{})
	runner := &fakeRunner{
		planFn: func(ctx context.Context, _ string, _ iac.PlanOptions) (*iac.PlanResult, error) {
			close(started)
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(10 * time.Second):
				return &iac.PlanResult{ExitCode: 0}, nil
			}
		},
	}
	s := NewServer(newTestDepsWithRunner(runner), "1.0.0")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reqMap := map[string]interface{}{
		"jsonrpc": "2.0",
		"id":      42,
		"method":  "tools/call",
		"params": map[string]interface{}{
			"name":      "agent.tofu.plan",
			"arguments": map[string]interface{}{"workdir": "/tmp/tf"},
		},
	}
	raw, _ := json.Marshal(reqMap)

	done := make(chan mcp.JSONRPCMessage, 1)
	go func() {
		done <- s.MCPServer().HandleMessage(ctx, raw)
	}()

	// Wait for handler to start (confirms the tool is actively running)
	select {
	case <-started:
		// Good — handler entered
	case <-time.After(2 * time.Second):
		t.Fatal("handler did not start within timeout")
	}

	// Cancel the context to abort
	cancel()

	select {
	case resp := <-done:
		if resp == nil {
			t.Fatal("got nil response after cancellation")
		}
		// Could be a JSONRPCResponse with IsError=true or a JSONRPCError
		switch r := resp.(type) {
		case mcp.JSONRPCResponse:
			// Use our toolCallResponse helper (same as callTool uses)
			data, _ := json.Marshal(r.Result)
			var result toolCallResponse
			if err := json.Unmarshal(data, &result); err != nil {
				t.Fatalf("unmarshal call result: %v\nraw: %s", err, string(data))
			}
			if !result.IsError {
				t.Error("expected IsError=true from cancelled tool call")
			}
			// Verify the error mentions context cancellation
			if len(result.Content) > 0 {
				text := result.Content[0].Text
				if !contains(text, "context canceled") && !contains(text, "cancel") {
					t.Logf("cancellation error text: %s", text)
				}
			}
		case mcp.JSONRPCError:
			// Also acceptable: library may return error-level response
		default:
			t.Fatalf("unexpected response type %T", resp)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("tool did not abort within 3s after context cancellation")
	}
}

// contains is a test helper checking for substring presence.
func contains(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(substr) == 0 ||
		(len(s) > 0 && stringContains(s, substr)))
}

func stringContains(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
