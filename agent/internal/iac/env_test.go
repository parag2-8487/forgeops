// SPDX-License-Identifier: Apache-2.0
package iac

import (
	"strings"
	"testing"
)

// WHY EVERY TEST IN THIS FILE USES `t.Setenv` AND NONE USES `os.Setenv`.
//
// `TestBuildEnv_AllowlistOnly` used to call `os.Setenv("PATH", "/usr/bin")` with no restore.
// That is a PROCESS-GLOBAL mutation inside one test binary, so every test that ran afterwards in
// this package saw `PATH=/usr/bin` — and on Windows the three `TestTerminateGroup_*` tests in
// `procattr_windows_test.go` then skipped with "powershell.exe is not available", because
// `exec.LookPath` searches the PATH it was handed. They passed when run alone with `-run` and
// skipped in the full suite, which is the worst possible shape: the skip looked like a platform
// limitation and was caused by a sibling test.
//
// `defer os.Unsetenv(...)` does not fix it either, and the other three tests here used exactly
// that. `Unsetenv` DELETES the variable. For a name that did not exist beforehand the two are the
// same; for one that did — `PATH` always does — restoring by deleting leaves the process in a
// state neither the test nor the caller asked for. The correct primitive is `t.Setenv`, which
// records the previous value (including "was absent") and restores it in a cleanup, and which
// panics if the test also calls `t.Parallel` — so the isolation it provides cannot be silently
// undermined by concurrency.

func TestBuildEnv_AllowlistOnly(t *testing.T) {
	// A sentinel rather than a plausible PATH: the assertion below compares the VALUE, so an
	// implementation that put the allowlisted name in the child environment with the wrong
	// contents would be caught. `t.Setenv` restores the real PATH when this test returns.
	const sentinelPath = "/forgeops-test-only-path-sentinel"
	t.Setenv("PATH", sentinelPath)
	t.Setenv("SECRET_KEY", "should-not-appear")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "should-not-appear")

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

	// PATH should be present (it's on the allowlist), carrying the value it was given.
	if got, ok := envMap["PATH"]; !ok {
		t.Error("PATH should be in child environment")
	} else if got != sentinelPath {
		t.Errorf("PATH = %q in the child environment, want the value the parent held, %q", got, sentinelPath)
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
	t.Setenv("MY_CUSTOM_VAR", "hello")

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
		t.Setenv(v, "secret-value")
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
