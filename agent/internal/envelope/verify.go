// SPDX-License-Identifier: Apache-2.0

package envelope

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// Verified is proof that an envelope passed every check in Verify.
//
// The guarantee, stated as a property of the language rather than of a convention: the
// fields are unexported, there is no exported constructor, and no method mutates. So a
// `*Verified` in a signature means "somebody verified this", and it means it because
// there is no other way to obtain one. That is why the mutation boundary
// (`executor/internal/mutate`) takes one — a mutation without a governance-signed
// envelope is a compile error, not a review miss (§2.2.1, D-45, D-59).
//
// Deliberately NOT an interface. An interface could be satisfied by any type in any
// package, including a test double whose Operation() returns whatever a caller wanted,
// and that would delete the only thing this type is for. D-59 rejects the interface form
// explicitly and records why.
type Verified struct {
	env        Envelope
	verifiedAt time.Time
	digest     string
}

// Operation returns the named operation to dispatch (§7.7).
func (v *Verified) Operation() Operation { return v.env.Operation }

// ApprovalID returns the approval this command was minted under.
func (v *Verified) ApprovalID() string { return v.env.ApprovalID }

// PolicyContext returns the bundle digest and decision the backend evaluated against.
func (v *Verified) PolicyContext() PolicyContext { return v.env.PolicyContext }

// Args returns the operation arguments as raw JSON.
//
// Raw rather than decoded, for the reason canonicalBody gives: decoding to `any` turns
// every number into a float64, and §7.6 forbids floats in an envelope. Each operation
// handler unmarshals into its own typed struct.
func (v *Verified) Args() json.RawMessage { return v.env.Args }

// CommandID returns the backend-assigned command identifier, used to correlate
// `command.result` and `command.progress` with the transit that produced them.
func (v *Verified) CommandID() string { return v.env.CommandID }

// DeviceID returns the device the envelope was addressed to.
func (v *Verified) DeviceID() string { return v.env.DeviceID }

// Seq returns the per-device sequence number that was accepted.
func (v *Verified) Seq() int64 { return v.env.Seq }

// Digest returns the hex SHA-256 of the signing input that verified.
//
// The audit row and `MutationAuthority.envelope_digest` carry this value, so the record
// names the exact bytes that were signed rather than a re-serialisation of them.
func (v *Verified) Digest() string { return v.digest }

// VerifiedAt returns when verification succeeded, from the Verifier's clock.
func (v *Verified) VerifiedAt() time.Time { return v.verifiedAt }

// ReplayGuard holds the per-device replay state: the `seq` high-water mark and the
// bounded nonce set (§7.6's ordering and uniqueness conditions).
//
// An interface because §7.6 makes the backend's copy Redis-authoritative while the
// agent's is an in-process bounded LRU, and because leaf 8.6 replaces group 7's
// implementation without touching Verify. Both methods take the device id, because one
// agent process may hold credentials for one device today and the invariant is per
// device, not per process.
type ReplayGuard interface {
	// SeenNonce reports whether nonce was already accepted for deviceID, and records it
	// when it was not. One call, so the check and the record cannot be separated by a
	// concurrent second envelope carrying the same nonce.
	SeenNonce(ctx context.Context, deviceID, nonce string) (bool, error)

	// AdvanceSeq accepts seq only when it is strictly greater than the stored high-water
	// mark, and stores it atomically when it accepts. Returns false without storing
	// otherwise.
	AdvanceSeq(ctx context.Context, deviceID string, seq int64) (bool, error)
}

// BundleDigestSource reports the digest of the policy bundle this agent currently holds.
//
// Separate from ReplayGuard because it has a different lifetime: replay state changes on
// every message, the bundle changes when the backend publishes one. Q-07 is the property
// that this comparison is a rejection and not a warning.
type BundleDigestSource interface {
	BundleDigest(ctx context.Context) (string, error)
}

// KeySource supplies the per-device envelope HMAC key.
//
// An interface so the key can live in the OS keychain (§10.3's Store) without this
// package importing `session`, which would reintroduce D-59's cycle from the other side.
type KeySource interface {
	EnvelopeKey(ctx context.Context, deviceID string) ([]byte, error)
}

// Verifier performs §10.4's six checks, in §10.4's order.
type Verifier struct {
	keys         KeySource
	replay       ReplayGuard
	bundle       BundleDigestSource
	maxAge       time.Duration
	clockSkew    time.Duration
	now          func() time.Time
	domainPrefix string
}

// VerifierOption adjusts a Verifier. Only the genuinely deployment-dependent values are
// options; nothing that would weaken a check is settable.
type VerifierOption func(*Verifier)

// WithClock replaces the clock, for tests that need a fixed `now`.
func WithClock(now func() time.Time) VerifierOption {
	return func(v *Verifier) { v.now = now }
}

// WithMaxAge sets ENVELOPE_MAX_AGE_SECONDS (§7.6, default 300s).
func WithMaxAge(d time.Duration) VerifierOption {
	return func(v *Verifier) { v.maxAge = d }
}

