// SPDX-License-Identifier: Apache-2.0
package session

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/parag8487/ForgeOps/agent/internal/connection"
	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/identity"
)

// ---------------------------------------------------------------------------------------
// Doubles.
//
// Every one of them enforces the real signature (FO-TD001..004's spirit, applied to Go):
// they are concrete types implementing the interface the manager declares, so a change to
// that interface is a compile error here rather than a test that keeps passing against a
// contract nobody implements any more.
// ---------------------------------------------------------------------------------------

// fakeTransport is one connection. It answers `session.connect` from a script, records
// everything sent, and delivers whatever a test queues.
type fakeTransport struct {
	mu       sync.Mutex
	sent     []connection.Request
	closes   []closeRecord
	inbound  chan []byte
	done     chan struct{}
	endOnce  sync.Once
	dialErr  error
	recvErr  error
	pingErr  error // when set, the far end has stopped answering pings
	connect  *connectResult
	rejectID string // when set, session.connect is answered with an error object
	dialed   int
	header   http.Header
	url      string
}

type closeRecord struct {
	code   connection.StatusCode
	reason string
}

func newFakeTransport(result *connectResult) *fakeTransport {
	return &fakeTransport{
		inbound: make(chan []byte, 16),
		done:    make(chan struct{}),
		connect: result,
		recvErr: errors.New("closed"),
	}
}

func (f *fakeTransport) Dial(_ context.Context, url string, hdr http.Header) error {
	f.mu.Lock()
	f.dialed++
	f.url = url
	f.header = hdr
	f.mu.Unlock()
	return f.dialErr
}

func (f *fakeTransport) Send(_ context.Context, payload []byte) error {
	var request connection.Request
	if err := json.Unmarshal(payload, &request); err != nil {
		return err
	}
	// `rejectID` and `connect` are SNAPSHOTTED UNDER THE SAME LOCK that records the request, because
	// tests mutate them WHILE `Serve` is running: `TestServe_ASuccessfulHandshakeResetsTheBackoffToTheBase`
	// clears `rejectID` mid-flight to let the handshake succeed, and takes `f.mu` to do it.
	//
	// This method took the lock only to append to `sent` and then read `rejectID` after releasing it,
	// which is a genuine data race that `-race` reported on a CI runner — the test's write was already
	// correctly guarded, so the unsynchronised half was here. It had gone unnoticed because it needs
	// the write and the read to interleave, and on a developer machine they rarely do.
	f.mu.Lock()
	f.sent = append(f.sent, request)
	rejectID := f.rejectID
	connectResult := f.connect
	f.mu.Unlock()

	if request.Method == "session.connect" && request.ID != nil {
		if rejectID != "" {
			f.push(mustJSON(connection.Response{
				JSONRPC: "2.0", ID: request.ID,
				Error: &connection.RPCError{Code: -32000, Message: rejectID},
			}))
			return nil
		}
		if connectResult != nil {
			f.push(mustJSON(connection.Response{
				JSONRPC: "2.0", ID: request.ID, Result: mustJSON(*connectResult),
			}))
		}
	}
	return nil
}

func (f *fakeTransport) Receive(ctx context.Context) ([]byte, error) {
	// `done` is checked first and non-blockingly: once the test has ended the connection,
	// every subsequent Receive must report that rather than draining frames that were
	// queued before it.
	select {
	case <-f.done:
		f.mu.Lock()
		defer f.mu.Unlock()
		return nil, f.recvErr
	default:
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-f.done:
		f.mu.Lock()
		defer f.mu.Unlock()
		return nil, f.recvErr
	case frame := <-f.inbound:
		return frame, nil
	}
}

// Ping answers the session's liveness check.
//
// Succeeds by default, because a healthy backend answering pings is the ordinary case and every
// existing test assumes the session stays up. `pingErr` lets a test make the far end stop answering,
// which is the condition that must drop the session now that inbound silence no longer does — see
// `beat` for why that changed.
func (f *fakeTransport) Ping(ctx context.Context) error {
	f.mu.Lock()
	err := f.pingErr
	f.mu.Unlock()
	if err != nil {
		return err
	}
	select {
	case <-f.done:
		f.mu.Lock()
		defer f.mu.Unlock()
		return f.recvErr
	case <-ctx.Done():
		return ctx.Err()
	default:
		return nil
	}
}

// failPings makes every subsequent Ping report err, as an unresponsive backend does.
func (f *fakeTransport) failPings(err error) {
	f.mu.Lock()
	f.pingErr = err
	f.mu.Unlock()
}

func (f *fakeTransport) Close(code connection.StatusCode, reason string) error {
	f.mu.Lock()
	f.closes = append(f.closes, closeRecord{code: code, reason: reason})
	f.mu.Unlock()
	return nil
}

func (f *fakeTransport) push(frame []byte) {
	select {
	case f.inbound <- frame:
	default:
	}
}

// end stops the session with err. Idempotent, because Serve reconnects: a second call from
// the loop's next attempt must not panic the test.
func (f *fakeTransport) end(err error) {
	f.mu.Lock()
	f.recvErr = err
	f.mu.Unlock()
	f.endOnce.Do(func() { close(f.done) })
}

func (f *fakeTransport) methods() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]string, 0, len(f.sent))
	for _, r := range f.sent {
		out = append(out, r.Method)
	}
	return out
}

func (f *fakeTransport) frames(method string) []connection.Request {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := []connection.Request{}
	for _, r := range f.sent {
		if r.Method == method {
			out = append(out, r)
		}
	}
	return out
}

// manualTicker is the heartbeat clock, driven by the test rather than by time passing.
type manualTicker struct {
	ch      chan time.Time
	stopped bool
	mu      sync.Mutex
}

func newManualTicker() *manualTicker { return &manualTicker{ch: make(chan time.Time, 4)} }

func (m *manualTicker) C() <-chan time.Time { return m.ch }
func (m *manualTicker) Stop() {
	m.mu.Lock()
	m.stopped = true
	m.mu.Unlock()
}
func (m *manualTicker) fire(now time.Time) { m.ch <- now }

// fakeIdentity is the mTLS half. It hands back an empty config: nothing in these tests
// completes a real handshake, and a test that needed one would be asserting crypto/tls.
type fakeIdentity struct {
	err error
}

func (f fakeIdentity) ClientTLS(context.Context) (*tls.Config, error) {
	if f.err != nil {
		return nil, f.err
	}
	return &tls.Config{MinVersion: tls.VersionTLS13}, nil
}

func (f fakeIdentity) Identity(context.Context) (identity.Info, error) { return identity.Info{}, nil }
func (f fakeIdentity) RenewBefore() time.Duration                      { return time.Hour }

