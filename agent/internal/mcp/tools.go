// SPDX-License-Identifier: Apache-2.0
package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"runtime"
	"time"

	"github.com/mark3labs/mcp-go/mcp"

	"github.com/parag8487/ForgeOps/agent/internal/iac"
)

// errorResult creates a CallToolResult indicating a tool-level error.
func errorResult(msg string) *mcp.CallToolResult {
	errJSON, _ := json.Marshal(map[string]string{"error": msg})
	result := mcp.NewToolResultText(string(errJSON))
	result.IsError = true
	return result
}

// registerTools adds all Phase 0 non-mutating tools to the MCP server.
func (s *Server) registerTools() {
	s.registerHealthTool()
	s.registerTofuValidateTool()
	s.registerTofuPlanTool()
}

// ToolAnnotations holds metadata about a tool's operational scope.
type ToolAnnotations struct {
	BlastRadius string `json:"blast_radius"`
}

// toolMeta maps tool names to their annotations.
var toolMeta = map[string]ToolAnnotations{
	"agent.health":        {BlastRadius: "read_only"},
	"agent.tofu.validate": {BlastRadius: "read_only"},
	"agent.tofu.plan":     {BlastRadius: "workspace"},
}

// ToolAnnotationsFor returns the annotations for a given tool name.
// The second return value indicates whether the tool was found.
func ToolAnnotationsFor(name string) (ToolAnnotations, bool) {
	a, ok := toolMeta[name]
	return a, ok
}

// registerHealthTool registers agent.health — blast radius: read_only.
func (s *Server) registerHealthTool() {
	tool := mcp.NewTool("agent.health",
		mcp.WithDescription("Returns agent health information including version, uptime, and platform [blast_radius:read_only]"),
	)
	s.srv.AddTool(tool, s.handleHealth)
}

// registerTofuValidateTool registers agent.tofu.validate — blast radius: read_only.
func (s *Server) registerTofuValidateTool() {
	tool := mcp.NewTool("agent.tofu.validate",
		mcp.WithDescription("Run OpenTofu validate on a working directory [blast_radius:read_only]"),
		mcp.WithString("workdir", mcp.Required(), mcp.Description("Working directory path")),
	)
	s.srv.AddTool(tool, s.handleTofuValidate)
}

// registerTofuPlanTool registers agent.tofu.plan — blast radius: workspace.
func (s *Server) registerTofuPlanTool() {
	tool := mcp.NewTool("agent.tofu.plan",
		mcp.WithDescription("Run OpenTofu plan on a working directory [blast_radius:workspace]"),
		mcp.WithString("workdir", mcp.Required(), mcp.Description("Working directory path")),
		mcp.WithObject("vars", mcp.Description("Optional variables to pass to the plan")),
	)
	s.srv.AddTool(tool, s.handleTofuPlan)
}

// healthResponse is the JSON structure returned by agent.health.
type healthResponse struct {
	Version   string  `json:"version"`
	Uptime    float64 `json:"uptime"`
	Platform  string  `json:"platform"`
	StartedAt string  `json:"started_at"`
}

func (s *Server) handleHealth(_ context.Context, _ mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	resp := healthResponse{
		Version:   s.version,
		Uptime:    time.Since(s.started).Seconds(),
		Platform:  runtime.GOOS + "/" + runtime.GOARCH,
		StartedAt: s.started.UTC().Format(time.RFC3339),
	}
	data, err := json.Marshal(resp)
	if err != nil {
		return nil, fmt.Errorf("marshal health response: %w", err)
	}
	return mcp.NewToolResultText(string(data)), nil
}

// validateResponse is the JSON structure returned by agent.tofu.validate.
type validateResponse struct {
	ExitCode    int              `json:"exit_code"`
	Diagnostics *json.RawMessage `json:"diagnostics,omitempty"`
}

func (s *Server) handleTofuValidate(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	workdir, ok := req.Params.Arguments["workdir"].(string)
	if !ok || workdir == "" {
		return errorResult(`workdir is required`), nil
	}

	result, err := s.deps.Tofu.Validate(ctx, workdir)
	if err != nil {
		return errorResult(err.Error()), nil
	}

	resp := validateResponse{
		ExitCode: result.ExitCode,
	}
	if result.Diagnostics != nil {
		resp.Diagnostics = &result.Diagnostics
	}
	data, err := json.Marshal(resp)
	if err != nil {
		return nil, fmt.Errorf("marshal validate response: %w", err)
	}
	return mcp.NewToolResultText(string(data)), nil
}

// planResponse is the JSON structure returned by agent.tofu.plan.
type planResponse struct {
	ExitCode   int              `json:"exit_code"`
	HasChanges bool             `json:"has_changes"`
	PlanJSON   *json.RawMessage `json:"plan_json,omitempty"`
}

func (s *Server) handleTofuPlan(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	workdir, ok := req.Params.Arguments["workdir"].(string)
	if !ok || workdir == "" {
		return errorResult(`workdir is required`), nil
	}

	opts := iac.PlanOptions{}
	if vars, ok := req.Params.Arguments["vars"].(map[string]interface{}); ok {
		opts.Vars = make(map[string]string, len(vars))
		for k, v := range vars {
			opts.Vars[k] = fmt.Sprintf("%v", v)
		}
	}

	result, err := s.deps.Tofu.Plan(ctx, workdir, opts)
	if err != nil {
		return errorResult(err.Error()), nil
	}

	resp := planResponse{
		ExitCode:   result.ExitCode,
		HasChanges: result.HasChanges,
	}
	if result.PlanJSON != nil {
		resp.PlanJSON = &result.PlanJSON
	}
	data, err := json.Marshal(resp)
	if err != nil {
		return nil, fmt.Errorf("marshal plan response: %w", err)
	}
	return mcp.NewToolResultText(string(data)), nil
}
