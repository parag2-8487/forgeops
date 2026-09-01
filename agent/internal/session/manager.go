// SPDX-License-Identifier: Apache-2.0

// This file is the session manager's pairing half (design §3.1, §10.3, §10.10,
// Appendix A.1). `Serve`, the heartbeat and the reconnect loop arrive with leaf 8.5.
package session

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"runtime"
	"strings"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/connection"
	"github.com/parag8487/ForgeOps/agent/internal/identity"
)

// exchangePath is the one unauthenticated backend route (§4.4, §3.1).
const exchangePath = "/api/v1/agents/pair/exchange"

// abandonPath is the self-surrender route, used only when a credential was issued and could
// not be persisted (§3.7's `abandoned` state).
const abandonPath = "/api/v1/agents/self/abandon"

// abandonTimeout bounds the surrender attempt. The user is already waiting on a failure
// whose outcome is decided; this is worth a few seconds and must not be worth more.
const abandonTimeout = 10 * time.Second

// pairingCSRCommonName is the placeholder subject the agent puts in its CSR.
//
// §3.1's flow builds the CSR before the device id exists — the backend assigns it — so
// there is nothing truthful to put here. D-73 settles the consequence: the CA
// **discards** the CSR's subject and issues `CN=<device_id>` itself, because a CSR
// arrives on an unauthenticated route and a CA that copied caller-supplied data into an
// identity field would let the caller choose who it is. The value is therefore a
// self-describing placeholder rather than a guess at an identity, so anybody reading a
// captured CSR can see it is not meant to be trusted.
const pairingCSRCommonName = "forgeops-agent-pairing-request"

// credentialByteLength is the size of both the device token and the envelope key (§3.1).
//
// Checked on receipt rather than assumed: the envelope key is the HMAC-SHA256 key for
// every command envelope, and a short key would verify signatures happily while being
// weaker than the design says. A wrong length is a backend bug or a tampered response,
// and both must stop pairing rather than be persisted.
const credentialByteLength = 32

// defaultExchangeTimeout bounds the one HTTP request pairing makes.
//
// Generous, because the backend generates a key, seals it and signs a certificate inside
// this call, and a user watching a 5-minute code countdown would rather wait than retry
// and burn an attempt against the 5-attempt cap.
const defaultExchangeTimeout = 30 * time.Second

var (
	// ErrUnpaired is returned when a backend URL is configured but no device token
	// exists. Deliberately distinct from Phase 0's connection.ErrDisabled (no URL at
	// all) so `agent doctor` can tell a user which of the two situations they are in:
	// one is fixed by setting AGENT_BACKEND_WSS_URL, the other by running `pair`.
	ErrUnpaired = errors.New("session: no device token; run `forgeops-agent pair`")

	// ErrRevoked is returned when the backend reports this device revoked. The caller
	// aborts in-flight work, rolls back from the backup manifest, and wipes credentials.
	ErrRevoked = errors.New("session: device revoked")

	// ErrPairingCodeInvalid is the exchange's 401. It covers unknown, expired, burned
	// and already-consumed codes as one outcome, because the backend deliberately makes
	// those four indistinguishable (Q-17) and an agent that guessed between them would
	// invent a distinction the protocol does not carry.
	ErrPairingCodeInvalid = errors.New("session: the pairing code is not valid")

	// ErrPairingRateLimited is the exchange's 429, per-IP or global.
	ErrPairingRateLimited = errors.New("session: pairing is rate limited; retry later")

	// ErrPairingUnavailable is the exchange's 503: Redis or the internal CA cannot
	// answer. Distinct from a rejection, because retrying is the right move here and
	// issuing a fresh code is the right move for a rejection.
	ErrPairingUnavailable = errors.New("session: the pairing service is unavailable")

	// ErrAlreadyPaired means credentials already exist locally. Pair refuses rather
	// than overwriting: the code is single-use server-side, so the exchange would fail
	// anyway — but only AFTER this agent had already destroyed the working credential
	// it holds. Refusing first is what keeps a mistyped second `pair` from unpairing a
	// healthy agent.
	ErrAlreadyPaired = errors.New("session: this agent is already paired; wipe credentials first")
)