// fakeBundle is the policy-bundle view. `current` is set by the test because leaf 9.4 owns the
// real one.
//
// Guarded by a mutex, and that is not defensive style: `ObserveBackend` is called from `Serve`'s
// goroutine while the test reads the result, and `go test -race -shuffle=on` reported it as a data
// race at group 8's close-out. A double that races is a double that can report a stale answer, so
// the assertion it feeds is not reliable — the detector was right and the fix belongs here.
type fakeBundle struct {
	mu       sync.Mutex
	digest   string
	current  bool
	observed []string
}

func (b *fakeBundle) Digest() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.digest
}

func (b *fakeBundle) Current() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.current
}

func (b *fakeBundle) ObserveBackend(digest string, stale bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.observed = append(b.observed, digest)
	if stale {
		b.current = false
	}
}

func (b *fakeBundle) observedDigests() []string {

	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]string(nil), b.observed...)
}

// recordingJournal captures what the drain did without touching a disk.
//
// Also mutex-guarded, for the reason above: `Drain` runs on the session's goroutine and the test
// polls its results. `records` and `stats` are the exception — both are written by the test BEFORE
// `Serve` starts and only read afterwards, so they need no lock on the write side; `Stats` still
// takes it, because it is called from the heartbeat goroutine.
type recordingJournal struct {
	mu            sync.Mutex
	records       []Record
	bundleCurrent []bool
	delivered     []Record
	sendErr       error
	wipes         int
	stats         JournalStats
}

func (j *recordingJournal) Append(context.Context, Record) error { return nil }

func (j *recordingJournal) Drain(
	ctx context.Context,
	send func(context.Context, Record) error,
	bundleCurrent bool,
) (DrainReport, error) {
	j.mu.Lock()
	j.bundleCurrent = append(j.bundleCurrent, bundleCurrent)
	queued := append([]Record(nil), j.records...)
	j.mu.Unlock()

	report := DrainReport{}
	// The real FileJournal orders non-intents before intents and stops at the first
	// failure. Both behaviours are reproduced here, because a double that delivered
	// everything regardless would make the stale-bundle assertion below vacuous.
	for _, pass := range []bool{false, true} {
		if pass && !bundleCurrent {
			for _, r := range queued {
				if r.Kind == KindIntent {
					report.IntentsHeld++
				}
			}
			return report, nil
		}
		for _, r := range queued {
			if (r.Kind == KindIntent) != pass {
				continue
			}
			if err := send(ctx, r); err != nil {
				return report, err
			}
			j.mu.Lock()
			j.delivered = append(j.delivered, r)
			j.mu.Unlock()
			report.Delivered++
			if pass {
				report.IntentsDelivered++
			}
		}
	}
	j.mu.Lock()
	err := j.sendErr
	j.mu.Unlock()
	return report, err
}

func (j *recordingJournal) Wipe(context.Context) error {
	j.mu.Lock()
	j.wipes++
	j.mu.Unlock()
	return nil
}

func (j *recordingJournal) Stats(context.Context) (JournalStats, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.stats, nil
}

func (j *recordingJournal) deliveredRecords() []Record {
	j.mu.Lock()
	defer j.mu.Unlock()
	return append([]Record(nil), j.delivered...)
}

func (j *recordingJournal) bundleFlags() []bool {
	j.mu.Lock()
	defer j.mu.Unlock()
	return append([]bool(nil), j.bundleCurrent...)
}

func (j *recordingJournal) wipeCount() int {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.wipes
}

func (j *recordingJournal) setStats(stats JournalStats) {
	j.mu.Lock()
	j.stats = stats
	j.mu.Unlock()
}

// recordingRunner is the dispatcher stand-in. It records the order it was called in, which
// is what makes "serial" a measurement rather than a claim.
type recordingRunner struct {
	mu       sync.Mutex
	started  []string
	inFlight int
	maxSeen  int
	outcome  CommandOutcome
	err      error
	release  chan struct{}
	progress bool
}

func (r *recordingRunner) Execute(
	ctx context.Context,
	v *envelope.Verified,
	progress func(Progress),
) (CommandOutcome, error) {
	r.mu.Lock()
	r.started = append(r.started, v.CommandID())
	r.inFlight++
	if r.inFlight > r.maxSeen {
		r.maxSeen = r.inFlight
	}
	r.mu.Unlock()

	if r.release != nil {
		select {
		case <-r.release:
		case <-ctx.Done():
		}
	}
	if r.progress {
		progress(Progress{Percent: 50, Stage: "apply", Message: "halfway"})
	}

	r.mu.Lock()
	r.inFlight--
	r.mu.Unlock()
	return r.outcome, r.err
}

// The verifier is the REAL one.
//
// No stub, and no test-only constructor for `envelope.Verified`: the whole value of that type
// is that only `Verify` can produce it, and a `NewTestVerified` seam would be a second
// constructor — exactly the hole the unexported field exists to close. So these tests sign
// real envelopes with the credential's own envelope key and let the real verifier decide. A
// refusal is produced by tampering, which is also how a real one arrives.
func newTestVerifier(t *testing.T, creds Credentials, digest string, now func() time.Time) *envelope.Verifier {
	t.Helper()
	keys := envelope.NewStaticKeySource()
	keys.Set(creds.DeviceID, creds.EnvelopeKey)
	guard, err := envelope.NewMemoryReplayGuard(300*time.Second, 1024)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	verifier, err := envelope.NewVerifier(keys, guard, envelope.NewStaticBundleDigest(digest),
		envelope.WithClock(now))
	if err != nil {
		t.Fatalf("NewVerifier: %v", err)
	}
	return verifier
}

// signedEnvelope builds one genuinely signed envelope as `command.execute` params.
func signedEnvelope(t *testing.T, creds Credentials, digest, commandID string, seq int64, now time.Time) json.RawMessage {
	t.Helper()
	env := envelope.Envelope{
		V:             envelope.Version,
		CommandID:     commandID,
		DeviceID:      creds.DeviceID,
		Operation:     envelope.Operation("files.apply"),
		Args:          json.RawMessage(`{"root":"."}`),
		ApprovalID:    "approval-" + commandID,
		PolicyContext: envelope.PolicyContext{BundleDigest: digest, Decision: "allow"},
		Nonce:         fmt.Sprintf("%032x", seq),
		Seq:           seq,
		NotAfter:      now.Add(60 * time.Second).Unix(),
	}
	signature, err := envelope.Sign(envelope.DomainPrefix, env, creds.EnvelopeKey)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	env.Signature = signature
	return mustJSON(env)
}

