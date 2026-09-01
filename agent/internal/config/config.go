// SPDX-License-Identifier: Apache-2.0
package config

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config holds the fully-validated agent configuration.
type Config struct {
	LogLevel      string
	LogFormat     string // "json" | "console"
	BackendWSSURL string
	// BackendWSSURLSource says which of the three sources supplied BackendWSSURL, so a command
	// can log where it came from. A user who does not know why the agent is dialling a
	// particular host has no way to find out otherwise.
	BackendWSSURLSource BackendURLSource
	ShutdownTimeout     time.Duration
	Tofu                TofuConfig
	Git                 GitConfig
	MCP                 MCPConfig
	Session             SessionConfig
	Journal             JournalConfig
	Identity            IdentityConfig
	Executor            ExecutorConfig
	Scanner             ScannerConfig
	Validator           ValidatorConfig
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

// ─── Phase 1 additions (design §7.1, §13.1) ─────────────────────────────────
//
// Grouped structs rather than a flat widening of Config. The agent gains five
// subsystems in Phase 1, and a flat struct of forty fields makes it impossible to see
// at a glance which subsystem owns which knob — which matters when `agent doctor` has
// to report a degraded mode and name the setting that governs it.

// SessionConfig covers pairing, the connection handshake and heartbeats (§10.3).
type SessionConfig struct {
	// StateDir holds the credential file fallback and the outbound journal. Empty
	// means "resolve the OS default at use time" rather than a baked-in path: the
	// correct location differs per platform, and resolving it during Load would make
	// the config depend on the filesystem.
	StateDir string
	// CredentialStore is "auto" | "keychain" | "file". `auto` prefers the OS keychain
	// and falls back to a 0600 file, REPORTING the fallback rather than hiding it
	// (§10.10, OQ-26).
	CredentialStore   string
	HeartbeatInterval time.Duration
	HeartbeatTimeout  time.Duration
	// EnvelopeMaxAge is ENVELOPE_MAX_AGE_SECONDS (§7.6, default 300s): how far ahead of
	// now an envelope's `not_after` may sit.
	//
	// It was in `.env.example` and read by nobody, so the agent verified against
	// `NewVerifier`'s hardcoded 300s default and an operator who changed the setting saw
	// no effect. It also sizes the replay guard's age window, which is why the two must
	// come from one value: a nonce set that forgets sooner than an envelope stays fresh
	// would narrow uniqueness to "unique among the recent", which is not what §7.6 says.
	EnvelopeMaxAge time.Duration
	// EnvelopeClockSkew is the tolerated clock difference when checking an envelope's
	// freshness (§10.4). Separate from the max age because they fail differently: a
	// stale envelope is a slow backend, a skewed one is a wrong clock, and `doctor`
	// reports the measured skew so the second is diagnosable.
	EnvelopeClockSkew time.Duration
}

// ExecutorConfig bounds where named operations may write (§10.5, §7.7).
type ExecutorConfig struct {
	// WorkspaceRoot is the directory every path in a change set is resolved against and
	// confined to.
	//
	// Configuration and NOT a member of the envelope, which is the whole point: a root
	// carried in the signed arguments would let a command relocate the write boundary,
	// and a signature proves who sent something rather than that where it points is safe.
	// `executor`'s `applyArgs` deliberately has no `Root` field for the same reason.
	//
	// Empty means "resolve the process working directory at use time", following
	// StateDir's precedent above: resolving it during Load would make configuration
	// depend on the filesystem, and the container sets it explicitly anyway.
	WorkspaceRoot string
}

// JournalConfig bounds the offline outbound queue (§10.3, D-41, NFR-18).
type JournalConfig struct {
	// MaxBytes of 0 disables the journal entirely, which is a supported configuration:
	// a deployment that would rather fail fast than queue can say so.
	MaxBytes   int64
	MaxAge     time.Duration
	DrainBatch int
}

// IdentityConfig selects which identity provider dials the backend (§10.2, D-36).
type IdentityConfig struct {
	// Provider is "paired_device" | "spiffe_workload".
	Provider string
	// SPIFFEEndpointSocket is only meaningful for spiffe_workload. Validated as
	// required in that mode, because a SPIFFE provider with no socket cannot obtain an
	// SVID and would fail at first dial instead of at startup.
	SPIFFEEndpointSocket string
	CertRenewBefore      time.Duration
}

// ScannerConfig bounds traversal, parsing and chunking (§10.8).
type ScannerConfig struct {
	MaxFileSize   int64
	WatchDebounce time.Duration
	// ParserConcurrency of 0 means min(GOMAXPROCS, 8), resolved by the scanner rather
	// than here so the value in the config stays the value the operator wrote.
	ParserConcurrency int
	ChunkTarget       int
	ChunkOverlap      int
	SummaryTarget     int
}

// ValidatorConfig covers the external tools §10.7's validators may invoke.
type ValidatorConfig struct {
	TrivyBinary string
	Timeout     time.Duration
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

	// Discovery, not a guess. The environment variable still wins over anything found on disk,
	// and a value found on disk is accepted only when it is loopback — see
	// `backend_discovery.go` for why that distinction is the whole safety argument.
	//
	// The working directory is the search root. That is deliberate: a developer running the agent
	// from the repository that started the stack gets the right answer with no flag at all, and a
	// user anywhere else gets the refusal plus a message naming every source.
	workingDir, _ := os.Getwd()
	backendWSSURL, backendWSSURLSource, backendErr := DiscoverBackendURL(
		"", getenv("AGENT_BACKEND_WSS_URL"), workingDir)
	if backendErr != nil {
		errs = append(errs, backendErr.Error())
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

	// ─── Phase 1 fields (§13.1) ─────────────────────────────────────────────
	// Every problem below appends to `errs` rather than returning, so one Load
	// reports every misconfiguration together (P-15). An operator bringing up an
	// agent should not need one restart per typo.

	stateDir := getenvDefault(getenv, "AGENT_STATE_DIR", "")
	credentialStore := getenvDefault(getenv, "AGENT_CREDENTIAL_STORE", "auto")
	if !oneOf(credentialStore, "auto", "keychain", "file") {
		errs = append(errs, fmt.Sprintf(
			"AGENT_CREDENTIAL_STORE must be auto, keychain or file, got %q", credentialStore))
	}
	heartbeatInterval := parseDurationDefault(getenv, "HEARTBEAT_INTERVAL_SECONDS", "30", &errs)
	heartbeatTimeout := parseDurationDefault(getenv, "HEARTBEAT_TIMEOUT_SECONDS", "90", &errs)
	if heartbeatTimeout > 0 && heartbeatInterval > 0 && heartbeatTimeout <= heartbeatInterval {
		// A timeout at or below the interval declares every healthy agent dead: the
		// deadline expires before the next beat can possibly arrive.
		errs = append(errs, fmt.Sprintf(
			"HEARTBEAT_TIMEOUT_SECONDS (%s) must exceed HEARTBEAT_INTERVAL_SECONDS (%s)",
			heartbeatTimeout, heartbeatInterval))
	}
	envelopeClockSkew := parseDurationDefault(getenv, "ENVELOPE_CLOCK_SKEW_SECONDS", "60", &errs)
	envelopeMaxAge := parseDurationDefault(getenv, "ENVELOPE_MAX_AGE_SECONDS", "300", &errs)
	workspaceRoot := getenvDefault(getenv, "AGENT_WORKSPACE_ROOT", "")

	journalMaxBytes := parseInt64Default(getenv, "AGENT_JOURNAL_MAX_BYTES", 67108864, 0, &errs)
	journalMaxAge := parseHoursDefault(getenv, "AGENT_JOURNAL_MAX_AGE_HOURS", "168", &errs)
	journalDrainBatch := parseIntDefault(getenv, "AGENT_JOURNAL_DRAIN_BATCH", 64, 1, &errs)

	identityProvider := getenvDefault(getenv, "AGENT_IDENTITY_PROVIDER", "paired_device")
	if !oneOf(identityProvider, "paired_device", "spiffe_workload") {
		errs = append(errs, fmt.Sprintf(
			"AGENT_IDENTITY_PROVIDER must be paired_device or spiffe_workload, got %q", identityProvider))
	}
	spiffeSocket := getenvDefault(getenv, "SPIFFE_ENDPOINT_SOCKET", "")
	if identityProvider == "spiffe_workload" && strings.TrimSpace(spiffeSocket) == "" {
		// Caught here rather than at first dial: a SPIFFE provider with no socket
		// cannot obtain an SVID, and failing at startup names the missing setting
		// while failing at dial looks like a network problem.
		errs = append(errs, "SPIFFE_ENDPOINT_SOCKET is required when AGENT_IDENTITY_PROVIDER=spiffe_workload")
	}
	certRenewBefore := parseHoursDefault(getenv, "DEVICE_CERT_RENEW_BEFORE_HOURS", "6", &errs)

	scanMaxFileSize := parseInt64Default(getenv, "SCAN_MAX_FILE_SIZE_BYTES", 1048576, 1024, &errs)
	watchDebounce := parseMillisDefault(getenv, "SCAN_WATCH_DEBOUNCE_MS", "250", &errs)
	parserConcurrency := parseIntDefault(getenv, "SCAN_PARSER_CONCURRENCY", 0, 0, &errs)
	chunkTarget := parseIntDefault(getenv, "CHUNK_TARGET_TOKENS", 512, 128, &errs)
	chunkOverlap := parseIntDefault(getenv, "CHUNK_OVERLAP_TOKENS", 128, 0, &errs)
	if chunkTarget > 0 && chunkOverlap >= chunkTarget {
		// Overlap >= target is not a tuning choice, it is a non-terminating chunker:
		// every window re-emits its predecessor's whole content. The backend refuses
		// the same combination (§7.1), and both sides must agree or a project indexes
		// differently depending on which one chunked it.
		errs = append(errs, fmt.Sprintf(
			"CHUNK_OVERLAP_TOKENS (%d) must be smaller than CHUNK_TARGET_TOKENS (%d)",
			chunkOverlap, chunkTarget))
	}
	summaryTarget := parseIntDefault(getenv, "SUMMARY_TARGET_TOKENS", 1024, 256, &errs)

	trivyBinary := getenvDefault(getenv, "AGENT_TRIVY_BINARY", "trivy")
	validatorTimeout := parseDurationDefault(getenv, "AGENT_VALIDATOR_TIMEOUT_SECONDS", "120", &errs)

	if len(errs) > 0 {
		return nil, errors.New(strings.Join(errs, "; "))
	}

	return &Config{
		LogLevel:            logLevel,
		LogFormat:           logFormat,
		BackendWSSURL:       backendWSSURL,
		BackendWSSURLSource: backendWSSURLSource,
		ShutdownTimeout:     shutdownTimeout,
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
		Session: SessionConfig{
			StateDir:          stateDir,
			CredentialStore:   credentialStore,
			HeartbeatInterval: heartbeatInterval,
			HeartbeatTimeout:  heartbeatTimeout,
			EnvelopeMaxAge:    envelopeMaxAge,
			EnvelopeClockSkew: envelopeClockSkew,
		},
		Journal: JournalConfig{
			MaxBytes:   journalMaxBytes,
			MaxAge:     journalMaxAge,
			DrainBatch: journalDrainBatch,
		},
		Identity: IdentityConfig{
			Provider:             identityProvider,
			SPIFFEEndpointSocket: spiffeSocket,
			CertRenewBefore:      certRenewBefore,
		},
		Executor: ExecutorConfig{
			WorkspaceRoot: workspaceRoot,
		},
		Scanner: ScannerConfig{
			MaxFileSize:       scanMaxFileSize,
			WatchDebounce:     watchDebounce,
			ParserConcurrency: parserConcurrency,
			ChunkTarget:       chunkTarget,
			ChunkOverlap:      chunkOverlap,
			SummaryTarget:     summaryTarget,
		},
		Validator: ValidatorConfig{
			TrivyBinary: trivyBinary,
			Timeout:     validatorTimeout,
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

// ─── Phase 1 parsing helpers ────────────────────────────────────────────────
//
// Each follows the Phase 0 contract exactly: a blank value takes the default, a
// malformed value APPENDS to errs rather than returning, and a value below its floor is
// its own message. That is what keeps one Load reporting every problem together (P-15)
// instead of stopping at the first.

// oneOf reports whether v is one of the allowed values.
func oneOf(v string, allowed ...string) bool {
	for _, a := range allowed {
		if v == a {
			return true
		}
	}
	return false
}

func parseIntDefault(getenv func(string) string, key string, def, min int, errs *[]string) int {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		*errs = append(*errs, fmt.Sprintf("%s must be an integer, got %q", key, raw))
		return def
	}
	if n < min {
		*errs = append(*errs, fmt.Sprintf("%s must be >= %d, got %d", key, min, n))
		return def
	}
	return n
}

func parseInt64Default(getenv func(string) string, key string, def, min int64, errs *[]string) int64 {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		return def
	}
	n, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		*errs = append(*errs, fmt.Sprintf("%s must be an integer, got %q", key, raw))
		return def
	}
	if n < min {
		*errs = append(*errs, fmt.Sprintf("%s must be >= %d, got %d", key, min, n))
		return def
	}
	return n
}

// parseHoursDefault reads a whole number of HOURS. The env var is named `..._HOURS`, so
// accepting a Go duration string here would let `AGENT_JOURNAL_MAX_AGE_HOURS=30s` load
// as thirty seconds — a value 20000x smaller than the operator intended, silently.
func parseHoursDefault(getenv func(string) string, key, defHours string, errs *[]string) time.Duration {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		raw = defHours
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		*errs = append(*errs, fmt.Sprintf("%s must be a whole number of hours, got %q", key, raw))
		return 0
	}
	if n < 0 {
		*errs = append(*errs, fmt.Sprintf("%s must not be negative, got %d", key, n))
		return 0
	}
	return time.Duration(n) * time.Hour
}

// parseMillisDefault reads a whole number of MILLISECONDS, for the same reason
// parseHoursDefault exists: the unit is in the variable's name.
func parseMillisDefault(getenv func(string) string, key, defMillis string, errs *[]string) time.Duration {
	raw := strings.TrimSpace(getenv(key))
	if raw == "" {
		raw = defMillis
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		*errs = append(*errs, fmt.Sprintf("%s must be a whole number of milliseconds, got %q", key, raw))
		return 0
	}
	if n < 0 {
		*errs = append(*errs, fmt.Sprintf("%s must not be negative, got %d", key, n))
		return 0
	}
	return time.Duration(n) * time.Millisecond
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