// WithClockSkew sets the tolerated skew (§10.4 tolerates ±60s and reports it).
func WithClockSkew(d time.Duration) VerifierOption {
	return func(v *Verifier) { v.clockSkew = d }
}

// NewVerifier constructs a Verifier and REFUSES to construct an incomplete one.
//
// Every collaborator is required. A Verifier with a nil ReplayGuard would silently skip
// §7.6's ordering and uniqueness conditions while still returning a `*Verified` — which
// is precisely the "partial control that looks like a control" this package exists to
// prevent. Refusing at construction means the incomplete configuration cannot exist,
// rather than being detected later by a test somebody has to remember to write.
func NewVerifier(
	keys KeySource,
	replay ReplayGuard,
	bundle BundleDigestSource,
	options ...VerifierOption,
) (*Verifier, error) {
	if keys == nil {
		return nil, errors.New("envelope: a Verifier requires a KeySource")
	}
	if replay == nil {
		return nil, ErrNoReplayGuard
	}
	if bundle == nil {
		return nil, ErrNoBundle
	}
	v := &Verifier{
		keys:         keys,
		replay:       replay,
		bundle:       bundle,
		maxAge:       300 * time.Second,
		clockSkew:    60 * time.Second,
		now:          time.Now,
		domainPrefix: DomainPrefix,
	}
	for _, option := range options {
		option(v)
	}
	if v.maxAge <= 0 {
		return nil, errors.New("envelope: maxAge must be positive")
	}
	return v, nil
}

// Verify parses and checks raw, returning the only value that proves it passed.
//
// The order is §10.4's, and it short-circuits on the first failure:
//
//  1. schema      — required members present, `v` known, no unknown members, seq and
//     not_after integral, no float anywhere;
//  2. freshness   — now <= not_after, and not_after - now <= maxAge, with clockSkew
//     tolerated;
//  3. signature   — constant-time compare of HMAC-SHA256(key, prefix||0x00||JCS(e));
//  4. ordering    — seq > lastSeq for this device, then lastSeq = seq atomically;
//  5. uniqueness  — nonce unseen in a set covering at least maxAge;
//  6. policy bind — policy_context.bundle_digest == the loaded bundle's digest.
//
// Why 3 precedes 4 and 5, which is the part that is easy to get backwards: verifying
// order first would let an UNAUTHENTICATED attacker advance a device's seq high-water
// mark or burn a nonce, locking out the real backend. A denial of service through a
// check that was supposed to be a defence. Q-15's negative control is exactly this
// inversion.
//
// No failure path returns a non-nil *Verified, so no failure can reach the executor.
func (v *Verifier) Verify(ctx context.Context, raw []byte) (*Verified, error) {
	env, err := v.parse(raw)
	if err != nil {
		return nil, err
	}
	if err := v.checkFreshness(env); err != nil {
		return nil, err
	}
	digest, err := v.checkSignature(ctx, env)
	if err != nil {
		return nil, err
	}
	if err := v.checkOrdering(ctx, env); err != nil {
		return nil, err
	}
	if err := v.checkUniqueness(ctx, env); err != nil {
		return nil, err
	}
	if err := v.checkPolicyBinding(ctx, env); err != nil {
		return nil, err
	}
	return &Verified{env: env, verifiedAt: v.now(), digest: digest}, nil
}

// parse implements check 1.
//
// `DisallowUnknownFields` is the load-bearing call. Without it an envelope carrying an
// extra member would parse, be canonicalised over the KNOWN members only, and verify —
// so an attacker could append arbitrary content to a signed envelope and have it accepted
// as authentic. The canonical form covers a closed member set, so the parser must reject
// anything outside it rather than ignore it.
func (v *Verifier) parse(raw []byte) (Envelope, error) {
	var env Envelope
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	decoder.UseNumber()
	if err := decoder.Decode(&env); err != nil {
		return Envelope{}, fmt.Errorf("%w: %v", ErrSchema, err)
	}
	if decoder.More() {
		return Envelope{}, fmt.Errorf("%w: trailing content after the envelope object", ErrSchema)
	}
	if env.V != Version {
		return Envelope{}, fmt.Errorf("%w: v is %q, expected %q", ErrSchema, env.V, Version)
	}
	for name, value := range map[string]string{
		"command_id":                   env.CommandID,
		"device_id":                    env.DeviceID,
		"operation":                    string(env.Operation),
		"approval_id":                  env.ApprovalID,
		"nonce":                        env.Nonce,
		"policy_context.bundle_digest": env.PolicyContext.BundleDigest,
		"signature":                    env.Signature,
	} {
		if value == "" {
			return Envelope{}, fmt.Errorf("%w: %s is required and empty", ErrSchema, name)
		}
	}
	if env.Seq <= 0 {
		return Envelope{}, fmt.Errorf("%w: seq must be a positive integer, got %d", ErrSchema, env.Seq)
	}
	if env.NotAfter <= 0 {
		return Envelope{}, fmt.Errorf("%w: not_after must be a positive integer, got %d", ErrSchema, env.NotAfter)
	}
	if err := requireNoFloat(env.Args); err != nil {
		return Envelope{}, err
	}
	return env, nil
}

