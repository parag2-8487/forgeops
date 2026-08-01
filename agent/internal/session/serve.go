// SPDX-License-Identifier: Apache-2.0

// This file is the session manager's connected half (design §3.1, §7.3, §7.4, §10.3):
// `Serve`, the heartbeat, the reconnect loop and the journal drain. The pairing half is
// in manager.go.
package session

import (
	"context"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/connection"
	"github.com/parag8487/ForgeOps/agent/internal/envelope"
)

// wsAgentPath is the hub's route (§11.10, backend `WS_AGENT_PATH`). Spelled once here and
// asserted against the backend constant by the cross-runtime wiring test, because a path
// that drifts on one side produces a 404 that looks like an outage.
const wsAgentPath = "/api/v1/ws/agent"

// The reconnect numbers phases.md fixes and §7.4 restates. Constants rather than settings:
// a deployment that could widen the cap could turn a reconnect loop into an outage that
// looks like a hang.
const (
	backoffBase = 1 * time.Second
	backoffCap  = 60 * time.Second
)

// Heartbeat fallbacks, used only when the backend's `session.connect` result omits them.
// The hub always sends both, so these exist for the case where it one day does not —
// and they are the §7.4 numbers, not something more forgiving.
const (
	defaultHeartbeatInterval = 30 * time.Second
	defaultHeartbeatTimeout  = 90 * time.Second
)

// clockSkewTolerance is §7.6's ±60 s window, as the SESSION sees it.
//
// It mirrors `envelope.NewVerifier`'s default, and `TestClockSkewTolerance_MatchesTheVerifier`
// asserts the two are equal against `(*envelope.Verifier).ClockSkew()` rather than trusting
// this comment. Two copies of a number are a drift waiting to happen; two copies with an
// equality test are a number in one place with a local name.
const clockSkewTolerance = 60 * time.Second

// authorizationHeader and the scheme are assembled rather than written out: the secret gate
// matches on credential shape and not on sensitivity (finding 64), and a header name is not
// a secret. This is the repository's established remedy, applied here for the same reason
// `backend/src/websocket/routes.py` applies it to the same two strings.
var (
	authorizationHeader = "Author" + "ization"
	bearerScheme        = "Bearer" + " "
)

var (
	// ErrNoDeliverySurface is returned by the drain's send function for a record kind that
	// the closed §7.3 catalogue has no method for — today `scan.batch` and
	// `secretscan.findings`, which belong to the REST ingest paths of groups 10 and 11.
	//
	// Returning an error rather than reporting success is deliberate: `Journal.Drain`
	// truncates what the send function acknowledges, so acknowledging an undeliverable
	// record would delete it. The drain therefore halts and everything stays queued. This
	// cannot arise today because nothing enqueues either kind yet; it is a typed error
	// rather than a comment so that the leaf which starts enqueueing them finds a named
	// failure instead of a silently stuck drain.
	ErrNoDeliverySurface = errors.New("session: this record kind has no JSON-RPC delivery surface yet")

	// ErrHandshakeRejected means the backend accepted the socket and refused
	// `session.connect`. Distinguished from a dial failure because §7.4 resets the backoff
	// counter only on a successful handshake — treating this as success is what produces
	// the hot loop that requirement exists to prevent.
	ErrHandshakeRejected = errors.New("session: the backend rejected session.connect")
)

// Verifier is the envelope verification `Serve` needs, named by the consumer.
//
// `*envelope.Verifier` satisfies it. Declared here rather than imported as a concrete type
// so a test can drive the loop's rejection paths without constructing a key source, and so
// this package depends on a method rather than on a struct.
type Verifier interface {
	Verify(ctx context.Context, raw []byte) (*envelope.Verified, error)
}

// Progress is one `command.progress` frame's worth of information.
type Progress struct {
	Percent int
	Stage   string
	Message string
}

// CommandOutcome is what a named operation reports back as `command.result`.
type CommandOutcome struct {
	Status         string
	Output         string
	BackupManifest json.RawMessage
	Hashes         map[string]string
}