// Deps is what the session manager needs from the rest of the agent.
//
// A struct rather than a widening argument list, because leaves 8.5 to 8.7 add the
// transport, the verifier and the dispatcher to it, and every one of those additions
// would otherwise change the signature of every constructor call in the tree.
type Deps struct {
	Store  Store
	Logger *zap.Logger
	// HTTPClient performs the pairing exchange. Optional: nil means a client with
	// defaultExchangeTimeout and the system trust store. Injected so the exchange is
	// testable against an httptest server without reaching the network.
	HTTPClient *http.Client
	// Clock is time.Now unless a test replaces it.
	Clock func() time.Time
	// AgentVersion is reported to the backend during the exchange and stored on the
	// device row. Passed in from the build info rather than read from a package
	// variable here, so the value in an audit row is the version that was actually
	// built and not a constant somebody forgot to bump.
	AgentVersion string

	// ---- Serve's collaborators (leaf 8.5) -------------------------------------------
	//
	// All optional in the sense that Pair, Status and Wipe do not need them; Serve
	// refuses to run without the ones it cannot substitute for, rather than running in a
	// weakened form. A missing Verifier, for instance, refuses every inbound frame.

	// Identity supplies the mTLS client configuration for the socket dial (§10.2).
	Identity identity.Provider
	// Transport builds one transport per connection attempt. A factory rather than an
	// instance because a transport holds a connection: reusing one across reconnects
	// would carry the dead socket into the new attempt. nil means the real WSS transport.
	Transport func(*tls.Config) connection.Transport
	// Verifier checks every inbound envelope. nil refuses every frame.
	Verifier Verifier
	// Runner executes verified commands; leaf 8.7's dispatcher, through an adapter.
	Runner CommandRunner
	// Journal is the durable outbound queue drained after a successful connect (D-41).
	Journal Journal
	// Bundle is the agent's policy-bundle view; nil means "holds no bundle", which
	// refuses mutations and holds intents rather than allowing them.
	Bundle BundleState
	// Jitter returns §7.4's uniform factor in [0.5, 1.5]. Injected so a backoff test
	// measures the bound rather than a random draw.
	Jitter func() float64
	// After and NewTicker are the two time sources the reconnect and heartbeat loops
	// use. Injected together with Clock so a test drives 90-second behaviour in
	// milliseconds instead of waiting.
	After     func(time.Duration) <-chan time.Time
	NewTicker func(time.Duration) Ticker
	// Capabilities is reported in `session.connect`.
	Capabilities []string
}

// Ticker is the heartbeat's clock, narrowed to what the loop uses so a test can drive it.
type Ticker interface {
	C() <-chan time.Time
	Stop()
}

type realTicker struct{ t *time.Ticker }

func (r realTicker) C() <-chan time.Time { return r.t.C }
func (r realTicker) Stop()               { r.t.Stop() }

// Manager is the agent half of phases.md §1.1: JSON-RPC 2.0 over WSS on Phase 0's fixed
// envelope, layered ABOVE connection.Transport so the Phase 0 transport contract is
// consumed, not modified.
type Manager struct {
	backendURL string
	store      Store
	logger     *zap.Logger
	http       *http.Client
	now        func() time.Time
	version    string

	// Serve's collaborators (leaf 8.5).
	identity       identity.Provider
	dial           func(*tls.Config) connection.Transport
	verifier       Verifier
	runner         CommandRunner
	journal        Journal
	bundle         BundleState
	jitter         func() float64
	after          func(time.Duration) <-chan time.Time
	newTicker      func(time.Duration) Ticker
	capabilityList []string
	startedAt      time.Time

	// The measured clock offset against the backend (§7.6). Guarded by its own mutex rather
	// than folded into the live session, because `agent doctor` reads it between sessions.
	skewMu       sync.Mutex
	skew         time.Duration
	skewMeasured bool
}

// uptime is what `session.heartbeat` reports. Measured from construction rather than from
// the current connection, because an agent that has reconnected forty times has still been
// up the whole time and that is the number an operator is asking about.
func (m *Manager) uptime() time.Duration { return m.now().Sub(m.startedAt) }

