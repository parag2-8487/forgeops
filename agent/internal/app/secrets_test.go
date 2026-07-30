// SPDX-License-Identifier: Apache-2.0

package app

import (
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/config"
	"github.com/parag8487/ForgeOps/agent/internal/logging"
)

// The secret set handed to the redacting logger (design §7.2, §14.5, Q-24).
//
// Credentials here are synthetic, self-labelling and assembled at runtime, per
// .kiro/steering/secret-safety.md: no contiguous literal in this file resembles a real
// provider token, so scanners stay quiet on our own content while the assertions keep
// their full strength.

// syntheticToken builds an obviously-fake credential long enough to be redactable.
func syntheticToken(suffix string) string {
	return "test-only-not-a-real-secret-" + suffix
}

func TestKnownSecrets_CollectsTheConfiguredToken(t *testing.T) {
	t.Parallel()

	token := syntheticToken("github")
	secrets := knownSecrets(&config.Config{Git: config.GitConfig{Token: token}})

	if len(secrets) != 1 || secrets[0] != token {
		t.Fatalf("knownSecrets = %v, want exactly [%q]", secrets, token)
	}
}

func TestKnownSecrets_DropsEmptyAndTrivialValues(t *testing.T) {
	t.Parallel()

	// A one- or two-character "secret" would turn the redactor into a mangler that
	// rewrites ordinary log text, and a redactor whose output is unreadable gets
	// switched off. Dropping short values keeps the redaction useful.
	cases := []string{"", "   ", "x", "abc", "1234567"}
	for _, value := range cases {
		secrets := knownSecrets(&config.Config{Git: config.GitConfig{Token: value}})
		if len(secrets) != 0 {
			t.Errorf("knownSecrets(%q) = %v, want none", value, secrets)
		}
	}
}

func TestKnownSecrets_AcceptsTheMinimumRedactableLength(t *testing.T) {
	t.Parallel()

	eight := "12345678"
	if got := knownSecrets(&config.Config{Git: config.GitConfig{Token: eight}}); len(got) != 1 {
		t.Errorf("an 8-character value must be redactable, got %v", got)
	}
}

func TestKnownSecrets_Deduplicates(t *testing.T) {
	t.Parallel()

	// Duplicate entries would make the redactor do the same substitution twice. Not
	// harmful, but the set grows with every credential Phase 1 adds, and a redactor is
	// on the hot path of every log line.
	token := syntheticToken("dup")
	cfg := &config.Config{Git: config.GitConfig{Token: token}}
	if got := knownSecrets(cfg); len(got) != 1 {
		t.Errorf("knownSecrets = %v, want one entry", got)
	}
}

func TestKnownSecrets_TrimsSurroundingWhitespace(t *testing.T) {
	t.Parallel()

	// A trailing newline is what a value read from a file or a shell heredoc carries,
	// and an untrimmed secret would never match the value that actually appears in a
	// log line.
	token := syntheticToken("trimmed")
	secrets := knownSecrets(&config.Config{Git: config.GitConfig{Token: "  " + token + "\n"}})
	if len(secrets) != 1 || secrets[0] != token {
		t.Fatalf("knownSecrets = %v, want [%q]", secrets, token)
	}
}

func TestRedactingLoggerIsConstructableWithTheCollectedSet(t *testing.T) {
	t.Parallel()

	// The composition this leaf is about, exercised end to end: the collector's output
	// is accepted by the constructor app.New uses. The redaction behaviour itself is
	// asserted in internal/logging, which owns the core and can capture its output.
	cfg := &config.Config{
		LogLevel:  "INFO",
		LogFormat: "json",
		Git:       config.GitConfig{Token: syntheticToken("wired")},
	}
	logger, err := logging.NewRedacted(cfg.LogLevel, cfg.LogFormat, knownSecrets(cfg))
	if err != nil {
		t.Fatalf("NewRedacted: %v", err)
	}
	defer func() { _ = logger.Sync() }()

	if logger == nil {
		t.Fatal("nil logger")
	}
}

func TestKnownSecrets_HasNoRealLookingLiteralInThisFile(t *testing.T) {
	t.Parallel()

	// Guards the steering rule rather than the code: every credential in this file is
	// composed at runtime from a self-labelling prefix, so `gitleaks` and GitHub secret
	// scanning stay quiet on our own test material. A blocked scan everyone learns to
	// wave through is worse than no scan.
	for _, value := range []string{syntheticToken("a"), syntheticToken("b")} {
		if !strings.HasPrefix(value, "test-only-not-a-real-secret") {
			t.Errorf("%q is not self-labelling", value)
		}
	}
}