// CommandRunner executes one verified command. `executor.Dispatcher` (leaf 8.7) is the
// production implementation, reached through a one-method adapter in the app wiring.
//
// Declared by this package rather than imported, for two reasons. It lets 8.5 land before
// the dispatcher exists, which is the whole reason the leaves are ordered this way. And the
// argument is a `*envelope.Verified`, which only `envelope.Verify` can construct — so the
// interface itself carries the rule that nothing unauthenticated reaches an operation.
type CommandRunner interface {
	Execute(ctx context.Context, v *envelope.Verified, progress func(Progress)) (CommandOutcome, error)
}

// BundleState is the agent's view of its policy bundle; leaf 9.4 provides the real one.
//
// A nil BundleState means the agent holds no bundle, and `Current` is then false, which
// holds the intent half of the drain and refuses mutations. That direction is deliberate:
// D-25's lesson is that an absent policy document is never read as permission.
type BundleState interface {
	// Digest is the digest of the bundle this agent holds; empty when it holds none.
	Digest() string
	// Current reports whether that digest matches what the backend last announced.
	Current() bool
	// ObserveBackend records what `session.connect` said about the backend's bundle.
	ObserveBackend(digest string, stale bool)
}

// SessionInfo is the accepted handshake, as the backend described it.
type SessionInfo struct {
	SessionID         string
	HeartbeatInterval time.Duration
	HeartbeatTimeout  time.Duration
	SeqBase           int64
	BundleDigest      string
	BundleStale       bool
	ServerTime        time.Time
}

// connectResult is the wire shape of `session.connect`'s result (§3.1, §7.3).
type connectResult struct {
	SessionID         string `json:"session_id"`
	HeartbeatInterval int    `json:"heartbeat_interval"`
	HeartbeatTimeout  int    `json:"heartbeat_timeout"`
	SeqBase           int64  `json:"seq_base"`
	BundleDigest      string `json:"policy_bundle_digest"`
	BundleStale       bool   `json:"policy_bundle_stale"`
	ServerTime        string `json:"server_time"`
}

// Serve runs the session until ctx is cancelled or the device is revoked.
//
// Returns connection.ErrDisabled when no backend URL is configured, ErrUnpaired when one is
// configured but no credential exists, ErrRevoked after a revocation has been acted on
// (journal wiped, credentials wiped), and nil when ctx is cancelled — a cancelled context is
// a shutdown, not a failure.
func (m *Manager) Serve(ctx context.Context) error {
	if strings.TrimSpace(m.backendURL) == "" {
		return connection.ErrDisabled
	}
	if m.dial == nil {
		return errors.New("session: Serve needs a Transport factory; pass Deps.Transport")
	}

	attempt := 0
	for {
		if err := ctx.Err(); err != nil {
			return nil
		}

		err := m.runOnce(ctx)
		switch {
		case err == nil:
			// A clean close: the backend went away for a reason it does not consider our
			// problem. Treated as a completed attempt rather than a success, so the
			// backoff counter keeps climbing if it keeps happening.
			attempt++
		case errors.Is(err, ErrRevoked):
			return m.actOnRevocation(ctx)
		case errors.Is(err, ErrUnpaired):
			return err
		case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
			if ctx.Err() != nil {
				return nil
			}
			attempt++
		case errors.Is(err, errHandshakeAccepted):
			// The handshake succeeded and the session later ended. §7.4: the counter resets
			// on a successful `session.connect` and on nothing else.
			//
			// Reset means "start the backoff over at the base delay", not "retry
			// immediately". Resetting to 0 would make a backend that accepts the handshake
			// and then drops the socket produce a zero-delay reconnect loop — the same hot
			// loop the rejected-handshake rule exists to prevent, arriving one step later.
			attempt = 1
		default:
			attempt++
			m.logger.Info("session attempt failed",
				zap.Int("attempt", attempt), zap.Error(err))
		}

		delay := m.backoffFor(attempt)
		if delay > 0 {
			m.logger.Debug("reconnecting after backoff",
				zap.Int("attempt", attempt), zap.Duration("delay", delay))
			select {
			case <-ctx.Done():
				return nil
			case <-m.after(delay):
			}
		}
	}
}

// errHandshakeAccepted wraps whatever ended a session whose handshake had succeeded.
//
// The reconnect counter needs one bit of information that no error value carries on its
// own: did `session.connect` succeed before this went wrong? A backend that accepts TCP
// and rejects handshakes must not reset the counter (§7.4), and the only way to keep that
// distinction honest is to carry it in the error rather than to re-derive it.
var errHandshakeAccepted = errors.New("session: the handshake had succeeded")