// NewManager builds the manager. `backendURL` is AGENT_BACKEND_WSS_URL; empty is a
// supported state and makes Status return connection.ErrDisabled.
func NewManager(backendURL string, deps Deps) (*Manager, error) {
	if deps.Store == nil {
		return nil, fmt.Errorf("session: NewManager needs a credential Store")
	}
	logger := deps.Logger
	if logger == nil {
		logger = zap.NewNop()
	}
	client := deps.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: defaultExchangeTimeout}
	}
	clock := deps.Clock
	if clock == nil {
		clock = time.Now
	}
	version := deps.AgentVersion
	if version == "" {
		version = "unknown"
	}
	dial := deps.Transport
	if dial == nil {
		dial = func(cfg *tls.Config) connection.Transport {
			return connection.NewWSSTransport(logger, connection.WithTLSConfig(cfg))
		}
	}
	jitter := deps.Jitter
	if jitter == nil {
		jitter = defaultJitter
	}
	after := deps.After
	if after == nil {
		after = time.After
	}
	ticker := deps.NewTicker
	if ticker == nil {
		ticker = func(d time.Duration) Ticker { return realTicker{t: time.NewTicker(d)} }
	}
	return &Manager{
		backendURL:     backendURL,
		store:          deps.Store,
		logger:         logger,
		http:           client,
		now:            clock,
		version:        version,
		identity:       deps.Identity,
		dial:           dial,
		verifier:       deps.Verifier,
		runner:         deps.Runner,
		journal:        deps.Journal,
		bundle:         deps.Bundle,
		jitter:         jitter,
		after:          after,
		newTicker:      ticker,
		capabilityList: deps.Capabilities,
		startedAt:      clock(),
	}, nil
}

// PairResult is what `forgeops-agent pair` reports.
//
// Carries no secret: the device token and the envelope key go straight into the Store and
// are never returned to the caller, so a command that printed this struct — or a log line
// that captured it — cannot leak a credential. `CertFingerprint` is a hash and
// `DeviceID` is an identifier the backend already published.
type PairResult struct {
	DeviceID        string
	ProjectID       string
	CertFingerprint string
	CertNotAfter    time.Time
	RenewAfter      time.Time
	StoreBackend    string
}

// Status describes the pairing state for `agent doctor` and `agent.status` (§10.10).
type Status struct {
	Paired       bool
	DeviceID     string
	StoreBackend string
	CertNotAfter time.Time
	// Degraded is true when credentials live in a 0600 file because no keychain was
	// usable. Reported rather than hidden: OQ-26 accepts the fallback, and the whole
	// value of accepting it is that the operator is told.
	Degraded bool
	// ClockSkew is the measured offset against the backend's clock, positive when this
	// agent's clock is ahead. Meaningful only when ClockSkewMeasured is true — a zero skew
	// and an unmeasured one are the same number and very different facts (§7.6).
	ClockSkew         time.Duration
	ClockSkewMeasured bool
	ClockSkewBeyond   bool
}

// Status reports whether this agent is paired.
//
// Returns connection.ErrDisabled when no backend URL is configured and ErrUnpaired when
// one is configured but no credential exists. Those are the two situations §10.3 requires
// `doctor` to distinguish, and returning one error for both would erase the distinction
// at the only place a user can act on it.
func (m *Manager) Status(ctx context.Context) (Status, error) {
	backend := m.store.Backend()
	status := Status{StoreBackend: backend, Degraded: backend == BackendFile}
	if skew, measured := m.Skew(); measured {
		status.ClockSkew = skew
		status.ClockSkewMeasured = true
		_, status.ClockSkewBeyond = m.skewBeyondTolerance()
	}

	if strings.TrimSpace(m.backendURL) == "" {
		return status, connection.ErrDisabled
	}

	creds, err := m.store.Load(ctx)
	if err != nil {
		if errors.Is(err, ErrNoCredentials) {
			return status, ErrUnpaired
		}
		return status, err
	}
	if len(creds.DeviceToken) == 0 {
		return status, ErrUnpaired
	}

	status.Paired = true
	status.DeviceID = creds.DeviceID
	if notAfter, err := leafNotAfter(creds.ClientCert); err == nil {
		status.CertNotAfter = notAfter
	}
	return status, nil
}