// tamperedEnvelope is a correctly shaped envelope whose signature covers different bytes.
func tamperedEnvelope(t *testing.T, creds Credentials, digest, commandID string, seq int64, now time.Time) json.RawMessage {
	t.Helper()
	raw := signedEnvelope(t, creds, digest, commandID, seq, now)
	var env envelope.Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	env.Args = json.RawMessage(`{"root":"/etc"}`)
	return mustJSON(env)
}

func mustJSON(v any) json.RawMessage {
	encoded, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return encoded
}

// ---------------------------------------------------------------------------------------
// The harness.
// ---------------------------------------------------------------------------------------

type serveHarness struct {
	manager   *Manager
	store     *FileStore
	transport *fakeTransport
	ticker    *manualTicker
	journal   *recordingJournal
	runner    *recordingRunner
	bundle    *fakeBundle
	delays    chan time.Duration
	now       *clockHolder
	creds     Credentials
	digest    string
}

type clockHolder struct {
	mu sync.Mutex
	at time.Time
}

func (c *clockHolder) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.at
}

func (c *clockHolder) advance(d time.Duration) {
	c.mu.Lock()
	c.at = c.at.Add(d)
	c.mu.Unlock()
}

func newServeHarness(t *testing.T, transport *fakeTransport) *serveHarness {
	t.Helper()

	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	creds := syntheticCredentials()
	if err := store.Save(context.Background(), creds); err != nil {
		t.Fatalf("Save: %v", err)
	}

	harness := &serveHarness{
		store:     store,
		transport: transport,
		ticker:    newManualTicker(),
		journal:   &recordingJournal{},
		runner:    &recordingRunner{outcome: CommandOutcome{Status: "succeeded"}},
		bundle:    &fakeBundle{digest: "sha256:local", current: true},
		delays:    make(chan time.Duration, 32),
		now:       &clockHolder{at: time.Date(2026, 8, 2, 12, 0, 0, 0, time.UTC)},
		creds:     creds,
		digest:    "sha256:local",
	}

	manager, err := NewManager("wss://backend.invalid", Deps{
		Store:        store,
		Clock:        harness.now.now,
		AgentVersion: "test",
		Identity:     fakeIdentity{},
		Transport:    func(*tls.Config) connection.Transport { return transport },
		Verifier:     newTestVerifier(t, creds, "sha256:local", harness.now.now),
		Runner:       harness.runner,
		Journal:      harness.journal,
		Bundle:       harness.bundle,
		Jitter:       func() float64 { return 1.0 },
		After: func(d time.Duration) <-chan time.Time {
			// Non-blocking: the reconnect loop must never be held up by a test that has
			// stopped reading, or a cancelled Serve would hang here instead of returning.
			select {
			case harness.delays <- d:
			default:
			}
			fired := make(chan time.Time, 1)
			fired <- harness.now.now()
			return fired
		},
		NewTicker: func(time.Duration) Ticker { return harness.ticker },
	})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	harness.manager = manager
	return harness
}

func okConnect() *connectResult {
	return &connectResult{
		SessionID:         "sess-1",
		HeartbeatInterval: 30,
		HeartbeatTimeout:  90,
		SeqBase:           7,
		BundleDigest:      "sha256:local",
	}
}

// ---------------------------------------------------------------------------------------
// §7.4 — backoff bounds and the hot loop.
// ---------------------------------------------------------------------------------------

func TestBackoffFor_MatchesTheDesignFormulaAndItsBounds(t *testing.T) {
	harness := newServeHarness(t, newFakeTransport(okConnect()))
	m := harness.manager

	// Attempt 0 is "connect now": the first dial, and the first dial after a successful
	// handshake, are not delayed. Anything else would add a second to every start-up.
	if got := m.backoffFor(0); got != 0 {
		t.Errorf("attempt 0 delayed by %s; the first dial must be immediate", got)
	}
	for _, tc := range []struct {
		attempt int
		want    time.Duration
	}{{1, time.Second}, {2, 2 * time.Second}, {3, 4 * time.Second}, {7, 64 * time.Second / 1}} {
		got := m.backoffFor(tc.attempt)
		want := tc.want
		if want > backoffCap {
			want = backoffCap
		}
		if got != want {
			t.Errorf("attempt %d: delay %s, want %s", tc.attempt, got, want)
		}
	}

	// The cap holds however many attempts have failed. Without this clause a formula that
	// forgot the min() would only be caught by whoever was still watching at attempt 30.
	for _, attempt := range []int{7, 8, 20, 1000} {
		if got := m.backoffFor(attempt); got != backoffCap {
			t.Errorf("attempt %d: delay %s, want the %s cap", attempt, got, backoffCap)
		}
	}
}

func TestBackoffFor_JitterStaysWithinTheHalfToOneAndAHalfBand(t *testing.T) {
	harness := newServeHarness(t, newFakeTransport(okConnect()))

	// The bound is what §7.4 fixes, so the bound is what is asserted -- at the cap, where a
	// jitter applied before the min() would show up as a delay above 1.5x.
	for _, factor := range []float64{0.5, 1.0, 1.5} {
		harness.manager.jitter = func() float64 { return factor }
		got := harness.manager.backoffFor(1000)
		want := time.Duration(float64(backoffCap) * factor)
		if got != want {
			t.Errorf("jitter %.1f at the cap: %s, want %s", factor, got, want)
		}
		if got < backoffCap/2 || got > backoffCap*3/2 {
			t.Errorf("jitter %.1f produced %s, outside [0.5x, 1.5x] of the cap", factor, got)
		}
	}

	// And the real jitter must actually stay in the band, over enough draws that a
	// mis-scaled random would show.
	for i := 0; i < 500; i++ {
		if f := defaultJitter(); f < 0.5 || f >= 1.5 {
			t.Fatalf("defaultJitter returned %v, outside [0.5, 1.5)", f)
		}
	}
}

func TestServe_ARejectedHandshakeDoesNotResetTheBackoff(t *testing.T) {
	// The hot loop §7.4 exists to prevent: a backend that accepts sockets and refuses
	// `session.connect`. If the attempt counter reset on a successful dial, every delay
	// would be the base one and the agent would hammer the backend forever.
	transport := newFakeTransport(nil)
	transport.rejectID = "handshake-required"
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()

	observed := []time.Duration{}
	for len(observed) < 4 {
		select {
		case d := <-harness.delays:
			observed = append(observed, d)
		case <-time.After(5 * time.Second):
			t.Fatalf("only %d backoff delays observed: %v", len(observed), observed)
		}
	}
	cancel()
	<-done

	want := []time.Duration{time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second}
	for i := range want {
		if observed[i] != want[i] {
			t.Fatalf("delays %v; want them to grow as %v -- a reset counter is the hot loop",
				observed, want)
		}
	}
	if transport.dialed < 4 {
		t.Errorf("dialed %d times; the loop is not retrying at all", transport.dialed)
	}
}