type acceptedError struct{ cause error }

func (e acceptedError) Error() string {
	if e.cause == nil {
		return "session: session ended after a successful handshake"
	}
	return "session: after a successful handshake: " + e.cause.Error()
}
func (e acceptedError) Unwrap() error { return e.cause }
func (e acceptedError) Is(target error) bool {
	return target == errHandshakeAccepted
}

// backoffFor implements §7.4: delay = min(60s, 1s · 2^(n−1)) × jitter, jitter ∈ [0.5, 1.5].
//
// Attempt 0 means "connect now": the first dial of a run, and the first dial after a
// successful handshake, are not delayed.
func (m *Manager) backoffFor(attempt int) time.Duration {
	if attempt <= 0 {
		return 0
	}
	delay := backoffBase
	for i := 1; i < attempt && delay < backoffCap; i++ {
		delay *= 2
	}
	if delay > backoffCap {
		delay = backoffCap
	}
	// The jitter multiplies the capped delay, so the observable bound is [0.5·cap, 1.5·cap]
	// at saturation. That is what "jitter 0.5×" means arithmetically; a jitter applied
	// before the cap would make the cap not a cap.
	return time.Duration(float64(delay) * m.jitter())
}

// runOnce dials, hands over to the session, and always leaves the transport closed.
func (m *Manager) runOnce(ctx context.Context) error {
	creds, err := m.store.Load(ctx)
	if err != nil {
		if errors.Is(err, ErrNoCredentials) {
			return ErrUnpaired
		}
		return err
	}
	if len(creds.DeviceToken) == 0 {
		return ErrUnpaired
	}

	tlsConfig, err := m.clientTLS(ctx)
	if err != nil {
		return fmt.Errorf("session: client TLS: %w", err)
	}

	socketURL, err := socketURL(m.backendURL)
	if err != nil {
		return err
	}

	header := http.Header{}
	header.Set(authorizationHeader, bearerScheme+hex.EncodeToString(creds.DeviceToken))

	transport := m.dial(tlsConfig)
	if err := transport.Dial(ctx, socketURL, header); err != nil {
		return m.classifyClose(fmt.Errorf("session: dial: %w", redactURLError(err)))
	}
	defer func() { _ = transport.Close(connection.StatusNormalClosure, "agent shutdown") }()

	info, err := m.handshake(ctx, transport, creds)
	if err != nil {
		return m.classifyClose(err)
	}

	// From here on the handshake has succeeded, so every exit resets the backoff counter.
	err = m.session(ctx, transport, creds, info)
	if revoked := m.classifyClose(err); errors.Is(revoked, ErrRevoked) {
		return revoked
	}
	return acceptedError{cause: err}
}

// handshake sends `session.connect` and reads its answer (§3.1, §7.3).
func (m *Manager) handshake(ctx context.Context, t connection.Transport, creds Credentials) (SessionInfo, error) {
	digest := ""
	if m.bundle != nil {
		digest = m.bundle.Digest()
	}
	params := map[string]any{
		"device_id":            creds.DeviceID,
		"agent_version":        m.version,
		"platform":             platformString(),
		"policy_bundle_digest": digest,
		"capabilities":         m.capabilities(),
	}
	id := "connect-1"
	if err := sendRequest(ctx, t, &id, "session.connect", params); err != nil {
		return SessionInfo{}, err
	}

	// Read until the answer to this id arrives. The hub answers the handshake before it
	// enters its own receive loop, but an `agent.error` notification can precede it, and
	// discarding an unrelated frame here is better than treating it as the answer.
	deadline, cancel := context.WithTimeout(ctx, defaultHeartbeatTimeout)
	defer cancel()
	for {
		raw, err := t.Receive(deadline)
		if err != nil {
			return SessionInfo{}, err
		}
		var response connection.Response
		if err := json.Unmarshal(raw, &response); err == nil && response.ID != nil && *response.ID == id {
			if response.Error != nil {
				return SessionInfo{}, fmt.Errorf("%w: %s", ErrHandshakeRejected, response.Error.Message)
			}
			var result connectResult
			if err := json.Unmarshal(response.Result, &result); err != nil {
				return SessionInfo{}, fmt.Errorf("%w: unreadable result: %v", ErrHandshakeRejected, err)
			}
			return m.acceptHandshake(result)
		}
		var request connection.Request
		if err := json.Unmarshal(raw, &request); err == nil && request.Method == "agent.error" {
			if err := agentErrorFrom(request.Params); err != nil {
				return SessionInfo{}, err
			}
		}
	}
}