// Pair exchanges a one-time pairing code for a device token, an envelope key and a
// short-lived client certificate. It is the only method that runs unauthenticated, and it
// runs exactly once per device: the code is single-use server-side, so a retry after a
// successful exchange fails by design.
//
// The 6-character code is small (32^6 ≈ 1.07e9 for Crockford base32). What makes it safe
// is not the code's entropy alone but the enclosing controls, all server-side: single use,
// 5-minute expiry, a 5-attempt cap per code after which the code is burned, a per-IP
// exchange limit, a global exchange bucket, at most one live code per project,
// constant-time comparison, and storage of only an HMAC of the code (§14.6 does the
// arithmetic; Q-17 quantifies it).
//
// The private key is generated here and never leaves the machine: only the CSR is sent.
func (m *Manager) Pair(ctx context.Context, code string, backendURL string) (*PairResult, error) {
	code = strings.TrimSpace(code)
	if code == "" {
		return nil, fmt.Errorf("session: pair needs a code")
	}
	if strings.TrimSpace(backendURL) == "" {
		backendURL = m.backendURL
	}
	endpoint, err := exchangeURL(backendURL)
	if err != nil {
		return nil, err
	}

	// Checked BEFORE the key pair is generated and before anything is sent, so a second
	// `pair` on a healthy agent costs nothing: no attempt against the code's 5-attempt
	// cap, and no window in which the local credential has been replaced by a request
	// that had not yet succeeded.
	if err := m.assertUnpaired(ctx); err != nil {
		return nil, err
	}

	pair, err := identity.NewKeyPair()
	if err != nil {
		return nil, err
	}
	csrPEM, err := identity.BuildCSR(pair, pairingCSRCommonName)
	if err != nil {
		return nil, err
	}
	fingerprint, err := pair.SPKISHA256()
	if err != nil {
		return nil, err
	}

	// CAN THIS MACHINE EVEN STORE THE RESULT? ASKED BEFORE THE CODE IS SPENT.
	//
	// The exchange is single-use and irreversible: it burns the code, issues a certificate
	// and marks the device active. Finding out afterwards that the credential does not fit
	// in the OS credential store leaves the user with a burned code, a backend that thinks
	// a device is active, and an agent holding nothing — which is what every `pair` on
	// Windows did, because the full bundle is past the 2560-byte Credential Manager
	// ceiling.
	//
	// The probe is built from what is already known at this point rather than from a guess:
	// the private key is the real one just generated and is the largest secret field, and
	// the token and envelope key are `credentialByteLength` by protocol on both sides. The
	// device id is not known until the response, so the probe uses a longer id than the
	// backend's ULID — erring towards refusing a credential that would have just fitted
	// rather than accepting one that will not.
	if err := m.store.CheckCapacity(ctx, capacityProbe(pair.KeyPEM)); err != nil {
		return nil, err
	}

	response, err := m.exchange(ctx, endpoint, exchangeRequest{
		Code:         code,
		CSR:          string(csrPEM),
		AgentVersion: m.version,
		Platform:     platformString(),
		Fingerprint:  fingerprint,
	})
	if err != nil {
		return nil, err
	}

	creds, err := credentialsFrom(response, pair.KeyPEM, fingerprint)
	if err != nil {
		return nil, err
	}
	if err := m.store.Save(ctx, creds); err != nil {
		// THE CODE HAS BEEN SPENT AND THE CREDENTIAL CANNOT BE KEPT, so the device is
		// surrendered rather than left active with nobody holding its token.
		//
		// `CheckCapacity` above makes the size case unreachable, so arriving here means
		// something changed between the probe and the write — a full disk, a keychain
		// locked mid-flight. Rare, but the resulting state is the worst one in the system:
		// the backend counts an active device against the project, the certificate is
		// valid for 24 hours, and no operator has a reason to look.
		return nil, m.surrender(ctx, response, err)
	}

	// Logged without the code, the token or the key. The device id and the certificate
	// serial are what an operator needs to correlate this pairing with the backend's
	// `device_paired` audit row, and they are both already known to the backend.
	m.logger.Info("device paired",
		zap.String("device_id", response.DeviceID),
		zap.String("cert_serial", response.CertSerial),
		zap.String("credential_store", m.store.Backend()),
	)

	notAfter, _ := time.Parse(time.RFC3339, response.CertNotAfter)
	renewAfter, _ := time.Parse(time.RFC3339, response.RenewAfter)
	return &PairResult{
		DeviceID:        response.DeviceID,
		ProjectID:       response.ProjectID,
		CertFingerprint: response.CertFingerprint,
		CertNotAfter:    notAfter,
		RenewAfter:      renewAfter,
		StoreBackend:    m.store.Backend(),
	}, nil
}

// Wipe removes the stored credentials and returns the agent to the unpaired state.
//
// Called by `pair --wipe` and, in leaf 8.5, on ErrRevoked. Succeeds when there is
// nothing to remove, because an agent told it is revoked has to reach the unpaired state
// regardless of what it finds.
func (m *Manager) Wipe(ctx context.Context) error {
	if err := m.store.Wipe(ctx); err != nil {
		return err
	}
	m.logger.Info("device credentials wiped", zap.String("credential_store", m.store.Backend()))
	return nil
}

// assertUnpaired refuses to overwrite a credential that already exists.
func (m *Manager) assertUnpaired(ctx context.Context) error {
	creds, err := m.store.Load(ctx)
	switch {
	case err == nil && len(creds.DeviceToken) > 0:
		return fmt.Errorf("%w (device %s)", ErrAlreadyPaired, creds.DeviceID)
	case err == nil, errors.Is(err, ErrNoCredentials):
		return nil
	default:
		// An unreadable or wrongly-permissioned store stops pairing rather than being
		// overwritten: ErrInsecurePermissions means the existing credential has already
		// been exposed, and writing a new one on top hides that from the operator.
		return err
	}
}

