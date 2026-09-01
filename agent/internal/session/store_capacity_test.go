// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/zalando/go-keyring"
)

// Pairing on Windows could never succeed, and this file is the regression suite for why.
//
// `zalando/go-keyring` refuses a value over 2560 bytes before it reaches `CredWriteW`, because
// `CRED_MAX_CREDENTIAL_BLOB_SIZE` is 2560 — counted in RAW UTF-8 BYTES, not UTF-16 code units.
// Measured against the real Windows Credential Manager with go-keyring v0.2.6: 2560 single-byte
// characters accepted, 2561 refused; 1280 two-byte runes accepted, 1281 refused. Both are the
// same 2560-byte ceiling, which is what `limitedKeyring` reproduces here.
//
// The full credential set is far past it — the policy bundle alone is a gzipped tar of the
// project's rego, base64-encoded — so `pair` failed AFTER the exchange had spent the code.

// platformKeychainLimits records what each OS credential store actually accepts.
//
// ASSERTED PER PLATFORM RATHER THAN ASSUMED TO AGREE, because they do not. The Windows number is
// a hard ceiling enforced by the library; macOS and libsecret have no comparable small limit, so
// a single shared constant would either be wrong for two platforms or needlessly punitive on
// them. `secretHalfBudget` is what this repository commits to fitting inside the smallest one.
var platformKeychainLimits = map[string]struct {
	bytes  int
	source string
}{
	"windows": {
		bytes: 2560,
		source: "CRED_MAX_CREDENTIAL_BLOB_SIZE, enforced by go-keyring's own length check in " +
			"keyring_windows.go before CredWriteW is called",
	},
	"darwin": {
		bytes: 1 << 20,
		source: "macOS Keychain imposes no documented per-item limit for generic passwords; " +
			"1 MiB is a conservative floor far above anything this agent stores",
	},
	"linux": {
		bytes: 1 << 20,
		source: "libsecret has no per-item limit; the practical bound is the D-Bus maximum " +
			"message size, which defaults to 128 MiB",
	},
}

// secretHalfBudget is the ceiling the secret half must fit inside on EVERY platform.
//
// The smallest real limit is Windows' 2560. The budget is deliberately set at that number rather
// than at something more comfortable, because a budget larger than the smallest platform's limit
// would pass here and fail in the user's hands, which is the entire defect this file covers.
const secretHalfBudget = 2560

// limitedKeyring is a keyring that enforces a byte ceiling, the way the Windows one does.
type limitedKeyring struct {
	items map[string]string
	limit int
}

func newLimitedKeyring(limit int) *limitedKeyring {
	return &limitedKeyring{items: map[string]string{}, limit: limit}
}

func (l *limitedKeyring) Set(service, user, password string) error {
	// The real library's message, so a test that matches on it is matching on what a user sees.
	if len(password) > l.limit {
		return errors.New("data passed to Set was too big")
	}
	l.items[service+"/"+user] = password
	return nil
}

func (l *limitedKeyring) Get(service, user string) (string, error) {
	value, ok := l.items[service+"/"+user]
	if !ok {
		return "", errNotFoundForTest
	}
	return value, nil
}

func (l *limitedKeyring) Delete(service, user string) error {
	key := service + "/" + user
	if _, ok := l.items[key]; !ok {
		return errNotFoundForTest
	}
	delete(l.items, key)
	return nil
}

// realisticCredentials is the credential set the backend actually returns, at the sizes it
// actually returns, including the policy bundle that is the largest single field.
//
// SIZES, NOT STRUCTURE, AND NO PEM ARMOUR. The store treats every one of these as opaque bytes —
// it marshals them to JSON and hands them to a keychain or a file — so what decides whether the
// defect reproduces is the BYTE COUNT and nothing else. Writing real armour here would add no
// coverage and would put credential-shaped text in the tree for the repository's secret gate to
// find, where a fixture is indistinguishable from a leak. The counts are measured from a real
// exchange: ~227 bytes for a P-256 private key, ~1200 for a leaf certificate, ~1400 for a CA
// bundle, and tens of kilobytes for the base64 of a gzipped rego bundle.
func realisticCredentials() Credentials {
	const (
		privateKeyBytes   = 227
		leafCertBytes     = 1200
		caBundleBytes     = 1400
		policyBundleBytes = 18000
	)
	return Credentials{
		DeviceID:    "01JBQ8Z0000000000000000000",
		DeviceToken: []byte(strings.Repeat("t", credentialByteLength)),
		EnvelopeKey: []byte(strings.Repeat("e", credentialByteLength)),
		// Self-labelling, so a value that somehow escaped into a log or a bug report reads as
		// what it is. The label is inside the byte budget, not added to it.
		ClientKey:          sizedBlob("not-a-real-private-key", privateKeyBytes),
		ClientCert:         sizedBlob("not-a-real-certificate", leafCertBytes),
		CABundle:           sizedBlob("not-a-real-ca-bundle", caBundleBytes),
		PolicyBundle:       sizedBlob("not-a-real-policy-bundle", policyBundleBytes),
		PolicyBundleDigest: "sha256:" + strings.Repeat("0", 64),
	}
}

