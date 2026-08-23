// SPDX-License-Identifier: Apache-2.0

package config

import (
	"strings"
	"testing"
	"time"
)

// The Phase 1 configuration surface (design.md §7.1, §13.1).
//
// Phase 1 adds fields; it changes no mechanism. So these tests cover both: the new
// defaults and bounds, AND that the Phase 0 contracts still hold over a much larger
// surface — one joined error containing EVERY problem (P-15), and unrelated ambient keys
// ignored. The second matters more as the surface grows: with five new field groups, a
// loader that started reading arbitrary environment keys would be hard to notice.

// env builds a getenv function from a map, so a test states exactly what is set and
// nothing else is. Using os.Getenv would make the ambient environment part of the test.
func env(pairs map[string]string) func(string) string {
	return func(key string) string { return pairs[key] }
}

func TestLoad_Phase1Defaults(t *testing.T) {
	t.Parallel()

	cfg, err := Load(env(nil))
	if err != nil {
		t.Fatalf("Load with an empty environment must succeed on defaults: %v", err)
	}

	cases := []struct {
		name string
		got  any
		want any
	}{
		{"Session.CredentialStore", cfg.Session.CredentialStore, "auto"},
		{"Session.HeartbeatInterval", cfg.Session.HeartbeatInterval, 30 * time.Second},
		{"Session.HeartbeatTimeout", cfg.Session.HeartbeatTimeout, 90 * time.Second},
		{"Session.EnvelopeClockSkew", cfg.Session.EnvelopeClockSkew, 60 * time.Second},
		// §7.6's 300s. It was in `.env.example` and read by nobody, so the agent used
		// `NewVerifier`'s hardcoded default and the setting did nothing.
		{"Session.EnvelopeMaxAge", cfg.Session.EnvelopeMaxAge, 300 * time.Second},
		// Empty means "the process working directory, resolved at use time", like StateDir.
		{"Executor.WorkspaceRoot", cfg.Executor.WorkspaceRoot, ""},
		{"Journal.MaxBytes", cfg.Journal.MaxBytes, int64(67108864)},
		{"Journal.MaxAge", cfg.Journal.MaxAge, 168 * time.Hour},
		{"Journal.DrainBatch", cfg.Journal.DrainBatch, 64},
		{"Identity.Provider", cfg.Identity.Provider, "paired_device"},
		{"Identity.CertRenewBefore", cfg.Identity.CertRenewBefore, 6 * time.Hour},
		{"Scanner.MaxFileSize", cfg.Scanner.MaxFileSize, int64(1048576)},
		{"Scanner.WatchDebounce", cfg.Scanner.WatchDebounce, 250 * time.Millisecond},
		{"Scanner.ParserConcurrency", cfg.Scanner.ParserConcurrency, 0},
		{"Scanner.ChunkTarget", cfg.Scanner.ChunkTarget, 512},
		{"Scanner.ChunkOverlap", cfg.Scanner.ChunkOverlap, 128},
		{"Scanner.SummaryTarget", cfg.Scanner.SummaryTarget, 1024},
		{"Validator.TrivyBinary", cfg.Validator.TrivyBinary, "trivy"},
		{"Validator.Timeout", cfg.Validator.Timeout, 120 * time.Second},
	}
	for _, c := range cases {
		if c.got != c.want {
			t.Errorf("%s = %v, want %v", c.name, c.got, c.want)
		}
	}
}

