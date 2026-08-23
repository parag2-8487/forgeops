// SPDX-License-Identifier: Apache-2.0

// This file supplies the two envelope collaborators that have to read the agent's own
// credentials (design §10.3, §10.4, §10.6, D-59).
//
// WHY THEY LIVE HERE AND NOT IN `envelope`
// `envelope` is a leaf: `TestPackageIsALeaf` fails the build if it imports anything under
// `internal/**`, because `session -> executor -> mutate -> envelope` would otherwise close a
// cycle. So `envelope` declares `KeySource` and `BundleDigestSource` as interfaces and cannot
// itself reach the credential Store. `session` already owns the Store and already imports
// `envelope`, so this is the one package that can join the two without pointing a dependency
// the wrong way.
//
// WHY NOT THE `Static*` TYPES
// `envelope.StaticKeySource` and `envelope.StaticBundleDigest` hold a value somebody has to
// remember to `Set`. On a production path that is a latent wrong answer: the agent would verify
// against whatever was installed at construction, which is before any credential is loaded, so
// the key would be absent and the digest empty. Both types below read the live credential
// instead, so there is no window in which the verifier is configured with a stale fact.
package session

import (
	"context"
	"crypto/subtle"
	"errors"
	"fmt"
	"sync"
)

// CredentialKeySource serves the envelope HMAC key from the credential Store (§10.3).
//
// It also enforces something no other layer can: that the key it hands out belongs to the
// device the envelope claims to be for. `Verify` looks the key up BY the envelope's
// `device_id`, so a source that ignored the argument and returned "the key we have" would
// verify an envelope addressed to a different device against this device's key. That cannot
// succeed against an honest backend, but the check costs one comparison and turns "it would
// fail anyway" into "it is refused here".
type CredentialKeySource struct {
	store Store

	mu     sync.RWMutex
	cached Credentials
	loaded bool
}

// NewCredentialKeySource builds a key source over the store.
func NewCredentialKeySource(store Store) (*CredentialKeySource, error) {
	if store == nil {
		return nil, errors.New("session: a CredentialKeySource needs a Store")
	}
	return &CredentialKeySource{store: store}, nil
}

// EnvelopeKey implements envelope.KeySource.
//
// The credential is cached after the first successful load. Rereading it per envelope would
// hit the OS keychain on every inbound frame, and the key does not change within a pairing:
// `Pair` refuses to overwrite an existing credential (ErrAlreadyPaired) and a revocation
// wipes the process along with the file. `Invalidate` exists for the one caller that needs
// the cache dropped.
func (s *CredentialKeySource) EnvelopeKey(ctx context.Context, deviceID string) ([]byte, error) {
	if deviceID == "" {
		return nil, errors.New("session: EnvelopeKey needs a device id")
	}
	creds, err := s.credentials(ctx)
	if err != nil {
		return nil, err
	}
	if len(creds.EnvelopeKey) == 0 {
		return nil, errors.New("session: the stored credential carries no envelope key")
	}
	// Constant time, because this compares an attacker-supplied identifier against a local
	// one and a length-dependent early return would leak the stored device id a character at
	// a time. The value is not secret; the habit is what keeps the one that is safe.
	if subtle.ConstantTimeCompare([]byte(creds.DeviceID), []byte(deviceID)) != 1 {
		return nil, fmt.Errorf(
			"session: this agent holds no key for device %s; the envelope is addressed elsewhere", deviceID)
	}
	key := make([]byte, len(creds.EnvelopeKey))
	copy(key, creds.EnvelopeKey)
	return key, nil
}

// Invalidate drops the cached credential, so the next lookup reloads it.
func (s *CredentialKeySource) Invalidate() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.loaded = false
	s.cached = Credentials{}
}

func (s *CredentialKeySource) credentials(ctx context.Context) (Credentials, error) {
	s.mu.RLock()
	if s.loaded {
		defer s.mu.RUnlock()
		return s.cached, nil
	}
	s.mu.RUnlock()

	s.mu.Lock()
	defer s.mu.Unlock()
	if s.loaded {
		return s.cached, nil
	}
	creds, err := s.store.Load(ctx)
	if err != nil {
		return Credentials{}, err
	}
	s.cached = creds
	s.loaded = true
	return creds, nil
}