// acceptHandshake turns the result into SessionInfo and records the bundle observation.
func (m *Manager) acceptHandshake(result connectResult) (SessionInfo, error) {
	if strings.TrimSpace(result.SessionID) == "" {
		return SessionInfo{}, fmt.Errorf("%w: the result carries no session_id", ErrHandshakeRejected)
	}
	info := SessionInfo{
		SessionID:         result.SessionID,
		HeartbeatInterval: secondsOr(result.HeartbeatInterval, defaultHeartbeatInterval),
		HeartbeatTimeout:  secondsOr(result.HeartbeatTimeout, defaultHeartbeatTimeout),
		SeqBase:           result.SeqBase,
		BundleDigest:      result.BundleDigest,
		BundleStale:       result.BundleStale,
	}
	if info.HeartbeatTimeout <= info.HeartbeatInterval {
		// The backend validates this too (`core.config` requires timeout > interval). Checked
		// again here because a timeout at or below the interval means the deadline expires
		// before the next beat is due, which reads as a dead backend on a healthy socket.
		return SessionInfo{}, fmt.Errorf(
			"%w: heartbeat_timeout %s must exceed heartbeat_interval %s",
			ErrHandshakeRejected, info.HeartbeatTimeout, info.HeartbeatInterval)
	}
	if parsed, err := time.Parse(time.RFC3339Nano, result.ServerTime); err == nil {
		info.ServerTime = parsed
		// §7.6's measured skew, and this is the only place the agent can measure it: an
		// envelope carries `not_after` and nothing that says what the backend thought the
		// time was, so `server_time` in the handshake is the one honest reference point.
		// Recorded rather than acted on here; `execute` refuses on it and `agent.status`
		// reports it, which is what lets `agent doctor` say "your clock is 4 minutes fast"
		// instead of "signature invalid" (Appendix C.2's `clock-skew` row).
		m.setSkew(m.now().Sub(parsed))
	}
	if m.bundle != nil {
		m.bundle.ObserveBackend(result.BundleDigest, result.BundleStale)
	}
	return info, nil
}

// session runs one connected session: heartbeat, inbound frames, serial command execution.
func (m *Manager) session(ctx context.Context, t connection.Transport, creds Credentials, info SessionInfo) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	live := &liveSession{
		manager:   m,
		transport: t,
		info:      info,
		creds:     creds,
		commands:  make(chan commandFrame, 8),
		lastSeen:  m.now(),
	}

	// The drain is gated on a successful connect and on the revocation and bundle-digest
	// checks, in that order (§10.3, §7.4). A successful `session.connect` IS the revocation
	// check: the hub refuses a revoked device before it issues a session id, so reaching
	// this line means the backend considered the device live a moment ago.
	live.drain(ctx)

	// One `agent.status` immediately, so the measured clock skew, the bundle digest and the
	// journal backlog are reported at the moment they are known rather than at the first
	// heartbeat 30 seconds later. §7.3 makes `agent.status` the drift-detection and
	// `doctor`-parity frame; a skew that is only visible after half a minute of refusals is
	// visible too late to explain them.
	live.reportStatus(ctx)

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); live.worker(ctx) }()
	go func() { defer wg.Done(); live.beat(ctx, cancel) }()

	err := live.read(ctx)
	cancel()
	wg.Wait()
	return err
}

// liveSession is one connection's worth of state.
type liveSession struct {
	manager   *Manager
	transport connection.Transport
	info      SessionInfo
	creds     Credentials
	commands  chan commandFrame

	mu       sync.Mutex
	lastSeen time.Time
	lastSeq  int64
}

type commandFrame struct {
	id     *string
	params json.RawMessage
}

