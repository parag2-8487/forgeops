// SPDX-License-Identifier: Apache-2.0
package iac

import (
	"os"
	"strings"
	"testing"

	"pgregory.net/rapid"
)

func TestProperty_P12_EnvIsolation(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		// Generate random parent environment variables
		numParentVars := rapid.IntRange(0, 20).Draw(rt, "numParentVars")
		parentVars := make(map[string]string)
		for i := 0; i < numParentVars; i++ {
			key := rapid.StringMatching(`[A-Z_][A-Z0-9_]{2,20}`).Draw(rt, "key")
			value := rapid.StringMatching(`[a-zA-Z0-9/._-]{1,50}`).Draw(rt, "value")
			parentVars[key] = value
		}

		// Generate extra allow keys
		numExtra := rapid.IntRange(0, 3).Draw(rt, "numExtra")
		extraAllow := make([]string, numExtra)
		for i := 0; i < numExtra; i++ {
			extraAllow[i] = rapid.StringMatching(`[A-Z_][A-Z0-9_]{2,10}`).Draw(rt, "extra")
		}

		// Set parent env.
		//
		// Restored to its PREVIOUS VALUE rather than unset, and the difference is not
		// theoretical here: the generator's own pattern `[A-Z_][A-Z0-9_]{2,20}` matches `PATH`,
		// `HOME` and `TMP`, so `defer os.Unsetenv(k)` could delete a variable the rest of the
		// test binary needs — probabilistically, on some seeds and not others. That is the same
		// defect `env_test.go` records, with a random trigger instead of a fixed one. `t.Setenv`
		// is not usable inside a rapid closure (its cleanup is scoped to the whole test, not to
		// one example, and it would accumulate one restore per generated variable per example),
		// so the snapshot is taken by hand and the restore is per-example.
		for k, v := range parentVars {
			previous, existed := os.LookupEnv(k)
			if err := os.Setenv(k, v); err != nil {
				rt.Fatalf("Setenv %s: %v", k, err)
			}
			defer func(name, value string, wasSet bool) {
				if wasSet {
					_ = os.Setenv(name, value)
					return
				}
				_ = os.Unsetenv(name)
			}(k, previous, existed)
		}

		cfg := TofuConfig{
			ExtraEnvAllow:  extraAllow,
			PluginCacheDir: "/cache",
		}

		env := buildEnv(cfg, "/work")

		// Build set of allowed keys
		allowed := make(map[string]bool)
		for _, k := range platformAllowlist {
			allowed[k] = true
		}
		for _, k := range extraAllow {
			allowed[k] = true
		}
		// Fixed keys
		fixedKeys := map[string]bool{
			"TF_IN_AUTOMATION":    true,
			"TF_INPUT":            true,
			"NO_COLOR":            true,
			"TF_CLI_ARGS":         true,
			"TF_PLUGIN_CACHE_DIR": true,
			"TF_DATA_DIR":         true,
		}

		// Verify P-12: child keys are SUBSET of (allowlist + fixed)
		for _, e := range env {
			parts := strings.SplitN(e, "=", 2)
			key := parts[0]
			if !allowed[key] && !fixedKeys[key] {
				t.Fatalf("child env contains disallowed key %q", key)
			}
		}

		// Verify disallowed parent keys don't appear
		envKeys := make(map[string]bool)
		for _, e := range env {
			parts := strings.SplitN(e, "=", 2)
			envKeys[parts[0]] = true
		}
		for k := range parentVars {
			if !allowed[k] && envKeys[k] {
				t.Fatalf("disallowed parent key %q leaked into child env", k)
			}
		}

		// Verify mandatory automation keys
		if !envKeys["TF_IN_AUTOMATION"] {
			t.Fatal("TF_IN_AUTOMATION missing")
		}
		if !envKeys["TF_INPUT"] {
			t.Fatal("TF_INPUT missing")
		}
	})
}