// ─── the exchange ───────────────────────────────────────────────────────────

// exchangeRequest is §3.1's `{code, csr, agent_version, platform, fingerprint}`.
type exchangeRequest struct {
	Code         string `json:"code"`
	CSR          string `json:"csr"`
	AgentVersion string `json:"agent_version"`
	Platform     string `json:"platform"`
	Fingerprint  string `json:"fingerprint"`
}

// exchangeResponse mirrors the backend's ExchangeResponse.
//
// `policy_bundle` and `policy_bundle_digest` are absent because the backend does not send
// them yet (leaf 9.3 publishes the bundle). Absent rather than optional-and-empty for the
// same reason the backend gives: D-30 makes a missing bundle a deny, so a zero-byte
// bundle would be a field meaning "refuse everything" that looks like a bundle.
type exchangeResponse struct {
	DeviceID        string `json:"device_id"`
	ProjectID       string `json:"project_id"`
	DeviceToken     string `json:"device_token"`
	EnvelopeKey     string `json:"envelope_key"`
	CSRSPKISHA256   string `json:"csr_spki_sha256"`
	ClientCert      string `json:"client_cert"`
	CABundle        string `json:"ca_bundle"`
	CertSerial      string `json:"cert_serial"`
	CertFingerprint string `json:"cert_fingerprint"`
	CertNotAfter    string `json:"cert_not_after"`
	RenewAfter      string `json:"renew_after"`
	// PolicyBundle is base64 (the bundle is a gzipped tar, so it is not text) and
	// PolicyBundleDigest is `sha256:…`. Both are absent when the project has no published
	// bundle — absent rather than empty, per D-30, so "none published" is distinguishable
	// from "published and empty".
	PolicyBundle       *string `json:"policy_bundle"`
	PolicyBundleDigest *string `json:"policy_bundle_digest"`
}

// problemDocument is the RFC 9457 body the backend returns on every failure path.
type problemDocument struct {
	Type   string `json:"type"`
	Title  string `json:"title"`
	Detail string `json:"detail"`
}