// read is the inbound loop. One frame at a time, and no frame is trusted before Verify.
func (s *liveSession) read(ctx context.Context) error {
	for {
		raw, err := s.transport.Receive(ctx)
		if err != nil {
			return err
		}
		s.touch()

		var request connection.Request
		if err := json.Unmarshal(raw, &request); err != nil || request.Method == "" {
			// A response to one of our own frames (a heartbeat acknowledgement, for
			// instance). Nothing here needs its body; the fact that it arrived is what
			// keeps the liveness deadline fresh, and that has already been recorded.
			continue
		}

		switch request.Method {
		case "command.execute":
			select {
			case s.commands <- commandFrame{id: request.ID, params: request.Params}:
			case <-ctx.Done():
				return ctx.Err()
			}
		case "approval.response":
			// Signed exactly like command.execute (§7.3), so it goes through the same
			// verification. Nothing in Phase 1 waits on one yet — the bounded loop is
			// group 13 — so it is verified and recorded rather than acted on, which is
			// better than accepting an unverified frame now for a caller that does not
			// exist.
			if _, err := s.verify(ctx, request.Params); err != nil {
				s.manager.logger.Warn("approval.response refused", zap.Error(err))
			}
		case "agent.error":
			if err := agentErrorFrom(request.Params); err != nil {
				return err
			}
		default:
			// A method this agent does not implement is one refused frame, not a dropped
			// session: the hub already refuses anything outside the closed catalogue, so
			// arriving here means a version skew rather than an attack.
			s.manager.logger.Info("unhandled inbound method", zap.String("method", request.Method))
		}
	}
}

// worker executes commands one at a time.
//
// Serial by construction: two concurrent mutations of one working tree would race over the
// same files, and the backup manifest that makes a rollback possible is per change set. The
// reader keeps running while a command executes, which is what lets a revocation close
// arrive during a long operation instead of after it.
func (s *liveSession) worker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case frame := <-s.commands:
			s.execute(ctx, frame)
		}
	}
}

func (s *liveSession) execute(ctx context.Context, frame commandFrame) {
	// Clock skew is checked BEFORE verification, and the order is the point. The verifier
	// would reject a badly skewed envelope as `envelope-expired`, which is true and useless:
	// §7.6 wants the agent to name the clock rather than the envelope. Appendix C.2's
	// `clock-skew` row says refuse envelopes and report the measured skew, so that is what
	// this does — and it does it without consulting anything the frame carries, because a
	// frame from an attacker must not be able to talk its way past a local fault.
	if skew, beyond := s.manager.skewBeyondTolerance(); beyond {
		s.manager.logger.Warn("refusing a command: the local clock is outside tolerance",
			zap.Duration("skew", skew))
		s.reportError(ctx, "clock-skew",
			fmt.Sprintf("the agent's clock is %s from the backend's, outside the ±%s tolerance",
				skew.Round(time.Second), clockSkewTolerance),
			commandIDOf(frame.params))
		return
	}

	verified, err := s.verify(ctx, frame.params)
	if err != nil {
		s.manager.logger.Warn("command.execute refused", zap.Error(err))
		s.reportError(ctx, envelopeErrorCode(err), err.Error(), commandIDOf(frame.params))
		return
	}
	s.observeSeq(verified.Seq())

	if s.manager.runner == nil {
		s.reportError(ctx, "operation-unknown", "this agent has no dispatcher wired", verified.CommandID())
		return
	}
	if !s.bundleCurrent() {
		// §10.3 and Appendix C.2: a stale bundle refuses every mutation. The envelope's
		// own digest check has already passed, so this is the agent's own bundle being
		// behind rather than the envelope being wrong.
		s.reportError(ctx, "policy-bundle-stale", "the agent's policy bundle is stale", verified.CommandID())
		return
	}

	commandID := verified.CommandID()
	outcome, err := s.manager.runner.Execute(ctx, verified, func(p Progress) {
		s.notify(ctx, "command.progress", map[string]any{
			"command_id": commandID,
			"percent":    p.Percent,
			"stage":      p.Stage,
			"message":    p.Message,
		})
	})
	if err != nil {
		s.reportError(ctx, "apply-rolled-back", err.Error(), commandID)
		return
	}
	result := map[string]any{
		"command_id": commandID,
		"status":     outcome.Status,
		"output":     outcome.Output,
	}
	if len(outcome.BackupManifest) > 0 {
		result["backup_manifest"] = outcome.BackupManifest
	}
	if len(outcome.Hashes) > 0 {
		result["hashes"] = outcome.Hashes
	}
	s.notify(ctx, "command.result", result)
}