// sizedBlob returns exactly n bytes that say what they are.
func sizedBlob(label string, n int) []byte {
	if n <= len(label) {
		return []byte(label[:n])
	}
	return []byte(label + strings.Repeat(".", n-len(label)))
}

// ─── the defect itself ──────────────────────────────────────────────────────

func TestSave_TheRealCredentialFitsTheSmallestPlatformKeychain(t *testing.T) {
	t.Parallel()

	// THE REGRESSION TEST FOR THE WINDOWS DEFECT. Before the split this Save failed with
	// "data passed to Set was too big" and the caller had already spent its pairing code.
	ring := newLimitedKeyring(secretHalfBudget)
	store, err := newStoreWith(t.TempDir(), "keychain", ring)
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}

	want := realisticCredentials()
	if err := store.Save(context.Background(), want); err != nil {
		t.Fatalf("Save must succeed under a %d-byte keychain ceiling: %v", secretHalfBudget, err)
	}

	got, err := store.Load(context.Background())
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	// Every field must survive the round trip. A split that dropped the policy bundle would
	// make every command refuse with `policy-bundle-stale`, so completeness is the point.
	if got.DeviceID != want.DeviceID {
		t.Errorf("DeviceID = %q, want %q", got.DeviceID, want.DeviceID)
	}
	for _, f := range []struct {
		name      string
		got, want []byte
	}{
		{"DeviceToken", got.DeviceToken, want.DeviceToken},
		{"EnvelopeKey", got.EnvelopeKey, want.EnvelopeKey},
		{"ClientKey", got.ClientKey, want.ClientKey},
		{"ClientCert", got.ClientCert, want.ClientCert},
		{"CABundle", got.CABundle, want.CABundle},
		{"PolicyBundle", got.PolicyBundle, want.PolicyBundle},
	} {
		if string(f.got) != string(f.want) {
			t.Errorf("%s did not survive the round trip (%d bytes back, %d in)",
				f.name, len(f.got), len(f.want))
		}
	}
	if got.PolicyBundleDigest != want.PolicyBundleDigest {
		t.Errorf("PolicyBundleDigest = %q, want %q", got.PolicyBundleDigest, want.PolicyBundleDigest)
	}
}

func TestSecretHalfFitsEveryPlatformLimit(t *testing.T) {
	t.Parallel()

	secret, _ := split(realisticCredentials())
	encoded, err := json.Marshal(secret)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	if len(encoded) > secretHalfBudget {
		t.Fatalf("the secret half is %d bytes, over the %d-byte budget: it will not fit the "+
			"Windows Credential Manager", len(encoded), secretHalfBudget)
	}

	// Asserted against each platform's real limit, so a future field that fits Windows by luck
	// but breaks an assumption about another store is still caught by name.
	for goos, limit := range platformKeychainLimits {
		if len(encoded) > limit.bytes {
			t.Errorf("secret half is %d bytes, over %s's %d-byte limit (%s)",
				len(encoded), goos, limit.bytes, limit.source)
		}
	}

	// And the budget itself must equal the smallest platform limit, or the budget is a
	// comfortable number rather than a true one.
	smallest := 0
	for _, limit := range platformKeychainLimits {
		if smallest == 0 || limit.bytes < smallest {
			smallest = limit.bytes
		}
	}
	if secretHalfBudget != smallest {
		t.Errorf("secretHalfBudget is %d but the smallest platform limit is %d; the budget must "+
			"be the smallest real limit, not a chosen one", secretHalfBudget, smallest)
	}
}