func (m *Manager) exchange(ctx context.Context, endpoint string, body exchangeRequest) (*exchangeResponse, error) {
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("session: encoding the exchange request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(encoded))
	if err != nil {
		return nil, fmt.Errorf("session: building the exchange request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := m.http.Do(req)
	if err != nil {
		// Wrapped without the request body: the code is in there, and a dial error is
		// exactly the kind of message that gets pasted into an issue.
		return nil, fmt.Errorf("session: pairing exchange failed: %w", redactURLError(err))
	}
	defer func() { _ = resp.Body.Close() }()

	// Bounded read. The response is a few kilobytes of PEM and hex; an unbounded read
	// against a hostile or broken endpoint is a memory exhaustion the agent cannot
	// survive, and pairing is the one exchange with no authentication in front of it.
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("session: reading the exchange response: %w", err)
	}

	if resp.StatusCode != http.StatusCreated {
		return nil, exchangeError(resp.StatusCode, raw)
	}

	var decoded exchangeResponse
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return nil, fmt.Errorf("session: decoding the exchange response: %w", err)
	}
	return &decoded, nil
}

// exchangeError maps a status code onto one of the typed errors.
//
// The problem `type` is preferred over the status where both are available, because the
// backend maps a Redis outage and a CA outage onto the same 503 with the same
// `pairing-unavailable` type, and the status alone cannot distinguish 503 from a proxy's.
func exchangeError(status int, raw []byte) error {
	var doc problemDocument
	_ = json.Unmarshal(raw, &doc)
	kind := doc.Type
	if idx := strings.LastIndex(kind, "/"); idx >= 0 {
		kind = kind[idx+1:]
	}

	switch {
	case kind == "pairing-code-invalid" || status == http.StatusUnauthorized:
		return fmt.Errorf("%w: issue a new code and try again", ErrPairingCodeInvalid)
	case kind == "pairing-rate-limited" || status == http.StatusTooManyRequests:
		return fmt.Errorf("%w: %s", ErrPairingRateLimited, doc.Detail)
	case kind == "pairing-unavailable" || status == http.StatusServiceUnavailable:
		return fmt.Errorf("%w: %s", ErrPairingUnavailable, doc.Detail)
	case kind == "csr-invalid":
		return fmt.Errorf("session: the backend rejected the certificate request: %s", doc.Detail)
	default:
		return fmt.Errorf("session: pairing exchange returned HTTP %d: %s", status, doc.Title)
	}
}

// credentialsFrom validates the exchange response and turns it into Credentials.
//
// Every field is checked before anything is persisted. A credential set that is wrong in
// a way the agent could have detected here becomes, otherwise, a stored credential that
// fails at every future handshake with an error that points at the backend.
// CapacityProbeForDoctor is a credential of the shape and size a real pairing produces, for
// `agent doctor` to ask the store whether it could hold one.
//
// Exported for `doctor` alone. `pair` uses `capacityProbe` with the real private key it has just
// generated; `doctor` has no key pair and no reason to make one, so it uses the largest PEM the
// agent can produce. Both go through `Store.CheckCapacity`, so they cannot disagree about what
// "fits" means.
func CapacityProbeForDoctor() Credentials {
	// An EC P-256 private key PEM is ~227 bytes. A generous 512 keeps the answer valid if the
	// key type ever changes, and erring large is the safe direction: it can only predict a
	// failure slightly early, never miss one.
	// Opaque bytes: `CheckCapacity` marshals and measures, so only the length matters. No PEM
	// armour, which would put credential-shaped text on a production path for no coverage.
	return capacityProbe([]byte(strings.Repeat("A", 512)))
}

// capacityProbe builds a credential of the same shape and at least the same size as the one
// the exchange is about to return, for `Store.CheckCapacity` to try to write.
//
// NOT A FABRICATED CREDENTIAL: it is never returned to a caller, never stored under the real
// key, and never used to authenticate anything. It exists to be measured and is deleted
// immediately. The one field that is real is the private key, because it is the largest
// secret and is already in hand; the other two are sized from `credentialByteLength`, the
// protocol constant both sides agree on.
func capacityProbe(keyPEM []byte) Credentials {
	return Credentials{
		// Longer than the backend's 26-character ULID, so the probe cannot succeed where the
		// real save would fail.
		DeviceID:    strings.Repeat("D", 64),
		DeviceToken: make([]byte, credentialByteLength),
		EnvelopeKey: make([]byte, credentialByteLength),
		ClientKey:   keyPEM,
	}
}

// surrender gives back a device whose credential could not be stored, and returns the error
// the caller should report.
//
// WHY THE AGENT MAY DO THIS AT ALL, given `DELETE /api/v1/agents/{device_id}` is admin-only:
// this is not that operation. It is a self-report authenticated by the device token the
// exchange just issued, and its only possible effect is to abandon the caller's own device.
// It cannot name another device, so it gains no authority the individual verbs do not have —
// the token holder already IS the device, and the worst it can do to itself is precisely
// what this call does on purpose.
//
// The original persistence error is always preserved and always reported: the user's problem
// is that pairing failed. A failed surrender is appended rather than substituted, and the
// device id is named in both branches because that is what an operator needs.
func (m *Manager) surrender(ctx context.Context, response *exchangeResponse, cause error) error {
	surrenderErr := m.abandonSelf(ctx, response)
	if surrenderErr == nil {
		m.logger.Warn("credential could not be stored; device surrendered",
			zap.String("device_id", response.DeviceID),
			zap.String("credential_store", m.store.Backend()),
			zap.Error(cause),
		)
		return fmt.Errorf(
			"%w. The pairing code was already spent, so device %s was surrendered and is no "+
				"longer active — nothing was left half-paired. Fix the cause above, then pair "+
				"again with a new code",
			cause, response.DeviceID)
	}

	// Both failed. This is the one path that can still leave a row an operator must clean
	// up, so it says exactly that rather than implying the system is consistent.
	m.logger.Error("credential could not be stored and the device could not be surrendered",
		zap.String("device_id", response.DeviceID),
		zap.NamedError("store_error", cause),
		zap.NamedError("surrender_error", surrenderErr),
	)
	return fmt.Errorf(
		"%w. The pairing code was already spent and this agent then failed to surrender the "+
			"device (%v), so device %s may still be active on the backend with no agent holding "+
			"its credential. Revoke it from the ForgeOps UI, then pair again",
		cause, surrenderErr, response.DeviceID)
}

// abandonSelf tells the backend the agent could not keep the credential it was just issued.
//
// Authenticated by the device token from the response, which is the only credential in
// existence for this device and is held by exactly one process — this one. The route accepts
// no device id in its body: the token identifies the device, so the call cannot name another.
func (m *Manager) abandonSelf(ctx context.Context, response *exchangeResponse) error {
	endpoint, err := abandonURL(m.backendURL)
	if err != nil {
		return err
	}

	// A short, independent deadline. The caller's context may already be near its end after
	// a failed write, and surrendering is worth a fresh few seconds; equally it must not
	// hang, because the user is waiting on an error that is already determined.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), abandonTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, http.NoBody)
	if err != nil {
		return fmt.Errorf("session: building the surrender request: %w", err)
	}
	req.Header.Set(authorizationHeader, bearerScheme+response.DeviceToken)
	req.Header.Set("Accept", "application/json")

	resp, err := m.http.Do(req)
	if err != nil {
		return fmt.Errorf("session: surrender request failed: %w", redactURLError(err))
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))

	if resp.StatusCode != http.StatusNoContent {
		return fmt.Errorf("session: surrender returned HTTP %d", resp.StatusCode)
	}
	return nil
}