func TestServe_ASuccessfulHandshakeResetsTheBackoffToTheBase(t *testing.T) {
	// The control for the clause above, and the other half of §7.4's rule. A manager that
	// never reset the counter would pass the previous test and keep a 60-second delay for
	// hours after the backend came back. A manager that reset it to "no delay at all" would
	// hot-loop against a backend that accepts the handshake and drops the socket, which is
	// the same defect one step later. So the assertion is: after the delays have grown, an
	// accepted session's end is followed by the BASE delay.
	transport := newFakeTransport(okConnect())
	transport.rejectID = "handshake-required"
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()

	grown := []time.Duration{}
	for len(grown) < 3 {
		select {
		case d := <-harness.delays:
			grown = append(grown, d)
		case <-time.After(5 * time.Second):
			t.Fatalf("only %d delays observed: %v", len(grown), grown)
		}
	}
	if grown[2] != 4*time.Second {
		t.Fatalf("delays %v; expected them to have grown to 4s before the reset", grown)
	}

	// Now let the handshake succeed, and end the session once it has.
	transport.mu.Lock()
	transport.rejectID = ""
	transport.mu.Unlock()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })
	transport.end(errors.New("backend went away"))

	// Drain whatever was already queued, then require the next delay to be the base one.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case d := <-harness.delays:
			if d == backoffBase {
				cancel()
				<-done
				return
			}
		case <-time.After(50 * time.Millisecond):
		}
	}
	cancel()
	<-done
	t.Fatal("no delay of the base 1s was observed after an accepted session ended; the counter did not reset")
}

// ---------------------------------------------------------------------------------------
// §3.1, §7.3 — the handshake.
// ---------------------------------------------------------------------------------------

func TestServe_TheHandshakeCarriesTheDeviceIdVersionPlatformAndDigest(t *testing.T) {
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()

	waitFor(t, func() bool { return len(transport.frames("session.connect")) > 0 })
	frame := transport.frames("session.connect")[0]
	if frame.ID == nil {
		t.Error("session.connect was sent as a notification; the result cannot be correlated")
	}
	var params map[string]any
	if err := json.Unmarshal(frame.Params, &params); err != nil {
		t.Fatalf("params: %v", err)
	}
	creds := syntheticCredentials()
	if params["device_id"] != creds.DeviceID {
		t.Errorf("device_id %v, want %v", params["device_id"], creds.DeviceID)
	}
	if params["agent_version"] != "test" {
		t.Errorf("agent_version %v", params["agent_version"])
	}
	if params["policy_bundle_digest"] != "sha256:local" {
		t.Errorf("policy_bundle_digest %v; the handshake must present what the agent holds",
			params["policy_bundle_digest"])
	}
	if params["platform"] == "" {
		t.Error("platform is empty")
	}

	// The device token travels in the authorisation header and nowhere else: a token in a
	// URL is a token in an access log (§3.1).
	transport.mu.Lock()
	header := transport.header.Get(authorizationHeader)
	url := transport.url
	transport.mu.Unlock()
	if !strings.HasPrefix(strings.ToLower(header), strings.ToLower(bearerScheme)) {
		t.Errorf("authorisation header %q does not carry the expected scheme", header)
	}
	if len(header) <= len(bearerScheme) {
		t.Error("the authorisation header carries no token")
	}
	if strings.Contains(url, "token") || strings.Contains(url, header[len(bearerScheme):]) {
		t.Error("the device token appears in the URL")
	}
	if !strings.HasSuffix(url, wsAgentPath) {
		t.Errorf("dialled %q, want it to end in %q", url, wsAgentPath)
	}
	cancel()
	<-done
}

func TestServe_AHandshakeResultWithoutASessionIdIsRefused(t *testing.T) {
	result := okConnect()
	result.SessionID = ""
	transport := newFakeTransport(result)
	harness := newServeHarness(t, transport)

	_, err := harness.manager.handshake(context.Background(), transport, syntheticCredentials())
	if !errors.Is(err, ErrHandshakeRejected) {
		t.Fatalf("err = %v, want ErrHandshakeRejected", err)
	}
}

func TestServe_AHeartbeatTimeoutAtOrBelowTheIntervalIsRefused(t *testing.T) {
	// The deadline would expire before the next beat is due, so a healthy socket would look
	// dead and the agent would reconnect forever. The backend validates this too; both
	// halves check it because the one that acts on it is this one.
	result := okConnect()
	result.HeartbeatTimeout = 30
	transport := newFakeTransport(result)
	harness := newServeHarness(t, transport)

	_, err := harness.manager.handshake(context.Background(), transport, syntheticCredentials())
	if !errors.Is(err, ErrHandshakeRejected) {
		t.Fatalf("err = %v, want ErrHandshakeRejected for timeout <= interval", err)
	}
}

func TestServe_AStaleBundleAnnouncementIsRecorded(t *testing.T) {
	result := okConnect()
	result.BundleStale = true
	result.BundleDigest = "sha256:backend"
	transport := newFakeTransport(result)
	harness := newServeHarness(t, transport)

	info, err := harness.manager.handshake(context.Background(), transport, syntheticCredentials())
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	if !info.BundleStale {
		t.Error("the handshake did not report the bundle as stale")
	}
	if harness.bundle.Current() {
		t.Error("a stale announcement left the bundle marked current; mutations would be allowed")
	}
	if len(harness.bundle.observedDigests()) != 1 || harness.bundle.observedDigests()[0] != "sha256:backend" {
		t.Errorf("observed %v, want the backend digest recorded", harness.bundle.observedDigests())
	}
}

// ---------------------------------------------------------------------------------------
// §7.4 — the heartbeat, in both directions.
// ---------------------------------------------------------------------------------------

func TestServe_TheHeartbeatCarriesSeqUptimeAndQueueDepth(t *testing.T) {
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)
	harness.journal.setStats(JournalStats{Records: 3})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()

	waitFor(t, func() bool { return len(transport.frames("session.connect")) > 0 })
	harness.now.advance(45 * time.Second)
	harness.ticker.fire(harness.now.now())
	waitFor(t, func() bool { return len(transport.frames("session.heartbeat")) > 0 })

	frame := transport.frames("session.heartbeat")[0]
	if frame.ID != nil {
		t.Error("session.heartbeat was sent as a request; §7.3 does not correlate it")
	}
	var params map[string]any
	if err := json.Unmarshal(frame.Params, &params); err != nil {
		t.Fatalf("params: %v", err)
	}
	// seq_base was 7 and no envelope has arrived, so the reported seq is the base rather
	// than zero: §7.6 does not reset seq across reconnects.
	if params["seq"] != float64(7) {
		t.Errorf("seq = %v, want the handshake's seq_base 7", params["seq"])
	}
	if params["queue_depth"] != float64(3) {
		t.Errorf("queue_depth = %v, want the journal's 3", params["queue_depth"])
	}
	if params["uptime_seconds"] != float64(45) {
		t.Errorf("uptime_seconds = %v, want 45", params["uptime_seconds"])
	}
	cancel()
	<-done
}