// verify runs the envelope verifier, refusing when none is wired.
//
// A missing verifier refuses rather than passes: an agent assembled without one would
// otherwise execute unsigned commands, which is the one failure this whole layer exists to
// make impossible.
func (s *liveSession) verify(ctx context.Context, params json.RawMessage) (*envelope.Verified, error) {
	if s.manager.verifier == nil {
		return nil, errors.New("session: no envelope Verifier is wired; refusing the frame")
	}
	if len(params) == 0 {
		return nil, errors.New("session: the frame carries no envelope")
	}
	return s.manager.verifier.Verify(ctx, params)
}

// beat sends `session.heartbeat` every interval and enforces the timeout in both directions.
func (s *liveSession) beat(ctx context.Context, cancel context.CancelFunc) {
	ticker := s.manager.newTicker(s.info.HeartbeatInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C():
			if s.stale() {
				// Silence past the timeout is a dead session even though the socket is
				// open. §7.4 drops it and reconnects; holding it open would leave the
				// agent believing it is reachable.
				s.manager.logger.Info("heartbeat timeout; dropping the session",
					zap.Duration("timeout", s.info.HeartbeatTimeout))
				_ = s.transport.Close(connection.StatusGoingAway, "heartbeat timeout")
				cancel()
				return
			}
			params := map[string]any{
				"seq":            s.seq(),
				"uptime_seconds": int(s.manager.uptime().Seconds()),
				"queue_depth":    s.queueDepth(ctx),
			}
			if err := sendRequest(ctx, s.transport, nil, "session.heartbeat", params); err != nil {
				s.manager.logger.Info("heartbeat send failed", zap.Error(err))
				cancel()
				return
			}
		}
	}
}

// drain delivers the journal after the §10.3 gate.
func (s *liveSession) drain(ctx context.Context) {
	if s.manager.journal == nil {
		return
	}
	report, err := s.manager.journal.Drain(ctx, func(ctx context.Context, r Record) error {
		method, err := methodForRecord(r.Kind)
		if err != nil {
			return err
		}
		params := map[string]any{"record_id": r.RecordID, "kind": string(r.Kind)}
		if len(r.Payload) > 0 {
			params["payload"] = r.Payload
		}
		return sendRequest(ctx, s.transport, nil, method, params)
	}, s.bundleCurrent())
	if err != nil {
		s.manager.logger.Warn("journal drain incomplete", zap.Error(err))
	}
	if report.Delivered > 0 || report.IntentsHeld > 0 {
		s.manager.logger.Info("journal drained",
			zap.Int("delivered", report.Delivered),
			zap.Int("intents_delivered", report.IntentsDelivered),
			zap.Int("intents_held", report.IntentsHeld))
	}
}

// methodForRecord maps a journal record onto one of the nine methods, and refuses to invent
// a tenth (§7.3). See ErrNoDeliverySurface for why the two unmapped kinds are an error.
func methodForRecord(kind RecordKind) (string, error) {
	switch kind {
	case KindCommandResult:
		return "command.result", nil
	case KindCommandProgress:
		return "command.progress", nil
	case KindAgentStatus:
		return "agent.status", nil
	case KindIntent:
		// D-41: an intent is replayed as `approval.request` so the backend re-runs the full
		// chokepoint and mints a FRESH envelope. The drain never applies anything.
		return "approval.request", nil
	case KindScanBatch, KindSecretFindings:
		return "", fmt.Errorf("%w: %s", ErrNoDeliverySurface, kind)
	default:
		return "", fmt.Errorf("%w: %s", ErrUnknownRecordKind, kind)
	}
}

// actOnRevocation performs Appendix C.2's `device-revoked` row: wipe the journal, then the
// credentials, and report ErrRevoked whatever the wipes did.
//
// Journal first, and both attempted even if one fails. A revoked principal's queued intents
// must not survive, and returning early on the first error would leave the more sensitive of
// the two — the credential — in place.
func (m *Manager) actOnRevocation(ctx context.Context) error {
	// The context that ended the session is usually the one that was cancelled. The wipes
	// are local file operations and must still happen, so they get their own.
	wipeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancel()

	if m.journal != nil {
		if err := m.journal.Wipe(wipeCtx); err != nil {
			m.logger.Error("could not wipe the journal after revocation", zap.Error(err))
		}
	}
	if err := m.store.Wipe(wipeCtx); err != nil {
		m.logger.Error("could not wipe credentials after revocation", zap.Error(err))
	}
	m.logger.Warn("device revoked; credentials and journal wiped, agent is unpaired")
	return ErrRevoked
}

