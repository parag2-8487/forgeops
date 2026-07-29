// SPDX-License-Identifier: Apache-2.0
package config

import (
	"strings"
	"testing"
	"time"
)

func makeGetenv(m map[string]string) func(string) string {
	return func(key string) string {
		return m[key]
	}
}

func TestLoad_Defaults(t *testing.T) {
	cfg, err := Load(makeGetenv(map[string]string{}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.LogLevel != "INFO" {
		t.Errorf("LogLevel = %q, want INFO", cfg.LogLevel)
	}
	if cfg.LogFormat != "console" {
		t.Errorf("LogFormat = %q, want console", cfg.LogFormat)
	}
	if cfg.ShutdownTimeout != 15*time.Second {
		t.Errorf("ShutdownTimeout = %v, want 15s", cfg.ShutdownTimeout)
	}
	if cfg.Tofu.BinaryPath != "tofu" {
		t.Errorf("Tofu.BinaryPath = %q, want tofu", cfg.Tofu.BinaryPath)
	}
	if cfg.Tofu.DefaultTimeout != 300*time.Second {
		t.Errorf("Tofu.DefaultTimeout = %v, want 5m", cfg.Tofu.DefaultTimeout)
	}
	if cfg.Tofu.KillGrace != 10*time.Second {
		t.Errorf("Tofu.KillGrace = %v, want 10s", cfg.Tofu.KillGrace)
	}
	if cfg.MCP.Transport != "stdio" {
		t.Errorf("MCP.Transport = %q, want stdio", cfg.MCP.Transport)
	}
	if cfg.Git.AuthorName != "forgeops-agent" {
		t.Errorf("Git.AuthorName = %q, want forgeops-agent", cfg.Git.AuthorName)
	}
	if cfg.Git.BranchPrefix != "forgeops/" {
		t.Errorf("Git.BranchPrefix = %q, want forgeops/", cfg.Git.BranchPrefix)
	}
}

func TestLoad_CombinedFailures(t *testing.T) {
	_, err := Load(makeGetenv(map[string]string{
		"LOG_FORMAT":                     "xml",
		"AGENT_MCP_TRANSPORT":            "grpc",
		"TOFU_TIMEOUT_SECONDS":           "not-a-number",
		"AGENT_SHUTDOWN_TIMEOUT_SECONDS": "abc",
	}))
	if err == nil {
		t.Fatal("expected error for combined failures")
	}
	msg := err.Error()
	if !strings.Contains(msg, "LOG_FORMAT") {
		t.Errorf("error should mention LOG_FORMAT: %s", msg)
	}
	if !strings.Contains(msg, "AGENT_MCP_TRANSPORT") {
		t.Errorf("error should mention AGENT_MCP_TRANSPORT: %s", msg)
	}
	if !strings.Contains(msg, "TOFU_TIMEOUT_SECONDS") {
		t.Errorf("error should mention TOFU_TIMEOUT_SECONDS: %s", msg)
	}
	if !strings.Contains(msg, "AGENT_SHUTDOWN_TIMEOUT_SECONDS") {
		t.Errorf("error should mention AGENT_SHUTDOWN_TIMEOUT_SECONDS: %s", msg)
	}
}

func TestLoad_InvalidURL(t *testing.T) {
	_, err := Load(makeGetenv(map[string]string{
		"GITHUB_API_BASE_URL": "not-a-valid-url://[invalid",
	}))
	if err == nil {
		t.Fatal("expected error for invalid URL")
	}
	if !strings.Contains(err.Error(), "GITHUB_API_BASE_URL") {
		t.Errorf("error should mention GITHUB_API_BASE_URL: %s", err.Error())
	}
}

func TestLoad_InvalidEnum(t *testing.T) {
	_, err := Load(makeGetenv(map[string]string{
		"LOG_FORMAT": "yaml",
	}))
	if err == nil {
		t.Fatal("expected error for invalid enum")
	}
	if !strings.Contains(err.Error(), "LOG_FORMAT") {
		t.Errorf("error should mention LOG_FORMAT: %s", err.Error())
	}
}

func TestLoad_UnrelatedEnvVarsIgnored(t *testing.T) {
	// Even with tons of unrelated env vars, Load should succeed
	env := map[string]string{
		"PATH":                    "/usr/bin:/bin",
		"HOME":                    "/home/user",
		"SHELL":                   "/bin/bash",
		"EDITOR":                  "vim",
		"CI":                      "true",
		"GITHUB_ACTIONS":          "true",
		"JAVA_HOME":               "/usr/lib/jvm/java-11",
		"NODE_ENV":                "production",
		"PYTHONPATH":              "/some/path",
		"AWS_SECRET_ACCESS_KEY":   "secret123",
		"OPENAI_API_KEY":          "sk-test",
		"RANDOM_UNRELATED_VAR_XY": "some-value",
	}
	cfg, err := Load(makeGetenv(env))
	if err != nil {
		t.Fatalf("unrelated env vars should not cause failure: %v", err)
	}
	// Verify defaults still apply
	if cfg.LogLevel != "INFO" {
		t.Errorf("LogLevel = %q, want INFO", cfg.LogLevel)
	}
}

func TestLoad_NeverReturnsPartialConfig(t *testing.T) {
	cfg, err := Load(makeGetenv(map[string]string{
		"LOG_FORMAT": "invalid",
	}))
	if err == nil {
		t.Fatal("expected error")
	}
	if cfg != nil {
		t.Fatal("on error, config must be nil (never partial)")
	}
}

func TestLoad_ValidCustomValues(t *testing.T) {
	cfg, err := Load(makeGetenv(map[string]string{
		"LOG_LEVEL":                      "DEBUG",
		"LOG_FORMAT":                     "json",
		"AGENT_BACKEND_WSS_URL":          "wss://backend.example.com/ws",
		"AGENT_SHUTDOWN_TIMEOUT_SECONDS": "30",
		"TOFU_BINARY":                    "/usr/local/bin/tofu",
		"TOFU_TIMEOUT_SECONDS":           "600",
		"TOFU_KILL_GRACE_SECONDS":        "20",
		"GITHUB_TOKEN":                   "ghp_test",
		"GITHUB_REPO":                    "owner/repo",
		"AGENT_MCP_TRANSPORT":            "http",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.LogLevel != "DEBUG" {
		t.Errorf("LogLevel = %q, want DEBUG", cfg.LogLevel)
	}
	if cfg.LogFormat != "json" {
		t.Errorf("LogFormat = %q, want json", cfg.LogFormat)
	}
	if cfg.BackendWSSURL != "wss://backend.example.com/ws" {
		t.Errorf("BackendWSSURL = %q", cfg.BackendWSSURL)
	}
	if cfg.ShutdownTimeout != 30*time.Second {
		t.Errorf("ShutdownTimeout = %v, want 30s", cfg.ShutdownTimeout)
	}
	if cfg.Tofu.DefaultTimeout != 600*time.Second {
		t.Errorf("Tofu.DefaultTimeout = %v, want 10m", cfg.Tofu.DefaultTimeout)
	}
	if cfg.Git.Token != "ghp_test" {
		t.Errorf("Git.Token = %q, want ghp_test", cfg.Git.Token)
	}
	if cfg.MCP.Transport != "http" {
		t.Errorf("MCP.Transport = %q, want http", cfg.MCP.Transport)
	}
}