func TestServe_AnUnansweredPingDropsTheSession(t *testing.T) {
	// THIS TEST USED TO ASSERT THE OPPOSITE OF THE PROTOCOL, and it is worth saying why rather than
	// quietly editing it. It was `TestServe_SilencePastTheTimeoutDropsTheSession`: it advanced the
	// clock 91 s past a 90 s timeout, fired the heartbeat tick and required the socket to close.
	//
	// That is not a rule §7.3 supports. `session.heartbeat` is a NOTIFICATION there — only
	// `command.result` is marked as correlated — and the hub's `_respond` correctly sends nothing for
	// a frame with a null id. So a healthy backend with no command to send transmits NOTHING to the
	// agent, and inbound silence is the ordinary idle state. Enforcing the timeout against it dropped
	// every live session on a 90-second cycle: the agent logged `heartbeat timeout; dropping the
	// session` while the hub logged a fresh `agent session connected` each time, and a command
	// dispatched near the boundary was delivered to a session already going away.
	//
	// §7.3 gives the 90 s rule to the HUB ("Missing for 90 s ⇒ the hub drops the session"). The agent
	// still has to notice a backend that stopped answering — a half-open socket accepts writes long
	// after the peer is gone — so the evidence it uses is a WebSocket PING, which is transport-level
	// and therefore adds no tenth JSON-RPC method. This test asserts that rule, and
	// `TestServe_SilenceAloneDoesNotDropTheSession` below asserts the case that used to fail.
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()

	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	// The far end stops answering pings. Nothing else changes: frames still flow nowhere, exactly as
	// in the healthy idle case, so the ping is the only thing that can distinguish the two.
	transport.failPings(errors.New("no pong: the peer is gone"))
	harness.ticker.fire(harness.now.now())

	waitFor(t, func() bool {
		transport.mu.Lock()
		defer transport.mu.Unlock()
		for _, c := range transport.closes {
			if c.reason == "heartbeat timeout" {
				return true
			}
		}
		return false
	})
	cancel()
	<-done
}

func TestServe_SilenceAloneDoesNotDropTheSession(t *testing.T) {
	// The regression this pair exists for. An idle backend answers pings and sends no frames; the
	// session must survive that indefinitely, because it is what a healthy connection looks like
	// whenever there is no work.
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()

	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	// Well past the timeout, and several heartbeat ticks. The clock is injected, so this measures
	// the rule rather than waiting.
	for range 3 {
		harness.now.advance(91 * time.Second)
		harness.ticker.fire(harness.now.now())
	}
	// Give the beat goroutine room to act on all three ticks before concluding it did not.
	waitFor(t, func() bool { return len(transport.frames("session.heartbeat")) >= 3 })

	transport.mu.Lock()
	closes := append([]closeRecord(nil), transport.closes...)
	transport.mu.Unlock()
	for _, c := range closes {
		if c.reason == "heartbeat timeout" {
			t.Fatal("an idle backend that answers pings must not be treated as dead: " +
				"§7.3 makes session.heartbeat a notification, so silence is the normal idle state")
		}
	}
	cancel()
	<-done
}

func TestServe_AnInboundFrameKeepsTheSessionAlive(t *testing.T) {
	// The control the timeout clause needs: without it, a manager that closed the socket on
	// every tick would pass the test above.
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("session.connect")) > 0 })

	harness.now.advance(80 * time.Second)
	transport.push(mustJSON(connection.Request{JSONRPC: "2.0", Method: "agent.status"}))
	time.Sleep(20 * time.Millisecond) // let the reader record the frame
	harness.ticker.fire(harness.now.now())
	time.Sleep(50 * time.Millisecond)

	transport.mu.Lock()
	closes := len(transport.closes)
	beats := 0
	for _, r := range transport.sent {
		if r.Method == "session.heartbeat" {
			beats++
		}
	}
	transport.mu.Unlock()
	if closes != 0 {
		t.Errorf("the session was closed %d time(s) despite a fresh inbound frame", closes)
	}
	if beats == 0 {
		t.Error("no heartbeat was sent on a live session")
	}
	cancel()
	<-done
}

// ---------------------------------------------------------------------------------------
// §10.3, D-41 — the journal drain and its gate.
// ---------------------------------------------------------------------------------------

func TestServe_TheDrainRunsAfterConnectAndMapsKindsToTheNineMethods(t *testing.T) {
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)
	harness.journal.records = []Record{
		{RecordID: "r1", Kind: KindCommandResult, Payload: mustJSON(map[string]any{"command_id": "c1"})},
		{RecordID: "r2", Kind: KindAgentStatus, Payload: mustJSON(map[string]any{"state": "idle"})},
		{RecordID: "r3", Kind: KindIntent, Payload: mustJSON(map[string]any{"reason": "offline edit"})},
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()

	waitFor(t, func() bool { return len(harness.journal.deliveredRecords()) == 3 })
	methods := transport.methods()
	if methods[0] != "session.connect" {
		t.Fatalf("the first frame was %q; the drain must not precede the handshake", methods[0])
	}
	want := []string{"session.connect", "command.result", "agent.status", "approval.request"}
	for i := range want {
		if methods[i] != want[i] {
			t.Fatalf("methods %v, want them to start %v -- an intent replays as approval.request (D-41)",
				methods, want)
		}
	}
	cancel()
	<-done
}

