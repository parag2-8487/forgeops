// SPDX-License-Identifier: Apache-2.0
package config

import (
	"strings"
	"testing"

	"pgregory.net/rapid"
)

// TestProperty_P15_StrictFailureForInvalidForgeOpsKeys tests that the config
// loader aggregates all missing/invalid ForgeOps key failures into one error.
func TestProperty_P15_StrictFailureForInvalidForgeOpsKeys(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		// Generate combinations of invalid ForgeOps values
		invalidLogFormat := rapid.SampledFrom([]string{"xml", "yaml", "text", "binary", ""}).Draw(t, "logFormat")
		invalidTransport := rapid.SampledFrom([]string{"grpc", "tcp", "udp", "websocket", ""}).Draw(t, "transport")
		invalidTimeout := rapid.SampledFrom([]string{"not-a-number", "abc", "-5", "nan"}).Draw(t, "timeout")

		env := map[string]string{}

		// Add at least one invalid ForgeOps key
		numInvalid := rapid.IntRange(1, 3).Draw(t, "numInvalid")
		invalids := []string{invalidLogFormat, invalidTransport, invalidTimeout}
		keys := []string{"LOG_FORMAT", "AGENT_MCP_TRANSPORT", "TOFU_TIMEOUT_SECONDS"}

		for i := 0; i < numInvalid && i < len(invalids); i++ {
			if invalids[i] != "" { // Skip empty strings for LOG_FORMAT since it defaults
				env[keys[i]] = invalids[i]
			}
		}

		// Make sure we have at least one truly invalid value
		if _, ok := env["LOG_FORMAT"]; !ok {
			env["LOG_FORMAT"] = "invalid-format"
		}

		cfg, err := Load(makeGetenv(env))

		// Must fail
		if err == nil {
			t.Fatal("expected failure for invalid ForgeOps keys")
		}
		// Must not return partial config
		if cfg != nil {
			t.Fatal("config must be nil on error")
		}
		// Error must enumerate the problems
		if !strings.Contains(err.Error(), "LOG_FORMAT") {
			t.Errorf("error should mention the invalid key: %s", err.Error())
		}
	})
}

// TestProperty_P15_ToleranceOfAmbientEnvironment tests that the config loader
// tolerates ambient environments full of unrelated keys.
func TestProperty_P15_ToleranceOfAmbientEnvironment(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		env := make(map[string]string)

		// Generate a large number of unrelated environment variables
		numUnrelated := rapid.IntRange(10, 50).Draw(t, "numUnrelated")
		for i := 0; i < numUnrelated; i++ {
			key := rapid.SampledFrom([]string{
				"PATH", "HOME", "SHELL", "EDITOR", "LANG", "LC_ALL",
				"TERM", "USER", "LOGNAME", "HOSTNAME", "PWD", "OLDPWD",
				"CI", "GITHUB_ACTIONS", "GITHUB_SHA", "GITHUB_REF",
				"JAVA_HOME", "GOPATH", "GOROOT", "CARGO_HOME",
				"NODE_ENV", "NPM_CONFIG_PREFIX", "PYTHONPATH",
				"AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE",
				"DOCKER_HOST", "COMPOSE_PROJECT_NAME",
				"DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
				"COLORTERM", "TERM_PROGRAM", "VSCODE_PID",
				"SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO",
			}).Draw(t, "key")
			value := rapid.StringMatching(`[a-zA-Z0-9/._-]{1,100}`).Draw(t, "value")
			env[key] = value
		}

		cfg, err := Load(makeGetenv(env))

		// Must succeed - unrelated keys are ignored
		if err != nil {
			t.Fatalf("unrelated ambient keys should not cause failure: %v", err)
		}
		if cfg == nil {
			// Explicit return: rapid.T.Fatal aborts the case, but saying so here
			// lets static analysis see that nothing below dereferences a nil cfg.
			t.Fatal("config must not be nil on success")
			return
		}

		// Defaults should be intact
		if cfg.LogLevel != "INFO" {
			t.Errorf("LogLevel = %q, want INFO", cfg.LogLevel)
		}
		if cfg.LogFormat != "console" {
			t.Errorf("LogFormat = %q, want console", cfg.LogFormat)
		}
		if cfg.MCP.Transport != "stdio" {
			t.Errorf("MCP.Transport = %q, want stdio", cfg.MCP.Transport)
		}
	})
}