func credentialsFrom(r *exchangeResponse, keyPEM []byte, sentFingerprint string) (Credentials, error) {
	var zero Credentials

	if r.DeviceID == "" {
		return zero, fmt.Errorf("session: the exchange response carries no device id")
	}
	token, err := decodeCredentialBytes(r.DeviceToken, "device_token")
	if err != nil {
		return zero, err
	}
	envelopeKey, err := decodeCredentialBytes(r.EnvelopeKey, "envelope_key")
	if err != nil {
		return zero, err
	}

	// The backend echoes the fingerprint it computed from the CSR. Comparing it is how
	// the agent learns it is being issued a certificate for the key it actually holds,
	// rather than one substituted in flight by something terminating TLS.
	if r.CSRSPKISHA256 != "" && !strings.EqualFold(r.CSRSPKISHA256, sentFingerprint) {
		return zero, fmt.Errorf(
			"session: the backend signed a different public key than this agent submitted")
	}

	certPEM := []byte(r.ClientCert)
	caPEM := []byte(r.CABundle)
	if len(certPEM) == 0 {
		return zero, fmt.Errorf("session: the exchange response carries no client certificate")
	}
	if len(caPEM) == 0 {
		return zero, fmt.Errorf("session: the exchange response carries no CA bundle")
	}
	// Proof that the issued certificate matches the key that never left this machine.
	// Without it, a mismatch surfaces as a TLS handshake failure at first connect, long
	// after the pairing code has been burned and cannot be reused to recover.
	if _, err := tls.X509KeyPair(certPEM, keyPEM); err != nil {
		return zero, fmt.Errorf("session: the issued certificate does not match the local key: %w", err)
	}

	// The pinned bundle. Decoded and length-checked here for the same reason every other
	// field is: a malformed bundle stored now becomes a `policy-bundle-stale` refusal at every
	// future command, with an error that points at policy rather than at pairing.
	//
	// A MISSING bundle is not an error. The project may have none published yet, and §1.7's
	// order (publish, then pair) is a recommendation to the operator rather than something the
	// exchange can enforce. The agent then holds no bundle, `Current` is false, and mutations
	// are refused — which is D-25's direction: absent policy is never permission.
	var bundle []byte
	var bundleDigest string
	if r.PolicyBundleDigest != nil && *r.PolicyBundleDigest != "" {
		bundleDigest = *r.PolicyBundleDigest
		if !strings.HasPrefix(bundleDigest, "sha256:") {
			return zero, fmt.Errorf(
				"session: the pinned policy bundle digest is not sha256-prefixed")
		}
		if r.PolicyBundle == nil || *r.PolicyBundle == "" {
			return zero, fmt.Errorf(
				"session: the exchange pinned a policy bundle digest but sent no bundle")
		}
		decoded, err := base64.StdEncoding.DecodeString(*r.PolicyBundle)
		if err != nil {
			return zero, fmt.Errorf("session: the policy bundle is not valid base64")
		}
		if len(decoded) == 0 {
			return zero, fmt.Errorf("session: the policy bundle is empty")
		}
		// Verified against the digest the backend pinned, so a bundle corrupted in transit is
		// refused here rather than enforced. The digest is what the chokepoint compares, so a
		// bundle that does not match it is not the policy this device was authorised under.
		sum := sha256.Sum256(decoded)
		if computed := "sha256:" + hex.EncodeToString(sum[:]); computed != bundleDigest {
			return zero, fmt.Errorf(
				"session: the policy bundle does not match its pinned digest")
		}
		bundle = decoded
	}

	return Credentials{
		DeviceID:           r.DeviceID,
		DeviceToken:        token,
		EnvelopeKey:        envelopeKey,
		ClientCert:         certPEM,
		ClientKey:          keyPEM,
		CABundle:           caPEM,
		PolicyBundle:       bundle,
		PolicyBundleDigest: bundleDigest,
	}, nil
}