func TestLoad_Phase1UnitsAreNotDurationStrings(t *testing.T) {
	t.Parallel()

	// The variables are named `..._HOURS` and `..._MS`. Accepting a Go duration string
	// would let `AGENT_JOURNAL_MAX_AGE_HOURS=30s` load as thirty seconds — 20000x
	// smaller than intended, with no complaint. That is the whole reason
	// parseHoursDefault and parseMillisDefault exist instead of reusing
	// parseDurationDefault.
	cfg, err := Load(env(map[string]string{
		"AGENT_JOURNAL_MAX_AGE_HOURS":    "2",
		"SCAN_WATCH_DEBOUNCE_MS":         "500",
		"DEVICE_CERT_RENEW_BEFORE_HOURS": "12",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Journal.MaxAge != 2*time.Hour {
		t.Errorf("MaxAge = %v, want 2h", cfg.Journal.MaxAge)
	}
	if cfg.Scanner.WatchDebounce != 500*time.Millisecond {
		t.Errorf("WatchDebounce = %v, want 500ms", cfg.Scanner.WatchDebounce)
	}
	if cfg.Identity.CertRenewBefore != 12*time.Hour {
		t.Errorf("CertRenewBefore = %v, want 12h", cfg.Identity.CertRenewBefore)
	}

	if _, err := Load(env(map[string]string{"AGENT_JOURNAL_MAX_AGE_HOURS": "30s"})); err == nil {
		t.Fatal("a duration string in an hours-denominated variable must be refused")
	}
}

func TestLoad_Phase1InvalidValues(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name    string
		environ map[string]string
		wantIn  string
	}{
		{
			name:    "unknown credential store",
			environ: map[string]string{"AGENT_CREDENTIAL_STORE": "vault"},
			wantIn:  "AGENT_CREDENTIAL_STORE",
		},
		{
			name:    "unknown identity provider",
			environ: map[string]string{"AGENT_IDENTITY_PROVIDER": "kerberos"},
			wantIn:  "AGENT_IDENTITY_PROVIDER",
		},
		{
			name:    "spiffe provider without a socket",
			environ: map[string]string{"AGENT_IDENTITY_PROVIDER": "spiffe_workload"},
			wantIn:  "SPIFFE_ENDPOINT_SOCKET is required",
		},
		{
			name: "heartbeat timeout at the interval",
			environ: map[string]string{
				"HEARTBEAT_INTERVAL_SECONDS": "30",
				"HEARTBEAT_TIMEOUT_SECONDS":  "30",
			},
			wantIn: "must exceed HEARTBEAT_INTERVAL_SECONDS",
		},
		{
			name: "chunk overlap at the target",
			environ: map[string]string{
				"CHUNK_TARGET_TOKENS":  "512",
				"CHUNK_OVERLAP_TOKENS": "512",
			},
			wantIn: "must be smaller than CHUNK_TARGET_TOKENS",
		},
		{
			name:    "non-integer journal bound",
			environ: map[string]string{"AGENT_JOURNAL_MAX_BYTES": "lots"},
			wantIn:  "AGENT_JOURNAL_MAX_BYTES must be an integer",
		},
		{
			name:    "negative journal bound",
			environ: map[string]string{"AGENT_JOURNAL_MAX_BYTES": "-1"},
			wantIn:  "AGENT_JOURNAL_MAX_BYTES must be >= 0",
		},
		{
			name:    "drain batch below its floor",
			environ: map[string]string{"AGENT_JOURNAL_DRAIN_BATCH": "0"},
			wantIn:  "AGENT_JOURNAL_DRAIN_BATCH must be >= 1",
		},
		{
			name:    "scan file size below its floor",
			environ: map[string]string{"SCAN_MAX_FILE_SIZE_BYTES": "10"},
			wantIn:  "SCAN_MAX_FILE_SIZE_BYTES must be >= 1024",
		},
		{
			name:    "chunk target below its floor",
			environ: map[string]string{"CHUNK_TARGET_TOKENS": "1"},
			wantIn:  "CHUNK_TARGET_TOKENS must be >= 128",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			_, err := Load(env(c.environ))
			if err == nil {
				t.Fatalf("expected an error mentioning %q", c.wantIn)
			}
			if !strings.Contains(err.Error(), c.wantIn) {
				t.Errorf("error %q does not mention %q", err, c.wantIn)
			}
		})
	}
}

func TestLoad_Phase1ReportsEveryProblemTogether(t *testing.T) {
	t.Parallel()

	// P-15's contract over the enlarged surface. An operator bringing up an agent
	// should not need one restart per typo, so a single Load must name all of them.
	_, err := Load(env(map[string]string{
		"AGENT_CREDENTIAL_STORE":     "vault",
		"AGENT_IDENTITY_PROVIDER":    "kerberos",
		"AGENT_JOURNAL_MAX_BYTES":    "lots",
		"AGENT_JOURNAL_DRAIN_BATCH":  "0",
		"CHUNK_TARGET_TOKENS":        "512",
		"CHUNK_OVERLAP_TOKENS":       "600",
		"HEARTBEAT_INTERVAL_SECONDS": "90",
		"HEARTBEAT_TIMEOUT_SECONDS":  "30",
		"SCAN_MAX_FILE_SIZE_BYTES":   "10",
	}))
	if err != nil {
		// Not fatal yet: the assertions below say WHICH problems must appear.
		t.Logf("joined error: %v", err)
	} else {
		t.Fatal("expected a joined error")
	}

	for _, want := range []string{
		"AGENT_CREDENTIAL_STORE",
		"AGENT_IDENTITY_PROVIDER",
		"AGENT_JOURNAL_MAX_BYTES",
		"AGENT_JOURNAL_DRAIN_BATCH",
		"CHUNK_OVERLAP_TOKENS",
		"HEARTBEAT_TIMEOUT_SECONDS",
		"SCAN_MAX_FILE_SIZE_BYTES",
	} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the joined error omits %s: %v", want, err)
		}
	}
}