func TestSplit_NoSecretReachesThePublicFile(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	ring := newLimitedKeyring(secretHalfBudget)
	store, err := newStoreWith(dir, "keychain", ring)
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	creds := realisticCredentials()
	if err := store.Save(context.Background(), creds); err != nil {
		t.Fatalf("Save: %v", err)
	}

	onDisk, err := os.ReadFile(filepath.Join(dir, publicFile))
	if err != nil {
		t.Fatalf("reading the public half: %v", err)
	}

	// THE SPLIT'S CORRECTNESS CONDITION. Moving material out of the keychain is only sound if
	// what moved is not secret; a private key on disk beside the certificate would be a
	// downgrade dressed up as a bug fix.
	for _, secret := range []struct {
		name  string
		value []byte
	}{
		{"device token", creds.DeviceToken},
		{"envelope key", creds.EnvelopeKey},
		{"client private key", creds.ClientKey},
	} {
		if strings.Contains(string(onDisk), string(secret.value)) {
			t.Errorf("%s was written to %s; only the keychain may hold it", secret.name, publicFile)
		}
	}
}

func TestSplit_NoCertificateOccupiesTheKeychain(t *testing.T) {
	t.Parallel()

	ring := newLimitedKeyring(secretHalfBudget)
	store, err := newStoreWith(t.TempDir(), "keychain", ring)
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	creds := realisticCredentials()
	if err := store.Save(context.Background(), creds); err != nil {
		t.Fatalf("Save: %v", err)
	}

	stored, err := ring.Get(keyringService, keyringUser)
	if err != nil {
		t.Fatalf("keychain Get: %v", err)
	}

	// The converse of the test above: the reason the secret half fits is that these are NOT in
	// it, so a future change that puts one back must fail here rather than in the field.
	for _, public := range []struct {
		name  string
		value []byte
	}{
		{"client certificate", creds.ClientCert},
		{"CA bundle", creds.CABundle},
		{"policy bundle", creds.PolicyBundle},
	} {
		if strings.Contains(stored, string(public.value)) {
			t.Errorf("%s is in the keychain entry; it belongs in %s and its size is why "+
				"pairing failed on Windows", public.name, publicFile)
		}
	}
}

// ─── the pre-flight capacity check ──────────────────────────────────────────

func TestCheckCapacity_RefusesACredentialTheKeychainCannotHold(t *testing.T) {
	t.Parallel()

	// A ceiling below the secret half's real size: the case a platform with a smaller limit
	// than any known today would present.
	store, err := newStoreWith(t.TempDir(), "keychain", newLimitedKeyring(64))
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}

	err = store.CheckCapacity(context.Background(), realisticCredentials())
	if err == nil {
		t.Fatal("CheckCapacity must refuse a credential the keychain cannot hold")
	}
	if !errors.Is(err, ErrStoreTooSmall) {
		t.Errorf("error is not ErrStoreTooSmall, so `pair` cannot tell this apart from a "+
			"transport failure: %v", err)
	}
	// The remedy has to be in the message. A refusal the user cannot act on is a dead end.
	if !strings.Contains(err.Error(), "AGENT_CREDENTIAL_STORE=file") {
		t.Errorf("the refusal does not name the remedy: %v", err)
	}
}

func TestCheckCapacity_AcceptsWhatSaveWillAccept(t *testing.T) {
	t.Parallel()

	store, err := newStoreWith(t.TempDir(), "keychain", newLimitedKeyring(secretHalfBudget))
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	ctx := context.Background()

	// The probe must not be more pessimistic than the real save, or it would refuse pairings
	// that would have worked — a check that costs the user the thing it was protecting.
	if err := store.CheckCapacity(ctx, realisticCredentials()); err != nil {
		t.Fatalf("CheckCapacity refused a credential Save accepts: %v", err)
	}
	if err := store.Save(ctx, realisticCredentials()); err != nil {
		t.Fatalf("Save: %v", err)
	}
}