// checkFreshness implements check 2.
func (v *Verifier) checkFreshness(env Envelope) error {
	now := v.now()
	notAfter := time.Unix(env.NotAfter, 0).UTC()
	if now.After(notAfter.Add(v.clockSkew)) {
		return fmt.Errorf("%w: not_after %s is behind now %s", ErrExpired,
			notAfter.Format(time.RFC3339), now.UTC().Format(time.RFC3339))
	}
	// The upper bound is what stops a long-lived envelope. D-41 item 1 turns on it: an
	// envelope cannot be queued across an outage precisely because extending this bound
	// would widen the replay window it exists to close.
	if notAfter.Sub(now) > v.maxAge+v.clockSkew {
		return fmt.Errorf("%w: not_after %s is %s ahead of now, limit is %s", ErrTooFarFuture,
			notAfter.Format(time.RFC3339), notAfter.Sub(now), v.maxAge)
	}
	return nil
}

// checkSignature implements check 3 and returns the verified digest.
func (v *Verifier) checkSignature(ctx context.Context, env Envelope) (string, error) {
	key, err := v.keys.EnvelopeKey(ctx, env.DeviceID)
	if err != nil {
		return "", fmt.Errorf("%w: no envelope key for device %s: %v", ErrSignature, env.DeviceID, err)
	}
	if len(key) == 0 {
		return "", fmt.Errorf("%w: the envelope key for device %s is empty", ErrSignature, env.DeviceID)
	}

	unsigned := env
	unsigned.Signature = ""
	input, err := SigningInput(v.domainPrefix, unsigned)
	if err != nil {
		return "", err
	}

	presented, err := DecodeSignature(env.Signature)
	if err != nil {
		return "", err
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(input)
	expected := mac.Sum(nil)

	// Constant time. A byte-by-byte compare that returns early leaks how many leading
	// bytes matched, which is enough to forge a MAC one byte at a time given enough
	// attempts.
	if !hmac.Equal(presented, expected) {
		return "", ErrSignature
	}
	sum := sha256.Sum256(input)
	return hex.EncodeToString(sum[:]), nil
}

// checkOrdering implements check 4.
func (v *Verifier) checkOrdering(ctx context.Context, env Envelope) error {
	advanced, err := v.replay.AdvanceSeq(ctx, env.DeviceID, env.Seq)
	if err != nil {
		return fmt.Errorf("%w: seq state unavailable: %v", ErrReplaySeq, err)
	}
	if !advanced {
		return fmt.Errorf("%w: seq %d", ErrReplaySeq, env.Seq)
	}
	return nil
}

// checkUniqueness implements check 5.
func (v *Verifier) checkUniqueness(ctx context.Context, env Envelope) error {
	seen, err := v.replay.SeenNonce(ctx, env.DeviceID, env.Nonce)
	if err != nil {
		return fmt.Errorf("%w: nonce state unavailable: %v", ErrReplayNonce, err)
	}
	if seen {
		return ErrReplayNonce
	}
	return nil
}

// checkPolicyBinding implements check 6.
//
// A rejection, never a warning. Q-07's negative control is downgrading it, and the
// property must then fail: an agent that applies a mutation authorised against a bundle
// it no longer holds has defeated the double evaluation entirely.
func (v *Verifier) checkPolicyBinding(ctx context.Context, env Envelope) error {
	loaded, err := v.bundle.BundleDigest(ctx)
	if err != nil {
		return fmt.Errorf("%w: the loaded bundle digest is unavailable: %v", ErrPolicyStale, err)
	}
	if loaded == "" {
		return fmt.Errorf("%w: this agent holds no policy bundle", ErrPolicyStale)
	}
	if loaded != env.PolicyContext.BundleDigest {
		return fmt.Errorf("%w: envelope names %s, this agent holds %s",
			ErrPolicyStale, env.PolicyContext.BundleDigest, loaded)
	}
	return nil
}

// Code maps an error from this package to its RFC 9457 suffix / `agent.error` code
// (Appendix C.1, C.2), so a caller reports a diagnosis rather than a category.
func Code(err error) string {
	switch {
	case errors.Is(err, ErrSchema), errors.Is(err, ErrFloatValue):
		return "envelope-malformed"
	case errors.Is(err, ErrExpired), errors.Is(err, ErrTooFarFuture):
		return "envelope-expired"
	case errors.Is(err, ErrSignature):
		return "envelope-signature-invalid"
	case errors.Is(err, ErrReplayNonce), errors.Is(err, ErrReplaySeq):
		return "envelope-replayed"
	case errors.Is(err, ErrPolicyStale):
		return "policy-bundle-stale"
	case err == nil:
		return ""
	default:
		return "envelope-rejected"
	}
}