func TestLoad_Phase1IgnoresUnrelatedAmbientKeys(t *testing.T) {
	t.Parallel()

	// With five new field groups, a loader that started consulting arbitrary keys
	// would be very hard to spot. The getenv function here returns a value for EVERY
	// key, including ones the agent must not read; the load must still produce
	// defaults for the Phase 1 fields.
	everything := func(key string) string {
		switch key {
		// Keys the agent legitimately reads are left empty so defaults apply.
		case "LOG_LEVEL", "LOG_FORMAT", "AGENT_BACKEND_WSS_URL", "AGENT_SHUTDOWN_TIMEOUT_SECONDS",
			"TOFU_BINARY", "TOFU_TIMEOUT_SECONDS", "TOFU_KILL_GRACE_SECONDS", "TF_PLUGIN_CACHE_DIR",
			"TOFU_EXTRA_ENV_ALLOW", "GITHUB_TOKEN", "GITHUB_API_BASE_URL", "GITHUB_REPO",
			"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_BRANCH_PREFIX",
			"GIT_PR_POLL_INTERVAL_SECONDS", "GIT_PR_POLL_TIMEOUT_SECONDS", "AGENT_MCP_TRANSPORT",
			"AGENT_STATE_DIR", "AGENT_CREDENTIAL_STORE", "HEARTBEAT_INTERVAL_SECONDS",
			"HEARTBEAT_TIMEOUT_SECONDS", "ENVELOPE_CLOCK_SKEW_SECONDS", "ENVELOPE_MAX_AGE_SECONDS",
			"AGENT_WORKSPACE_ROOT", "AGENT_JOURNAL_MAX_BYTES",
			"AGENT_JOURNAL_MAX_AGE_HOURS", "AGENT_JOURNAL_DRAIN_BATCH", "AGENT_IDENTITY_PROVIDER",
			"SPIFFE_ENDPOINT_SOCKET", "DEVICE_CERT_RENEW_BEFORE_HOURS", "SCAN_MAX_FILE_SIZE_BYTES",
			"SCAN_WATCH_DEBOUNCE_MS", "SCAN_PARSER_CONCURRENCY", "CHUNK_TARGET_TOKENS",
			"CHUNK_OVERLAP_TOKENS", "SUMMARY_TARGET_TOKENS", "AGENT_TRIVY_BINARY",
			"AGENT_VALIDATOR_TIMEOUT_SECONDS":
			return ""
		default:
			// Anything else — PATH, HOME, CI, an unrelated OIDC_CLIENT_SECRET — would
			// break the load if it were consulted.
			return "this-value-must-never-be-read"
		}
	}

	cfg, err := Load(everything)
	if err != nil {
		t.Fatalf("an ambient environment full of unrelated keys must not break Load: %v", err)
	}
	if cfg.Journal.DrainBatch != 64 || cfg.Scanner.ChunkTarget != 512 {
		t.Errorf("defaults were not applied: %+v", cfg)
	}
}

func TestLoad_Phase1AcceptsValidNonDefaults(t *testing.T) {
	t.Parallel()

	cfg, err := Load(env(map[string]string{
		"AGENT_STATE_DIR":                 "/var/lib/forgeops",
		"AGENT_CREDENTIAL_STORE":          "file",
		"AGENT_IDENTITY_PROVIDER":         "spiffe_workload",
		"SPIFFE_ENDPOINT_SOCKET":          "unix:///run/spire/sockets/agent.sock",
		"AGENT_JOURNAL_MAX_BYTES":         "0",
		"SCAN_PARSER_CONCURRENCY":         "4",
		"AGENT_TRIVY_BINARY":              "/usr/local/bin/trivy",
		"AGENT_VALIDATOR_TIMEOUT_SECONDS": "300",
	}))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Session.StateDir != "/var/lib/forgeops" {
		t.Errorf("StateDir = %q", cfg.Session.StateDir)
	}
	if cfg.Identity.Provider != "spiffe_workload" || cfg.Identity.SPIFFEEndpointSocket == "" {
		t.Errorf("identity = %+v", cfg.Identity)
	}
	// 0 disables the journal entirely, and that is a supported configuration: a
	// deployment that would rather fail fast than queue can say so.
	if cfg.Journal.MaxBytes != 0 {
		t.Errorf("MaxBytes = %d, want 0 (journal disabled)", cfg.Journal.MaxBytes)
	}
	if cfg.Scanner.ParserConcurrency != 4 {
		t.Errorf("ParserConcurrency = %d", cfg.Scanner.ParserConcurrency)
	}
	if cfg.Validator.Timeout != 300*time.Second {
		t.Errorf("Validator.Timeout = %v", cfg.Validator.Timeout)
	}
}