func TestServe_AStaleBundleHoldsTheIntentsAndDeliversTheRest(t *testing.T) {
	result := okConnect()
	result.BundleStale = true
	transport := newFakeTransport(result)
	harness := newServeHarness(t, transport)
	harness.journal.records = []Record{
		{RecordID: "r1", Kind: KindCommandResult, Payload: mustJSON(map[string]any{})},
		{RecordID: "r2", Kind: KindIntent, Payload: mustJSON(map[string]any{})},
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()

	waitFor(t, func() bool { return len(harness.journal.bundleFlags()) > 0 })
	waitFor(t, func() bool { return len(harness.journal.deliveredRecords()) == 1 })
	time.Sleep(30 * time.Millisecond)

	if harness.journal.bundleFlags()[0] {
		t.Error("the drain was told the bundle was current after a stale announcement")
	}
	if len(harness.journal.deliveredRecords()) != 1 || harness.journal.deliveredRecords()[0].Kind != KindCommandResult {
		t.Errorf("delivered %v; a stale bundle must hold the intent and pass the rest",
			harness.journal.deliveredRecords())
	}
	for _, method := range transport.methods() {
		if method == "approval.request" {
			t.Error("an intent was replayed against a stale bundle")
		}
	}
	cancel()
	<-done
}

func TestMethodForRecord_RefusesToInventATenthMethod(t *testing.T) {
	for kind, want := range map[RecordKind]string{
		KindCommandResult:   "command.result",
		KindCommandProgress: "command.progress",
		KindAgentStatus:     "agent.status",
		KindIntent:          "approval.request",
	} {
		got, err := methodForRecord(kind)
		if err != nil || got != want {
			t.Errorf("methodForRecord(%s) = %q, %v; want %q", kind, got, err, want)
		}
	}
	// The two kinds with no method in the closed catalogue must halt the drain rather than
	// be acknowledged: `Drain` truncates what the send function acknowledges, so a "success"
	// here would delete the record.
	for _, kind := range []RecordKind{KindScanBatch, KindSecretFindings} {
		if _, err := methodForRecord(kind); !errors.Is(err, ErrNoDeliverySurface) {
			t.Errorf("methodForRecord(%s) err = %v, want ErrNoDeliverySurface", kind, err)
		}
	}
	if _, err := methodForRecord(RecordKind("something.else")); !errors.Is(err, ErrUnknownRecordKind) {
		t.Errorf("an unknown kind returned %v, want ErrUnknownRecordKind", err)
	}
}

// ---------------------------------------------------------------------------------------
// §7.6, §10.4 — nothing reaches an operation unverified.
// ---------------------------------------------------------------------------------------

func TestServe_AVerifiedCommandReachesTheRunnerAndItsResultIsReported(t *testing.T) {
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)
	harness.runner.progress = true
	harness.runner.outcome = CommandOutcome{Status: "succeeded", Output: "2 files written"}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	transport.push(mustJSON(connection.Request{
		JSONRPC: "2.0", Method: "command.execute",
		Params: signedEnvelope(t, harness.creds, harness.digest, "cmd-1", 9, harness.now.now()),
	}))

	waitFor(t, func() bool { return len(transport.frames("command.result")) > 0 })
	var result map[string]any
	if err := json.Unmarshal(transport.frames("command.result")[0].Params, &result); err != nil {
		t.Fatalf("result params: %v", err)
	}
	if result["command_id"] != "cmd-1" || result["status"] != "succeeded" {
		t.Errorf("command.result = %v", result)
	}
	if len(transport.frames("command.progress")) == 0 {
		t.Error("no command.progress frame; the SSE fan-out has nothing to show")
	}

	// The heartbeat must now report the envelope's seq rather than the handshake's base:
	// §7.6's counter is per device and does not reset, so a heartbeat that under-reported it
	// would ask the backend to re-allocate a seq the agent has already seen.
	harness.ticker.fire(harness.now.now())
	waitFor(t, func() bool { return len(transport.frames("session.heartbeat")) > 0 })
	var beat map[string]any
	_ = json.Unmarshal(transport.frames("session.heartbeat")[0].Params, &beat)
	if beat["seq"] != float64(9) {
		t.Errorf("heartbeat seq = %v after an envelope at seq 9", beat["seq"])
	}
	cancel()
	<-done
}

func TestServe_ARefusedEnvelopeNeverReachesTheRunner(t *testing.T) {
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	// A real envelope whose args were changed after signing: the signature is well formed and
	// covers different bytes, which is what a tampered frame actually looks like.
	transport.push(mustJSON(connection.Request{
		JSONRPC: "2.0", Method: "command.execute",
		Params: tamperedEnvelope(t, harness.creds, harness.digest, "cmd-bad", 9, harness.now.now()),
	}))

	waitFor(t, func() bool { return len(transport.frames("agent.error")) > 0 })
	var payload map[string]any
	if err := json.Unmarshal(transport.frames("agent.error")[0].Params, &payload); err != nil {
		t.Fatalf("agent.error params: %v", err)
	}
	if payload["code"] != "envelope-signature-invalid" {
		t.Errorf("code = %v, want Appendix C.2's envelope-signature-invalid", payload["code"])
	}
	harness.runner.mu.Lock()
	started := len(harness.runner.started)
	harness.runner.mu.Unlock()
	if started != 0 {
		t.Errorf("the runner ran %d command(s) for a refused envelope", started)
	}
	if len(transport.frames("command.result")) != 0 {
		t.Error("a refused envelope produced a command.result")
	}
	cancel()
	<-done
}

func TestServe_WithNoVerifierEveryFrameIsRefused(t *testing.T) {
	// An agent assembled without a verifier must refuse, not pass. This is the one failure
	// the whole layer exists to make impossible, so it is asserted rather than assumed from
	// the constructor.
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)
	harness.manager.verifier = nil

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	transport.push(mustJSON(connection.Request{
		JSONRPC: "2.0", Method: "command.execute",
		Params: signedEnvelope(t, harness.creds, harness.digest, "cmd-1", 1, harness.now.now()),
	}))
	waitFor(t, func() bool { return len(transport.frames("agent.error")) > 0 })

	harness.runner.mu.Lock()
	started := len(harness.runner.started)
	harness.runner.mu.Unlock()
	if started != 0 {
		t.Error("a command ran with no envelope verifier wired")
	}
	cancel()
	<-done
}

func TestServe_AStaleBundleRefusesEveryMutation(t *testing.T) {
	result := okConnect()
	result.BundleStale = true
	transport := newFakeTransport(result)
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	transport.push(mustJSON(connection.Request{
		JSONRPC: "2.0", Method: "command.execute",
		Params: signedEnvelope(t, harness.creds, harness.digest, "cmd-1", 1, harness.now.now()),
	}))
	waitFor(t, func() bool { return len(transport.frames("agent.error")) > 0 })

	var payload map[string]any
	_ = json.Unmarshal(transport.frames("agent.error")[0].Params, &payload)
	if payload["code"] != "policy-bundle-stale" {
		t.Errorf("code = %v, want policy-bundle-stale", payload["code"])
	}
	harness.runner.mu.Lock()
	started := len(harness.runner.started)
	harness.runner.mu.Unlock()
	if started != 0 {
		t.Error("a mutation ran against a stale policy bundle")
	}
	cancel()
	<-done
}

