// SPDX-License-Identifier: Apache-2.0
package iac

import (
	"os"
	"strings"
	"testing"
)

func TestBuildEnv_AllowlistOnly(t *testing.T) {
	// Set a known allowed key and a disallowed key
	os.Setenv("PATH", "/usr/bin")
	os.Setenv("SECRET_KEY", "should-not-appear")
	os.Setenv("AWS_SECRET_ACCESS_KEY", "should-not-appear")
	defer os.Unsetenv("SECRET_KEY")
	defer os.Unsetenv("AWS_SECRET_ACCESS_KEY")

	cfg := TofuConfig{
		PluginCacheDir: "/cache/plugins",
	}
	env := buildEnv(cfg, "/work")

	envMap := make(map[string]string)
	for _, e := range env {
		parts := strings.SplitN(e, "=", 2)
		if len(parts) == 2 {
			envMap[parts[0]] = parts[1]
		}
	}

	// PATH should be present (it's on the allowlist)
	if _, ok := envMap["PATH"]; !ok {
		t.Error("PATH should be in child environment")
	}

	// Secret keys should NOT be present
	if _, ok := envMap["SECRET_KEY"]; ok {
		t.Error("SECRET_KEY should not be in child environment")
	}
	if _, ok := envMap["AWS_SECRET_ACCESS_KEY"]; ok {
		t.Error("AWS_SECRET_ACCESS_KEY should not be in child environment")
	}

	// Fixed automation keys
	if v, ok := envMap["TF_IN_AUTOMATION"]; !ok || v != "1" {
		t.Error("TF_IN_AUTOMATION should be 1")
	}
	if v, ok := envMap["TF_INPUT"]; !ok || v != "0" {
		t.Error("TF_INPUT should be 0")
	}
	if v, ok := envMap["NO_COLOR"]; !ok || v != "1" {
		t.Error("NO_COLOR should be 1")
	}
	if v, ok := envMap["TF_CLI_ARGS"]; !ok || v != "" {
		t.Error("TF_CLI_ARGS should be empty")
	}
	if v, ok := envMap["TF_PLUGIN_CACHE_DIR"]; !ok || v != "/cache/plugins" {
		t.Error("TF_PLUGIN_CACHE_DIR should be /cache/plugins")
	}
}

func TestBuildEnv_ExtraAllowKeys(t *testing.T) {
	os.Setenv("MY_CUSTOM_VAR", "hello")
	defer os.Unsetenv("MY_CUSTOM_VAR")

	cfg := TofuConfig{
		ExtraEnvAllow: []string{"MY_CUSTOM_VAR"},
	}
	env := buildEnv(cfg, "/work")

	found := false
	for _, e := range env {
		if strings.HasPrefix(e, "MY_CUSTOM_VAR=") {
			found = true
			break
		}
	}
	if !found {
		t.Error("ExtraEnvAllow key should appear in child environment")
	}
}

func TestBuildEnv_TFDataDir(t *testing.T) {
	cfg := TofuConfig{}
	env := buildEnv(cfg, "/my/workdir")

	found := false
	for _, e := range env {
		if strings.Contains(e, "TF_DATA_DIR=") {
			found = true
			// Should contain workdir/.terraform
			if !strings.Contains(e, "/my/workdir") && !strings.Contains(e, "\\my\\workdir") {
				t.Errorf("TF_DATA_DIR doesn't contain workdir: %s", e)
			}
			break
		}
	}
	if !found {
		t.Error("TF_DATA_DIR should be set")
	}
}

func TestBuildEnv_UnrelatedVariablesExcluded(t *testing.T) {
	// Set various unrelated env vars
	vars := []string{"GITHUB_TOKEN", "OPENAI_API_KEY", "DATABASE_URL", "LLM_KEY_OPENAI"}
	for _, v := range vars {
		os.Setenv(v, "secret-value")
		defer os.Unsetenv(v)
	}

	cfg := TofuConfig{}
	env := buildEnv(cfg, "/work")

	for _, e := range env {
		for _, v := range vars {
			if strings.HasPrefix(e, v+"=") {
				t.Errorf("disallowed variable %s found in child env", v)
			}
		}
	}
}