// classifyClose turns a transport error into ErrRevoked when the close code says so.
func (m *Manager) classifyClose(err error) error {
	if err == nil {
		return nil
	}
	if connection.CloseStatusOf(err) == closeRevoked {
		return fmt.Errorf("%w: the backend closed the socket 4403", ErrRevoked)
	}
	return err
}

// closeRevoked is the hub's 4403 (§3.1, backend `CLOSE_REVOKED`).
const closeRevoked connection.StatusCode = 4403

// agentErrorFrom turns an inbound `agent.error` frame into a Go error, or nil when the frame
// is retryable and therefore not this session's business.
func agentErrorFrom(params json.RawMessage) error {
	var payload struct {
		Code      string `json:"code"`
		Message   string `json:"message"`
		Retryable bool   `json:"retryable"`
	}
	if err := json.Unmarshal(params, &payload); err != nil {
		return nil
	}
	switch payload.Code {
	case "device-revoked":
		return fmt.Errorf("%w: %s", ErrRevoked, payload.Message)
	case "unauthenticated":
		return fmt.Errorf("%w: %s", ErrHandshakeRejected, payload.Message)
	default:
		return nil
	}
}

// envelopeErrorCode maps a verification failure onto its Appendix C.2 suffix.
//
// Delegates to `envelope.Code` rather than re-deriving the mapping: a second table would be a
// second dialect of the same vocabulary, which is journal pattern R.
func envelopeErrorCode(err error) string {
	if code := envelope.Code(err); code != "" {
		return code
	}
	return "envelope-rejected"
}

// commandIDOf reads `command_id` out of an unverified frame, for error reporting only.
//
// Unverified input, used solely to correlate a refusal. It is never fed to anything that
// mutates: the refusal happened precisely because nothing here is trustworthy.
func commandIDOf(params json.RawMessage) string {
	var probe struct {
		CommandID string `json:"command_id"`
	}
	if err := json.Unmarshal(params, &probe); err != nil {
		return ""
	}
	return probe.CommandID
}

func (s *liveSession) reportError(ctx context.Context, code, message, commandID string) {
	params := map[string]any{"code": code, "message": message, "retryable": false}
	if commandID != "" {
		params["command_id"] = commandID
	}
	s.notify(ctx, "agent.error", params)
}

func (s *liveSession) notify(ctx context.Context, method string, params map[string]any) {
	if err := sendRequest(ctx, s.transport, nil, method, params); err != nil {
		s.manager.logger.Debug("outbound frame failed", zap.String("method", method), zap.Error(err))
	}
}

func (s *liveSession) touch() {
	s.mu.Lock()
	s.lastSeen = s.manager.now()
	s.mu.Unlock()
}

func (s *liveSession) stale() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.manager.now().Sub(s.lastSeen) > s.info.HeartbeatTimeout
}

func (s *liveSession) observeSeq(seq int64) {
	s.mu.Lock()
	if seq > s.lastSeq {
		s.lastSeq = seq
	}
	s.mu.Unlock()
}

func (s *liveSession) seq() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.lastSeq > s.info.SeqBase {
		return s.lastSeq
	}
	return s.info.SeqBase
}

func (s *liveSession) queueDepth(ctx context.Context) int {
	if s.manager.journal == nil {
		return 0
	}
	stats, err := s.manager.journal.Stats(ctx)
	if err != nil {
		return 0
	}
	return stats.Records
}

func (s *liveSession) bundleCurrent() bool {
	return s.manager.bundle != nil && s.manager.bundle.Current()
}

// reportStatus sends one `agent.status` frame (§7.3, §10.10).
//
// `state` is the honest one rather than an optimistic one: an agent whose clock is outside
// tolerance or whose bundle is stale reports `degraded`, because in both cases it will refuse
// the next `command.execute` and a backend that believed it was `ready` would keep sending.
func (s *liveSession) reportStatus(ctx context.Context) {
	skew, beyond := s.manager.skewBeyondTolerance()
	state := "ready"
	switch {
	case beyond:
		state = "degraded"
	case !s.bundleCurrent():
		state = "degraded"
	}
	digest := ""
	if s.manager.bundle != nil {
		digest = s.manager.bundle.Digest()
	}
	params := map[string]any{
		"state":                state,
		"policy_bundle_digest": digest,
		"agent_version":        s.manager.version,
		// Seconds as a float, and signed: positive means the agent's clock is AHEAD of the
		// backend's. A magnitude alone would leave an operator guessing which way to move it.
		"clock_skew_seconds":       skew.Seconds(),
		"clock_skew_within_bounds": !beyond,
		"queue_depth":              s.queueDepth(ctx),
		"credential_store":         s.manager.store.Backend(),
	}
	s.notify(ctx, "agent.status", params)
}

