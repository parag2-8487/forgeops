// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"testing"

	"go.uber.org/zap"
)

// Publishing a policy bundle used to invalidate every paired agent.
//
// The chokepoint admits a submission only when `agent_devices.policy_bundle_digest` equals the
// project's active digest. §3.1 says `session.connect` returns the bundle when the pin is stale; it did
// not — it reported only THAT the pin was stale, so an agent was told it was out of date and given no
// way to become up to date. `devices.py` conceded the consequence in a comment: submissions "are
// refused until one is published and it pairs again".
//
// These cover the adoption side. The digest check is the load-bearing one: the backend states a digest
// and sends a body, and those are two separate claims.

func bundleOf(t *testing.T, body []byte) (string, string) {
	t.Helper()
	sum := sha256.Sum256(body)
	return base64.StdEncoding.EncodeToString(body), "sha256:" + hex.EncodeToString(sum[:])
}

func pairedStore(t *testing.T) Store {
	t.Helper()
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("opening a file-backed store: %v", err)
	}
	if err := store.Save(context.Background(), Credentials{
		DeviceID:           "01J00000000000000000000RP",
		DeviceToken:        sizedBlob("token", credentialByteLength),
		EnvelopeKey:        sizedBlob("envelope", credentialByteLength),
		ClientKey:          sizedBlob("client-key", 227),
		ClientCert:         sizedBlob("client-cert", 900),
		CABundle:           sizedBlob("ca-bundle", 656),
		PolicyBundle:       []byte("the bundle stored at pairing"),
		PolicyBundleDigest: "sha256:" + hex.EncodeToString(sha256.New().Sum(nil)),
	}); err != nil {
		t.Fatalf("seeding a paired credential: %v", err)
	}
	return store
}

func TestAdoptingABundleStoresBodyAndDigest(t *testing.T) {
	t.Parallel()

	store := pairedStore(t)
	m := &Manager{store: store, logger: zap.NewNop()}
	encoded, digest := bundleOf(t, []byte("a newly published bundle body"))

	adopted, err := m.adoptBundle(context.Background(), encoded, digest)
	if err != nil {
		t.Fatalf("adopting: %v", err)
	}
	if !adopted {
		t.Fatal("a new digest must be adopted")
	}

	creds, err := store.Load(context.Background())
	if err != nil {
		t.Fatalf("loading: %v", err)
	}
	if creds.PolicyBundleDigest != digest {
		t.Fatalf("digest = %q, want %q", creds.PolicyBundleDigest, digest)
	}
	if string(creds.PolicyBundle) != "a newly published bundle body" {
		t.Fatalf("body = %q", string(creds.PolicyBundle))
	}
	// The rest of the credential must be untouched: adoption replaces a policy, not an identity.
	if creds.DeviceID != "01J00000000000000000000RP" {
		t.Fatalf("adoption disturbed the device identity: %q", creds.DeviceID)
	}
	if len(creds.ClientCert) != 900 || len(creds.CABundle) != 656 {
		t.Fatal("adoption disturbed the TLS material")
	}
}

func TestABodyThatDoesNotHashToTheStatedDigestIsRefused(t *testing.T) {
	t.Parallel()

	// The digest is what `envelope` compares every command against. Storing a body under a digest it
	// does not hash to would make every later verification compare against a value that describes
	// something else — discovered as a signature failure rather than as the mismatch it is.
	store := pairedStore(t)
	m := &Manager{store: store, logger: zap.NewNop()}
	encoded, _ := bundleOf(t, []byte("one body"))
	_, wrongDigest := bundleOf(t, []byte("a different body"))

	adopted, err := m.adoptBundle(context.Background(), encoded, wrongDigest)
	if err == nil {
		t.Fatal("a mismatched digest must be refused")
	}
	if adopted {
		t.Fatal("nothing may be adopted when the digest disagrees")
	}
	creds, _ := store.Load(context.Background())
	if string(creds.PolicyBundle) != "the bundle stored at pairing" {
		t.Fatal("the previous bundle must survive a refused adoption")
	}
}

func TestAnEmptyBundleIsRefused(t *testing.T) {
	t.Parallel()

	// D-30 makes a missing bundle a DENY, so accepting an empty one would replace a working policy
	// with a refuse-everything policy on the strength of a field the backend should not have sent.
	store := pairedStore(t)
	m := &Manager{store: store, logger: zap.NewNop()}
	_, digest := bundleOf(t, []byte{})
	if _, err := m.adoptBundle(context.Background(), "", digest); err == nil {
		t.Fatal("an empty bundle must be refused")
	}
}

func TestAdoptingTheSameDigestTwiceChangesNothing(t *testing.T) {
	t.Parallel()

	// Idempotent, because `agent.status` reports the held digest on a timer: a handshake that offered
	// the bundle the agent already holds must not rewrite the credential on every reconnect.
	store := pairedStore(t)
	m := &Manager{store: store, logger: zap.NewNop()}
	encoded, digest := bundleOf(t, []byte("stable body"))
	if _, err := m.adoptBundle(context.Background(), encoded, digest); err != nil {
		t.Fatalf("first adoption: %v", err)
	}
	again, err := m.adoptBundle(context.Background(), encoded, digest)
	if err != nil {
		t.Fatalf("second adoption: %v", err)
	}
	if again {
		t.Fatal("re-adopting an identical digest must report no change")
	}
}

func TestABundleWithNoDigestIsRefused(t *testing.T) {
	t.Parallel()

	store := pairedStore(t)
	m := &Manager{store: store, logger: zap.NewNop()}
	encoded, _ := bundleOf(t, []byte("body without a stated digest"))
	if _, err := m.adoptBundle(context.Background(), encoded, "   "); err == nil {
		t.Fatal("a bundle offered with no digest must be refused")
	}
}