func TestServe_CommandsRunOneAtATime(t *testing.T) {
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)
	harness.runner.release = make(chan struct{})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	for i, id := range []string{"cmd-1", "cmd-2"} {
		transport.push(mustJSON(connection.Request{
			JSONRPC: "2.0", Method: "command.execute",
			Params: signedEnvelope(t, harness.creds, harness.digest, id, int64(i+1), harness.now.now()),
		}))
	}
	waitFor(t, func() bool {
		harness.runner.mu.Lock()
		defer harness.runner.mu.Unlock()
		return len(harness.runner.started) == 1
	})

	// The reader must still be reading while a command is in flight -- that is what lets a
	// revocation close arrive during a long operation instead of after it.
	harness.now.advance(10 * time.Second)
	harness.ticker.fire(harness.now.now())
	waitFor(t, func() bool { return len(transport.frames("session.heartbeat")) > 0 })

	close(harness.runner.release)
	waitFor(t, func() bool {
		harness.runner.mu.Lock()
		defer harness.runner.mu.Unlock()
		return len(harness.runner.started) == 2
	})
	harness.runner.mu.Lock()
	maxSeen := harness.runner.maxSeen
	harness.runner.mu.Unlock()
	if maxSeen != 1 {
		t.Errorf("%d commands ran concurrently; two mutations of one tree would race", maxSeen)
	}
	cancel()
	<-done
}

// ---------------------------------------------------------------------------------------
// Appendix C.2 — revocation.
// ---------------------------------------------------------------------------------------

func TestServe_ARevocationWipesTheJournalAndTheCredentials(t *testing.T) {
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	transport.push(mustJSON(connection.Request{
		JSONRPC: "2.0", Method: "agent.error",
		Params: mustJSON(map[string]any{"code": "device-revoked", "message": "revoked by an operator"}),
	}))

	select {
	case err := <-done:
		if !errors.Is(err, ErrRevoked) {
			t.Fatalf("Serve returned %v, want ErrRevoked", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Serve did not return after a revocation")
	}

	if harness.journal.wipeCount() != 1 {
		t.Errorf("journal wiped %d time(s); a revoked principal's intents must not survive",
			harness.journal.wipeCount())
	}
	if _, err := harness.store.Load(context.Background()); !errors.Is(err, ErrNoCredentials) {
		t.Errorf("credentials still load after revocation: %v", err)
	}
}

func TestServe_ACloseOf4403IsRevocation(t *testing.T) {
	// The hub closes 4403 rather than sending a frame when it refuses mid-session (§3.1), so
	// the close code carries the meaning and the manager has to read it.
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	transport.end(websocket.CloseError{Code: 4403, Reason: "device revoked"})

	select {
	case err := <-done:
		if !errors.Is(err, ErrRevoked) {
			t.Fatalf("Serve returned %v, want ErrRevoked for a 4403 close", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Serve did not return after a 4403 close")
	}
	if harness.journal.wipeCount() != 1 {
		t.Error("the journal survived a 4403 close")
	}
}

func TestServe_AnOrdinaryCloseIsNotRevocation(t *testing.T) {
	// The control: without it, a manager that wiped credentials on every disconnect would
	// pass the two clauses above and destroy a healthy pairing on the first network blip.
	transport := newFakeTransport(okConnect())
	harness := newServeHarness(t, transport)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	transport.end(websocket.CloseError{Code: websocket.StatusGoingAway, Reason: "backend restart"})
	time.Sleep(50 * time.Millisecond)
	cancel()
	<-done

	if harness.journal.wipeCount() != 0 {
		t.Error("an ordinary close wiped the journal")
	}
	if _, err := harness.store.Load(context.Background()); err != nil {
		t.Errorf("an ordinary close destroyed the credentials: %v", err)
	}
}

// ---------------------------------------------------------------------------------------
// §7.6, Appendix C.2 — the measured clock skew (leaf 8.6).
// ---------------------------------------------------------------------------------------

// connectAtSkew builds a handshake result whose `server_time` puts the backend's clock the
// given offset BEHIND the agent's, so a positive offset means "the agent's clock is fast".
func connectAtSkew(now time.Time, agentAhead time.Duration) *connectResult {
	result := okConnect()
	result.ServerTime = now.Add(-agentAhead).Format(time.RFC3339Nano)
	return result
}

func TestClockSkewTolerance_MatchesTheVerifier(t *testing.T) {
	// Two copies of §7.6's ±60 s exist — this package's constant and the verifier's default —
	// and this is what stops them drifting. Without it the session could refuse at one bound
	// while the verifier refused at another, and the error an operator saw would depend on
	// which layer got there first.
	creds := syntheticCredentials()
	now := func() time.Time { return time.Unix(1899999900, 0).UTC() }
	verifier := newTestVerifier(t, creds, "sha256:local", now)
	if verifier.ClockSkew() != clockSkewTolerance {
		t.Errorf("the verifier tolerates %s and the session %s; they must agree",
			verifier.ClockSkew(), clockSkewTolerance)
	}
}

func TestServe_TheAgentStatusFrameReportsTheMeasuredSkew(t *testing.T) {
	harness := newServeHarness(t, newFakeTransport(nil))
	transport := harness.transport
	transport.mu.Lock()
	transport.connect = connectAtSkew(harness.now.now(), 5*time.Second)
	transport.mu.Unlock()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()

	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })
	var params map[string]any
	if err := json.Unmarshal(transport.frames("agent.status")[0].Params, &params); err != nil {
		t.Fatalf("agent.status params: %v", err)
	}
	if params["clock_skew_seconds"] != float64(5) {
		t.Errorf("clock_skew_seconds = %v, want 5 — signed, so an operator knows which way to move it",
			params["clock_skew_seconds"])
	}
	if params["clock_skew_within_bounds"] != true {
		t.Error("a 5 s skew was reported as out of bounds")
	}
	if params["state"] != "ready" {
		t.Errorf("state = %v for a healthy session, want ready", params["state"])
	}
	if params["policy_bundle_digest"] != "sha256:local" {
		t.Errorf("policy_bundle_digest = %v", params["policy_bundle_digest"])
	}

	// The same measurement has to reach `agent doctor`, which reads Status rather than frames.
	status, err := harness.manager.Status(ctx)
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if !status.ClockSkewMeasured || status.ClockSkew != 5*time.Second || status.ClockSkewBeyond {
		t.Errorf("Status carries skew=%s measured=%v beyond=%v; doctor cannot report what it cannot read",
			status.ClockSkew, status.ClockSkewMeasured, status.ClockSkewBeyond)
	}
	cancel()
	<-done
}

func TestServe_AClockOutsideToleranceRefusesTheCommandAndNamesTheClock(t *testing.T) {
	for _, tc := range []struct {
		name  string
		ahead time.Duration
	}{
		{name: "the agent's clock is four minutes fast", ahead: 4 * time.Minute},
		{name: "the agent's clock is four minutes slow", ahead: -4 * time.Minute},
	} {
		t.Run(tc.name, func(t *testing.T) {
			harness := newServeHarness(t, newFakeTransport(nil))
			transport := harness.transport
			transport.mu.Lock()
			transport.connect = connectAtSkew(harness.now.now(), tc.ahead)
			transport.mu.Unlock()

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan error, 1)
			go func() { done <- harness.manager.Serve(ctx) }()
			waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

			// A genuinely valid envelope. The refusal must come from the clock, not from
			// anything wrong with the frame — that is the whole point of Appendix C.2's
			// `clock-skew` row, which exists so the agent says "your clock is 4 minutes fast"
			// instead of "signature invalid".
			transport.push(mustJSON(connection.Request{
				JSONRPC: "2.0", Method: "command.execute",
				Params: signedEnvelope(t, harness.creds, harness.digest, "cmd-skew", 3, harness.now.now()),
			}))
			waitFor(t, func() bool { return len(transport.frames("agent.error")) > 0 })

			var payload map[string]any
			_ = json.Unmarshal(transport.frames("agent.error")[0].Params, &payload)
			if payload["code"] != "clock-skew" {
				t.Errorf("code = %v, want clock-skew", payload["code"])
			}
			if payload["command_id"] != "cmd-skew" {
				t.Errorf("command_id = %v; a refusal that cannot be correlated cannot be acted on",
					payload["command_id"])
			}
			harness.runner.mu.Lock()
			started := len(harness.runner.started)
			harness.runner.mu.Unlock()
			if started != 0 {
				t.Error("a command ran while the clock was outside tolerance")
			}

			var status map[string]any
			_ = json.Unmarshal(transport.frames("agent.status")[0].Params, &status)
			if status["state"] != "degraded" || status["clock_skew_within_bounds"] != false {
				t.Errorf("agent.status said state=%v within_bounds=%v; a backend reading `ready` "+
					"would keep sending commands this agent will refuse",
					status["state"], status["clock_skew_within_bounds"])
			}
			cancel()
			<-done
		})
	}
}

func TestServe_ASkewInsideToleranceStillExecutes(t *testing.T) {
	// The control. Without it the two clauses above would pass for an agent that refused every
	// command, and the refusal would look like a working skew check.
	harness := newServeHarness(t, newFakeTransport(nil))
	transport := harness.transport
	transport.mu.Lock()
	transport.connect = connectAtSkew(harness.now.now(), 59*time.Second)
	transport.mu.Unlock()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	transport.push(mustJSON(connection.Request{
		JSONRPC: "2.0", Method: "command.execute",
		Params: signedEnvelope(t, harness.creds, harness.digest, "cmd-ok", 3, harness.now.now()),
	}))
	waitFor(t, func() bool { return len(transport.frames("command.result")) > 0 })
	if len(transport.frames("agent.error")) != 0 {
		t.Errorf("a 59 s skew produced an error frame: %s", transport.frames("agent.error")[0].Params)
	}
	cancel()
	<-done
}

func TestServe_AnUnmeasuredSkewDoesNotRefuse(t *testing.T) {
	// A backend that omits `server_time` leaves the skew unmeasured, and an unmeasured skew is
	// within tolerance rather than outside it. Deliberately not fail-closed, and the reason is
	// narrow enough to be safe: the measurement comes from the handshake, so "not measured"
	// means the handshake has not completed, and no command can arrive before it has. Failing
	// closed here would refuse every command over a field this agent does not control.
	harness := newServeHarness(t, newFakeTransport(okConnect())) // okConnect carries no server_time
	transport := harness.transport

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- harness.manager.Serve(ctx) }()
	waitFor(t, func() bool { return len(transport.frames("agent.status")) > 0 })

	if _, measured := harness.manager.Skew(); measured {
		t.Error("a handshake with no server_time reported a measured skew")
	}
	transport.push(mustJSON(connection.Request{
		JSONRPC: "2.0", Method: "command.execute",
		Params: signedEnvelope(t, harness.creds, harness.digest, "cmd-1", 3, harness.now.now()),
	}))
	waitFor(t, func() bool { return len(transport.frames("command.result")) > 0 })
	cancel()
	<-done
}

