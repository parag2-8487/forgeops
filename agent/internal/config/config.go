// SPDX-License-Identifier: Apache-2.0
package config

import (
	"errors"
	"fmt"
	"net/url"
	"strings"
	"time"
)

// Config holds the fully-validated agent configuration.
type Config struct {
	LogLevel        string
	LogFormat       string // "json" | "console"
	BackendWSSURL   string
	ShutdownTimeout time.Duration
	Tofu            TofuConfig
	Git             GitConfig
	MCP             MCPConfig
}

// TofuConfig holds OpenTofu runner settings.
type TofuConfig struct {
	BinaryPath     string
	DefaultTimeout time.Duration
	KillGrace      time.Duration
	PluginCacheDir string
	ExtraEnvAllow  []string
}

// GitConfig holds Git/PR client settings.
type GitConfig struct {
	Token        string
	APIBaseURL   string
	Repo         string
	AuthorName   string
	AuthorEmail  string
	BranchPrefix string
	PollInterval time.Duration
	PollTimeout  time.Duration
}

// MCPConfig holds MCP server settings.
type MCPConfig struct {
	Transport string // "stdio" | "http"
}

// Load reads the environment via getenv and returns a fully-validated Config, or
// an error that enumerates every problem found. It never returns a partially-populated
// Config alongside an error. It validates only ForgeOps keys it consumes and
// ignores unrelated ambient env vars.
func Load(getenv func(string) string) (*Config, error) {
	var errs []string

	logLevel := getenvDefault(getenv, "LOG_LEVEL", "INFO")
	logFormat := getenvDefault(getenv, "LOG_FORMAT", "console")
	if logFormat != "json" && logFormat != "console" {
		errs = append(errs, fmt.Sprintf("LOG_FORMAT: must be 'json' or 'console', got %q", logFormat))
	}

	backendWSSURL := getenv("AGENT_BACKEND_WSS_URL")
	if backendWSSURL != "" {
		if _, err := url.Parse(backendWSSURL); err != nil {
			errs = append(errs, fmt.Sprintf("AGENT_BACKEND_WSS_URL: invalid URL: %v", err))
		}
	}

	shutdownTimeout := parseDurationDefault(getenv, "AGENT_SHUTDOWN_TIMEOUT_SECONDS", "15", &errs)

	tofuBinary := getenvDefault(getenv, "TOFU_BINARY", "tofu")
	tofuTimeout := parseDurationDefault(getenv, "TOFU_TIMEOUT_SECONDS", "300", &errs)
	tofuKillGrace := parseDurationDefault(getenv, "TOFU_KILL_GRACE_SECONDS", "10", &errs)
	tofuPluginCache := getenvDefault(getenv, "TF_PLUGIN_CACHE_DIR", "")
	tofuExtraEnv := parseCSV(getenv("TOFU_EXTRA_ENV_ALLOW"))

	gitToken := getenv("GITHUB_TOKEN")
	gitAPIBase := getenvDefault(getenv, "GITHUB_API_BASE_URL", "https://api.github.com")
	if gitAPIBase != "" {
		if u, err := url.Parse(gitAPIBase); err != nil || (u.Scheme != "http" && u.Scheme != "https") {
			errs = append(errs, fmt.Sprintf("GITHUB_API_BASE_URL: invalid URL: %q", gitAPIBase))
		}
	}
	gitRepo := getenvDefault(getenv, "GITHUB_REPO", "")
	gitAuthorName := getenvDefault(getenv, "GIT_AUTHOR_NAME", "forgeops-agent")
	gitAuthorEmail := getenvDefault(getenv, "GIT_AUTHOR_EMAIL", "agent@forgeops.invalid")
	gitBranchPrefix := getenvDefault(getenv, "GIT_BRANCH_PREFIX", "forgeops/")
	gitPollInterval := parseDurationDefault(getenv, "GIT_PR_POLL_INTERVAL_SECONDS", "15", &errs)
	gitPollTimeout := parseDurationDefault(getenv, "GIT_PR_POLL_TIMEOUT_SECONDS", "900", &errs)

	mcpTransport := getenvDefault(getenv, "AGENT_MCP_TRANSPORT", "stdio")
	if mcpTransport != "stdio" && mcpTransport != "http" {
		errs = append(errs, fmt.Sprintf("AGENT_MCP_TRANSPORT: must be 'stdio' or 'http', got %q", mcpTransport))
	}

	if len(errs) > 0 {
		return nil, errors.New(strings.Join(errs, "; "))
	}

	return &Config{
		LogLevel:        logLevel,
		LogFormat:       logFormat,
		BackendWSSURL:   backendWSSURL,
		ShutdownTimeout: shutdownTimeout,
		Tofu: TofuConfig{
			BinaryPath:     tofuBinary,
			DefaultTimeout: tofuTimeout,
			KillGrace:      tofuKillGrace,
			PluginCacheDir: tofuPluginCache,
			ExtraEnvAllow:  tofuExtraEnv,
		},
		Git: GitConfig{
			Token:        gitToken,
			APIBaseURL:   gitAPIBase,
			Repo:         gitRepo,
			AuthorName:   gitAuthorName,
			AuthorEmail:  gitAuthorEmail,
			BranchPrefix: gitBranchPrefix,
			PollInterval: gitPollInterval,
			PollTimeout:  gitPollTimeout,
		},
		MCP: MCPConfig{
			Transport: mcpTransport,
		},
	}, nil
}

func getenvDefault(getenv func(string) string, key, def string) string {
	v := getenv(key)
	if v == "" {
		return def
	}
	return v
}

func parseDurationDefault(getenv func(string) string, key, defSeconds string, errs *[]string) time.Duration {
	raw := getenv(key)
	if raw == "" {
		raw = defSeconds
	}
	// Try parsing as seconds (integer or float)
	raw = strings.TrimSpace(raw)
	if raw == "" {
		raw = defSeconds
	}
	d, err := time.ParseDuration(raw + "s")
	if err != nil {
		*errs = append(*errs, fmt.Sprintf("%s: invalid duration value: %q", key, raw))
		return 0
	}
	if d < 0 {
		*errs = append(*errs, fmt.Sprintf("%s: must be non-negative, got %v", key, d))
		return 0
	}
	return d
}

func parseCSV(s string) []string {
	if s == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	result := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			result = append(result, p)
		}
	}
	return result
}