// CredentialBundleState is the agent's policy-bundle view, backed by what pairing pinned.
//
// One type implementing BOTH `session.BundleState` and `envelope.BundleDigestSource`, and that
// is the point rather than a convenience. The two interfaces ask the same question — "which
// bundle does this agent hold?" — and two objects answering it separately is journal pattern
// H: the envelope check (Q-07) and the mutation gate (§10.3) could disagree, and the
// disagreement would look like an intermittent policy failure.
//
// WHERE THE DIGEST COMES FROM
// The backend pins `agent_devices.policy_bundle_digest` at the pairing exchange and returns the
// bundle and its digest in the same response. The governance chokepoint then admits a
// submission only when the device's pin equals the project's active digest, and it mints every
// envelope carrying that digest in `policy_context.bundle_digest`. So the digest stored at
// pairing is exactly the value the chokepoint compares against — which is why this reads the
// credential rather than a value handed in at construction.
type CredentialBundleState struct {
	store Store

	mu       sync.RWMutex
	held     string
	loaded   bool
	observed bool
	backend  string
	stale    bool
}

// NewCredentialBundleState builds the bundle view over the store.
func NewCredentialBundleState(store Store) (*CredentialBundleState, error) {
	if store == nil {
		return nil, errors.New("session: a CredentialBundleState needs a Store")
	}
	return &CredentialBundleState{store: store}, nil
}

// Digest implements BundleState: the digest this agent holds, empty when it holds none.
func (b *CredentialBundleState) Digest() string {
	digest, _ := b.load(context.Background())
	return digest
}

// BundleDigest implements envelope.BundleDigestSource.
//
// Returns an error rather than the empty string when nothing is held, matching
// `StaticBundleDigest`'s contract for the same reason: "no bundle" must not be readable as
// "matches nothing" by a caller that forgot to check. Q-07 is a rejection, not a warning.
func (b *CredentialBundleState) BundleDigest(ctx context.Context) (string, error) {
	digest, err := b.load(ctx)
	if err != nil {
		return "", err
	}
	if digest == "" {
		return "", errors.New("session: this agent holds no policy bundle; pair again to receive one")
	}
	return digest, nil
}

// Current implements BundleState: whether the held digest is what the backend last announced.
//
// FALSE BEFORE THE FIRST HANDSHAKE, deliberately. Until `session.connect` answers, the agent
// has no statement from the backend about which bundle is active, and D-25's lesson is that an
// absent policy fact is never read as permission. `Serve` calls `acceptHandshake` — and so
// `ObserveBackend` — before the drain and before any command reaches `execute`, so the honest
// answer is available by the time it is consulted.
func (b *CredentialBundleState) Current() bool {
	held, err := b.load(context.Background())
	if err != nil || held == "" {
		return false
	}
	b.mu.RLock()
	defer b.mu.RUnlock()
	if !b.observed {
		return false
	}
	if b.stale {
		return false
	}
	// An empty announcement means the backend has no active bundle for this project. That is
	// not agreement: the agent holds a digest the backend is no longer enforcing.
	return b.backend != "" && b.backend == held
}

// ObserveBackend implements BundleState, recording what the handshake said.
func (b *CredentialBundleState) ObserveBackend(digest string, stale bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.observed = true
	b.backend = digest
	b.stale = stale
}

// Invalidate drops the cached digest so the next read reloads it, as a re-pair requires.
func (b *CredentialBundleState) Invalidate() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.loaded = false
	b.held = ""
}

func (b *CredentialBundleState) load(ctx context.Context) (string, error) {
	b.mu.RLock()
	if b.loaded {
		defer b.mu.RUnlock()
		return b.held, nil
	}
	b.mu.RUnlock()

	b.mu.Lock()
	defer b.mu.Unlock()
	if b.loaded {
		return b.held, nil
	}
	creds, err := b.store.Load(ctx)
	if err != nil {
		return "", err
	}
	b.held = creds.PolicyBundleDigest
	b.loaded = true
	return b.held, nil
}
