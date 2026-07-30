// SPDX-License-Identifier: Apache-2.0

package app

import (
	"strings"

	"github.com/parag8487/ForgeOps/agent/internal/config"
)

// knownSecrets returns every secret value the agent was CONFIGURED with, for the
// redacting logger to scrub before encoding (design §7.2, §14.5, Q-24).
//
// Scope, stated precisely
// -----------------------
// These are values the agent is given, so they are the ones it can name exactly. That
// makes the redaction here complete for its scope and cheap: an exact string match, no
// pattern matching, no false positives.
//
// It is NOT the whole of Q-24. A secret the agent was never told about — one sitting in
// a scanned file, or echoed back by an external validator — cannot be matched by value,
// and catching those is `internal/secretscan`'s job (task 10.1), which detects by
// pattern and entropy. The two layers are complementary: this one cannot miss a
// configured credential, and that one cannot miss an unconfigured one.
//
// Empty and trivially short values are dropped. A one- or two-character "secret" would
// turn the redactor into a mangler that rewrites ordinary log text, and a redactor whose
// output is unreadable gets switched off.
func knownSecrets(cfg *config.Config) []string {
	candidates := []string{
		cfg.Git.Token,
	}

	const minRedactableLength = 8

	seen := make(map[string]struct{}, len(candidates))
	secrets := make([]string, 0, len(candidates))
	for _, candidate := range candidates {
		value := strings.TrimSpace(candidate)
		if len(value) < minRedactableLength {
			continue
		}
		if _, duplicate := seen[value]; duplicate {
			continue
		}
		seen[value] = struct{}{}
		secrets = append(secrets, value)
	}
	return secrets
}