// decodeCredentialBytes hex-decodes a 32-byte credential, naming the field but never the
// value: the error text for a malformed token must not contain the token.
func decodeCredentialBytes(value, field string) ([]byte, error) {
	if value == "" {
		return nil, fmt.Errorf("session: the exchange response carries no %s", field)
	}
	decoded, err := hex.DecodeString(value)
	if err != nil {
		return nil, fmt.Errorf("session: %s is not hex", field)
	}
	if len(decoded) != credentialByteLength {
		return nil, fmt.Errorf(
			"session: %s is %d bytes; expected %d", field, len(decoded), credentialByteLength)
	}
	return decoded, nil
}

// HTTPOrigin turns the configured backend WSS URL into the HTTP origin of the same backend.
//
// EXPORTED SO THERE IS ONE COPY OF THIS RULE. `exchangeURL` below needs it, and so does the app
// layer's codebase indexer, which POSTs a scan report to the same origin. A second implementation
// beside this one is two copies of a fact — the failure the journal calls pattern H — and the way
// it would show up is an agent that pairs against one backend and uploads its index to another.
//
// The agent is configured with a `wss://` URL because that is what the session uses. `ws://` maps
// to `http://` so a local development stack works, and any other scheme is REFUSED rather than
// guessed at: guessing `https` for an unrecognised scheme would silently send a device token
// somewhere the operator did not name.
//
// Any path on the configured URL is discarded. The WSS URL points at the socket route, so keeping
// it would produce `/api/v1/ws/agent/api/v1/...` at every caller.
func HTTPOrigin(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", fmt.Errorf("%w: pair needs --backend or AGENT_BACKEND_WSS_URL", connection.ErrDisabled)
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("session: backend URL is not a URL: %w", err)
	}
	switch parsed.Scheme {
	case "wss", "https":
		parsed.Scheme = "https"
	case "ws", "http":
		parsed.Scheme = "http"
	default:
		return "", fmt.Errorf(
			"session: backend URL scheme %q is not one of wss, ws, https, http", parsed.Scheme)
	}
	if parsed.Host == "" {
		return "", fmt.Errorf("session: backend URL has no host")
	}
	parsed.Path = ""
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String(), nil
}

// exchangeURL turns the configured backend URL into the exchange endpoint.
//
// The scheme and host come from `HTTPOrigin`; this adds the one path the exchange uses.
func exchangeURL(raw string) (string, error) {
	origin, err := HTTPOrigin(raw)
	if err != nil {
		return "", err
	}
	parsed, err := url.Parse(origin)
	if err != nil {
		return "", fmt.Errorf("session: backend origin is not a URL: %w", err)
	}
	parsed.Path = exchangePath
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String(), nil
}

// abandonURL turns the configured backend URL into the self-surrender endpoint.
//
// Built the same way as `exchangeURL` and from the same origin, so a surrender can only ever
// reach the backend the credential was issued by.
func abandonURL(raw string) (string, error) {
	origin, err := HTTPOrigin(raw)
	if err != nil {
		return "", err
	}
	parsed, err := url.Parse(origin)
	if err != nil {
		return "", fmt.Errorf("session: backend origin is not a URL: %w", err)
	}
	parsed.Path = abandonPath
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String(), nil
}

// redactURLError strips the request URL from a transport error.
//
// *url.Error stringifies as `Post "https://host/path": reason`. The path here carries no
// secret, but the error is user-facing and the habit of returning transport errors
// verbatim is how a query string with a credential in it ends up in a bug report.
func redactURLError(err error) error {
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		return fmt.Errorf("%s to the backend failed: %w", urlErr.Op, urlErr.Err)
	}
	return err
}

// platformString is what the backend stores on the device row.
//
// GOOS/GOARCH and nothing else. The hostname would be more useful to an operator and is
// deliberately not sent: it is the one field here that identifies the machine rather than
// the build, and the pairing request travels on an unauthenticated route.
func platformString() string {
	return runtime.GOOS + "/" + runtime.GOARCH
}

// leafNotAfter reports a PEM certificate's expiry, for Status.
func leafNotAfter(certPEM []byte) (time.Time, error) {
	block, _ := pem.Decode(certPEM)
	if block == nil {
		return time.Time{}, fmt.Errorf("session: stored certificate is not PEM")
	}
	leaf, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return time.Time{}, fmt.Errorf("session: parsing the stored certificate: %w", err)
	}
	return leaf.NotAfter, nil
}