// ---------------------------------------------------------------------------------------
// The two states before a session exists.
// ---------------------------------------------------------------------------------------

func TestServe_NoBackendURLIsDisabledAndNoTokenIsUnpaired(t *testing.T) {
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}

	disabled, err := NewManager("", Deps{Store: store, Identity: fakeIdentity{},
		Transport: func(*tls.Config) connection.Transport { return newFakeTransport(nil) }})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := disabled.Serve(context.Background()); !errors.Is(err, connection.ErrDisabled) {
		t.Errorf("no URL: Serve returned %v, want connection.ErrDisabled", err)
	}

	unpaired, err := NewManager("wss://backend.invalid", Deps{Store: store, Identity: fakeIdentity{},
		Transport: func(*tls.Config) connection.Transport { return newFakeTransport(nil) }})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := unpaired.Serve(context.Background()); !errors.Is(err, ErrUnpaired) {
		t.Errorf("no token: Serve returned %v, want ErrUnpaired", err)
	}
}

func TestSocketURL(t *testing.T) {
	for _, tc := range []struct {
		in      string
		want    string
		wantErr bool
	}{
		{in: "wss://api.example.com", want: "wss://api.example.com" + wsAgentPath},
		{in: "wss://api.example.com/", want: "wss://api.example.com" + wsAgentPath},
		{in: "wss://api.example.com" + wsAgentPath, want: "wss://api.example.com" + wsAgentPath},
		{in: "ws://localhost:8000", want: "ws://localhost:8000" + wsAgentPath},
		{in: "https://api.example.com", wantErr: true},
		{in: "api.example.com", wantErr: true},
	} {
		got, err := socketURL(tc.in)
		if tc.wantErr {
			if err == nil {
				t.Errorf("socketURL(%q) = %q, want an error", tc.in, got)
			}
			continue
		}
		if err != nil || got != tc.want {
			t.Errorf("socketURL(%q) = %q, %v; want %q", tc.in, got, err, tc.want)
		}
	}
}

// waitFor polls a condition with a bounded deadline.
//
// A poll rather than a sleep, because a fixed sleep is either flaky or slow and this suite
// has thirty of them. The failure message is the caller's assertion, so a timeout reads as
// the thing that did not happen.
func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("condition never held within the deadline")
}