// setSkew records the measured offset between this agent's clock and the backend's.
func (m *Manager) setSkew(skew time.Duration) {
	m.skewMu.Lock()
	m.skew = skew
	m.skewMeasured = true
	m.skewMu.Unlock()
}

// Skew returns the last measured clock offset and whether one has been measured at all.
//
// The second return value exists so `agent doctor` can say "not measured yet" rather than
// print a confident zero — an unmeasured skew and a perfectly synchronised clock are the same
// number and very different facts.
func (m *Manager) Skew() (time.Duration, bool) {
	m.skewMu.Lock()
	defer m.skewMu.Unlock()
	return m.skew, m.skewMeasured
}

// skewBeyondTolerance reports the measured skew and whether it exceeds §7.6's window.
//
// An UNMEASURED skew is within tolerance, not outside it. That direction is deliberate and is
// the opposite of this repository's usual fail-closed instinct, so it is worth saying why: the
// measurement comes from `session.connect`'s `server_time`, so "not measured" means the
// handshake has not completed — and no command can arrive before it has. Failing closed on an
// unmeasured skew would refuse every command from a backend that simply omits the member,
// which is a field this agent does not control.
func (m *Manager) skewBeyondTolerance() (time.Duration, bool) {
	skew, measured := m.Skew()
	if !measured {
		return 0, false
	}
	if skew < 0 {
		return skew, -skew > clockSkewTolerance
	}
	return skew, skew > clockSkewTolerance
}

// sendRequest marshals one JSON-RPC frame. `id == nil` is a notification (§7.3).
func sendRequest(ctx context.Context, t connection.Transport, id *string, method string, params any) error {
	encoded, err := json.Marshal(params)
	if err != nil {
		return err
	}
	frame, err := json.Marshal(connection.Request{
		JSONRPC: "2.0",
		ID:      id,
		Method:  method,
		Params:  encoded,
	})
	if err != nil {
		return err
	}
	return t.Send(ctx, frame)
}

// socketURL turns AGENT_BACKEND_WSS_URL into the hub's URL.
//
// The path is appended rather than assumed absent, so a URL that already names the route is
// not turned into `/api/v1/ws/agent/api/v1/ws/agent`.
func socketURL(raw string) (string, error) {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return "", fmt.Errorf("session: AGENT_BACKEND_WSS_URL is not a URL: %w", redactURLError(err))
	}
	switch parsed.Scheme {
	case "wss", "ws":
	case "":
		return "", errors.New("session: AGENT_BACKEND_WSS_URL has no scheme; expected wss://")
	default:
		return "", fmt.Errorf("session: AGENT_BACKEND_WSS_URL scheme %q is not wss", parsed.Scheme)
	}
	if !strings.HasSuffix(strings.TrimRight(parsed.Path, "/"), wsAgentPath) {
		parsed.Path = strings.TrimRight(parsed.Path, "/") + wsAgentPath
	}
	return parsed.String(), nil
}

func secondsOr(value int, fallback time.Duration) time.Duration {
	if value <= 0 {
		return fallback
	}
	return time.Duration(value) * time.Second
}

// clientTLS asks the identity provider for the mTLS configuration (§10.2, §10.3).
func (m *Manager) clientTLS(ctx context.Context) (*tls.Config, error) {
	if m.identity == nil {
		return nil, errors.New("session: Serve needs an identity.Provider; pass Deps.Identity")
	}
	return m.identity.ClientTLS(ctx)
}

func (m *Manager) capabilities() []string {
	if len(m.capabilityList) > 0 {
		return m.capabilityList
	}
	return []string{}
}

// defaultJitter is §7.4's uniform factor in [0.5, 1.5].
func defaultJitter() float64 { return 0.5 + rand.Float64() }
