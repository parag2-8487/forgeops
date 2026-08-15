// SPDX-License-Identifier: Apache-2.0
package iac

import (
	"os"
	"path/filepath"
)

// platformAllowlist contains environment variable keys that are always
// permitted through to the OpenTofu subprocess.
var platformAllowlist = []string{
	"PATH", "HOME", "USERPROFILE",
	"TMPDIR", "TEMP", "TMP",
	"SystemRoot", "SystemDrive", "ComSpec",
	"PATHEXT",           // Required on Windows for command/script extension resolution
	"RUNNER_TEMP",       // Required on GitHub Actions runners for tool wrappers (setup-opentofu)
	"RUNNER_TOOL_CACHE", // Required on GitHub Actions runners for tool wrappers (setup-opentofu)
}

// buildEnv returns the ONLY environment the tofu subprocess sees. Anything not
// on the allowlist is dropped, so provider credentials, LLM API keys, and CI
// secrets present in the agent's environment cannot leak into a plan, a log
// line, or a provider call (NFR-10, PRD §2.2 invariant 5).
func buildEnv(cfg TofuConfig, workdir string) []string {
	allow := make([]string, 0, len(platformAllowlist)+len(cfg.ExtraEnvAllow))
	allow = append(allow, platformAllowlist...)
	allow = append(allow, cfg.ExtraEnvAllow...)

	env := make([]string, 0, len(allow)+6)
	for _, k := range allow {
		if v, ok := os.LookupEnv(k); ok {
			env = append(env, k+"="+v)
		}
	}

	// Fixed automation keys that are always present
	env = append(env,
		"TF_IN_AUTOMATION=1",
		"TF_INPUT=0",
		"NO_COLOR=1",
		"TF_CLI_ARGS=",
	)

	if cfg.PluginCacheDir != "" {
		env = append(env, "TF_PLUGIN_CACHE_DIR="+cfg.PluginCacheDir)
	}

	env = append(env, "TF_DATA_DIR="+filepath.Join(workdir, ".terraform"))

	return env
}