func TestCheckCapacity_LeavesNoCredentialBehind(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	ring := newLimitedKeyring(secretHalfBudget)
	store, err := newStoreWith(dir, "keychain", ring)
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	ctx := context.Background()

	if err := store.CheckCapacity(ctx, realisticCredentials()); err != nil {
		t.Fatalf("CheckCapacity: %v", err)
	}

	// A probe that left its own value behind would make an unpaired agent look paired, and
	// `Load` would then return a credential nothing issued.
	if _, err := store.Load(ctx); !errors.Is(err, ErrNoCredentials) {
		t.Errorf("after a capacity probe the agent must still be unpaired, got %v", err)
	}
	if len(ring.items) != 0 {
		t.Errorf("the probe left %d keychain entry/entries behind: %v", len(ring.items), ring.items)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	for _, e := range entries {
		if strings.Contains(e.Name(), "probe") {
			t.Errorf("the probe left %s behind", e.Name())
		}
	}
}

func TestCapacityProbe_IsNoSmallerThanTheRealCredential(t *testing.T) {
	t.Parallel()

	// The probe decides whether a pairing goes ahead, so it must never be smaller than what
	// the exchange will hand back. A probe that under-measures turns the pre-flight check into
	// a false reassurance and puts the burned-code failure back.
	real := realisticCredentials()
	probeSecret, _ := split(capacityProbe(real.ClientKey))
	realSecret, _ := split(real)

	probeEncoded, err := json.Marshal(probeSecret)
	if err != nil {
		t.Fatalf("marshal probe: %v", err)
	}
	realEncoded, err := json.Marshal(realSecret)
	if err != nil {
		t.Fatalf("marshal real: %v", err)
	}

	if len(probeEncoded) < len(realEncoded) {
		t.Errorf("the probe is %d bytes but the real secret half is %d; the probe must be at "+
			"least as large or it can pass where Save fails", len(probeEncoded), len(realEncoded))
	}
}

// ─── the incomplete-credential state ────────────────────────────────────────

func TestLoad_RefusesASecretHalfWithNoPublicHalf(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	ring := newLimitedKeyring(secretHalfBudget)
	store, err := newStoreWith(dir, "keychain", ring)
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	ctx := context.Background()
	if err := store.Save(ctx, realisticCredentials()); err != nil {
		t.Fatalf("Save: %v", err)
	}

	// Clearing the state directory while the keychain entry survives is an ordinary thing to
	// do by hand, and on Windows the two live in entirely different places.
	if err := os.Remove(filepath.Join(dir, publicFile)); err != nil {
		t.Fatalf("Remove: %v", err)
	}

	_, err = store.Load(ctx)
	if !errors.Is(err, ErrCredentialsIncomplete) {
		t.Fatalf("a token with no certificate must be refused by name, got %v", err)
	}
	// The user needs to be told what to do, because nothing else will fix this state.
	if !strings.Contains(err.Error(), "pair --wipe") {
		t.Errorf("the refusal does not name the remedy: %v", err)
	}
}

func TestWipe_ClearsBothHalves(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	ring := newLimitedKeyring(secretHalfBudget)
	store, err := newStoreWith(dir, "keychain", ring)
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	ctx := context.Background()
	if err := store.Save(ctx, realisticCredentials()); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if err := store.Wipe(ctx); err != nil {
		t.Fatalf("Wipe: %v", err)
	}

	// A Wipe that cleared only the keychain would leave the certificate behind, and the next
	// pair would then write a fresh token beside a stale certificate.
	if _, err := os.Stat(filepath.Join(dir, publicFile)); !os.IsNotExist(err) {
		t.Errorf("%s survived Wipe (stat err = %v)", publicFile, err)
	}
	if len(ring.items) != 0 {
		t.Errorf("the keychain still holds %d entry/entries after Wipe", len(ring.items))
	}
	if _, err := store.Load(ctx); !errors.Is(err, ErrNoCredentials) {
		t.Errorf("after Wipe the agent must be unpaired, got %v", err)
	}
}

func TestWipe_ClearsThePublicHalfEvenWhenTheKeychainFails(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	ring := newLimitedKeyring(secretHalfBudget)
	store, err := newStoreWith(dir, "keychain", ring)
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	ctx := context.Background()
	if err := store.Save(ctx, realisticCredentials()); err != nil {
		t.Fatalf("Save: %v", err)
	}

	// A keychain that fails to delete must not stop the certificate being removed: stopping at
	// the first error is what leaves the half-state this whole change exists to remove.
	store.ring = failingDeleteKeyring{limitedKeyring: ring}
	if err := store.Wipe(ctx); err == nil {
		t.Fatal("Wipe must report the keychain failure")
	}
	if _, err := os.Stat(filepath.Join(dir, publicFile)); !os.IsNotExist(err) {
		t.Errorf("%s survived a Wipe whose keychain delete failed", publicFile)
	}
}

type failingDeleteKeyring struct{ *limitedKeyring }

func (failingDeleteKeyring) Delete(string, string) error {
	return fmt.Errorf("keychain is locked")
}

// errNotFoundForTest is the library's own sentinel, so the fake refuses exactly as the real one does.
var errNotFoundForTest = keyring.ErrNotFound
