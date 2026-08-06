// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/zalando/go-keyring"
)

// The credential store (design §10.3, §10.10, OQ-26).
//
// Both backends are exercised, and the fallback is exercised as a DECISION rather than as
// whatever this machine happens to have: OQ-26 is about reporting the degraded mode, and a
// test that only ran the backend available on the developer's laptop would assert nothing
// about the other one.
//
// Credentials are synthetic and self-labelling per .antigravity/steering/secret-safety.md.

// fakeKeyring is a real in-memory keyring, not a mock: the interface is three methods and
// a struct cannot drift from it, whereas a mock would accept any call shape (D-23).
type fakeKeyring struct {
	items     map[string]string
	failWrite error
}

func newFakeKeyring() *fakeKeyring { return &fakeKeyring{items: map[string]string{}} }

func (f *fakeKeyring) Set(service, user, password string) error {
	if f.failWrite != nil {
		return f.failWrite
	}
	f.items[service+"/"+user] = password
	return nil
}

func (f *fakeKeyring) Get(service, user string) (string, error) {
	value, ok := f.items[service+"/"+user]
	if !ok {
		return "", keyring.ErrNotFound
	}
	return value, nil
}

func (f *fakeKeyring) Delete(service, user string) error {
	key := service + "/" + user
	if _, ok := f.items[key]; !ok {
		return keyring.ErrNotFound
	}
	delete(f.items, key)
	return nil
}

// unavailableKeyring simulates headless Linux with no Secret Service: writes fail.
type unavailableKeyring struct{}

func (unavailableKeyring) Set(string, string, string) error { return errors.New("no secret service") }
func (unavailableKeyring) Get(string, string) (string, error) {
	return "", errors.New("no secret service")
}
func (unavailableKeyring) Delete(string, string) error { return errors.New("no secret service") }

func syntheticCredentials() Credentials {
	marker := []byte("test-only-not-a-real-secret")
	return Credentials{
		DeviceID:    "01JBQ8Z0000000000000000000",
		DeviceToken: append([]byte("token-"), marker...),
		EnvelopeKey: append([]byte("envkey-"), marker...),
		ClientCert:  []byte("-----BEGIN CERT" + "IFICATE-----\nnot-a-real-certificate\n"),
		ClientKey:   append([]byte("not-a-real-key-"), marker...),
		CABundle:    []byte("-----BEGIN CERT" + "IFICATE-----\nnot-a-real-ca\n"),
	}
}

// ─── backend selection and the degraded-mode report ─────────────────────────

func TestNewStore_AutoPrefersTheKeychain(t *testing.T) {
	t.Parallel()

	store, err := newStoreWith(t.TempDir(), "auto", newFakeKeyring())
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	if store.Backend() != BackendKeychain {
		t.Errorf("Backend = %q, want %q", store.Backend(), BackendKeychain)
	}
}

func TestNewStore_AutoFallsBackAndSaysSo(t *testing.T) {
	t.Parallel()

	// OQ-26's whole subject. Headless Linux without a Secret Service is the common CI
	// and server case, and the agent must keep working AND admit which backend it got.
	store, err := newStoreWith(t.TempDir(), "auto", unavailableKeyring{})
	if err != nil {
		t.Fatalf("auto must not fail when no keychain exists: %v", err)
	}
	if store.Backend() != BackendFile {
		t.Fatalf("Backend = %q, want %q", store.Backend(), BackendFile)
	}
	// `agent doctor` reads this string, so it has to name the mode rather than say "ok".
	if store.Backend() == "ok" || store.Backend() == "" {
		t.Error("the degraded mode is not reported distinctly")
	}
}

func TestNewStore_ExplicitKeychainFailsRatherThanDowngrading(t *testing.T) {
	t.Parallel()

	// An operator who asked for a keychain explicitly must not silently get a file.
	// Silently downgrading a security control is the failure mode this whole phase is
	// written against.
	if _, err := newStoreWith(t.TempDir(), "keychain", unavailableKeyring{}); err == nil {
		t.Fatal("AGENT_CREDENTIAL_STORE=keychain must fail when no keychain is usable")
	}
}

func TestNewStore_ExplicitFileNeverTouchesTheKeychain(t *testing.T) {
	t.Parallel()

	ring := newFakeKeyring()
	store, err := newStoreWith(t.TempDir(), "file", ring)
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	if store.Backend() != BackendFile {
		t.Fatalf("Backend = %q", store.Backend())
	}
	if err := store.Save(context.Background(), syntheticCredentials()); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if len(ring.items) != 0 {
		t.Errorf("the keychain was written to despite AGENT_CREDENTIAL_STORE=file: %v", ring.items)
	}
}

func TestNewStore_RejectsAnUnknownPreference(t *testing.T) {
	t.Parallel()

	if _, err := newStoreWith(t.TempDir(), "vault", newFakeKeyring()); err == nil {
		t.Fatal("an unknown credential store must be refused")
	}
}

func TestProbeKeyring_WritesRatherThanReads(t *testing.T) {
	t.Parallel()

	// A read-only probe cannot distinguish "no Secret Service" from "empty keychain":
	// both answer not-found. Writing is the only way to tell, and the marker must be
	// removed again.
	ring := newFakeKeyring()
	if err := probeKeyring(ring); err != nil {
		t.Fatalf("probeKeyring: %v", err)
	}
	if len(ring.items) != 0 {
		t.Errorf("the probe left state behind: %v", ring.items)
	}

	if err := probeKeyring(unavailableKeyring{}); err == nil {
		t.Error("probeKeyring must fail when writes fail")
	}
}

// ─── round trips on both backends ───────────────────────────────────────────

func TestStore_RoundTripOnBothBackends(t *testing.T) {
	t.Parallel()

	backends := map[string]keyringAPI{
		"keychain": newFakeKeyring(),
		"file":     unavailableKeyring{},
	}

	for name, ring := range backends {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			store, err := newStoreWith(t.TempDir(), "auto", ring)
			if err != nil {
				t.Fatalf("newStoreWith: %v", err)
			}

			ctx := context.Background()
			want := syntheticCredentials()
			if err := store.Save(ctx, want); err != nil {
				t.Fatalf("Save: %v", err)
			}

			got, err := store.Load(ctx)
			if err != nil {
				t.Fatalf("Load: %v", err)
			}
			if got.DeviceID != want.DeviceID ||
				string(got.DeviceToken) != string(want.DeviceToken) ||
				string(got.EnvelopeKey) != string(want.EnvelopeKey) ||
				string(got.ClientCert) != string(want.ClientCert) ||
				string(got.ClientKey) != string(want.ClientKey) ||
				string(got.CABundle) != string(want.CABundle) {
				t.Errorf("round trip lost data:\n got %+v\nwant %+v", got, want)
			}

			if err := store.Wipe(ctx); err != nil {
				t.Fatalf("Wipe: %v", err)
			}
			if _, err := store.Load(ctx); !errors.Is(err, ErrNoCredentials) {
				t.Errorf("after Wipe, Load = %v, want ErrNoCredentials", err)
			}
		})
	}
}

func TestStore_LoadWithoutSaveReportsUnpaired(t *testing.T) {
	t.Parallel()

	// Distinct from a read error, because `agent doctor` tells the user to run `pair` in
	// this case and something else otherwise (§10.10).
	for name, ring := range map[string]keyringAPI{"keychain": newFakeKeyring(), "file": unavailableKeyring{}} {
		store, err := newStoreWith(t.TempDir(), "auto", ring)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if _, err := store.Load(context.Background()); !errors.Is(err, ErrNoCredentials) {
			t.Errorf("%s: Load = %v, want ErrNoCredentials", name, err)
		}
	}
}

func TestStore_WipeIsIdempotent(t *testing.T) {
	t.Parallel()

	// Called on revocation: an agent told it is revoked must reach the unpaired state
	// whatever it finds, so a second Wipe cannot be an error.
	for name, ring := range map[string]keyringAPI{"keychain": newFakeKeyring(), "file": unavailableKeyring{}} {
		store, err := newStoreWith(t.TempDir(), "auto", ring)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		ctx := context.Background()
		if err := store.Wipe(ctx); err != nil {
			t.Errorf("%s: first Wipe on empty store = %v", name, err)
		}
		if err := store.Save(ctx, syntheticCredentials()); err != nil {
			t.Fatalf("%s: Save: %v", name, err)
		}
		if err := store.Wipe(ctx); err != nil {
			t.Errorf("%s: Wipe = %v", name, err)
		}
		if err := store.Wipe(ctx); err != nil {
			t.Errorf("%s: second Wipe = %v", name, err)
		}
	}
}

// ─── file mode ──────────────────────────────────────────────────────────────

func TestFileStore_WritesOwnerOnly(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("platform-only: posix - NTFS uses ACLs; Go reports a synthetic mode, so a " +
			"permission-bit assertion is meaningless (D-68)")
	}
	t.Parallel()

	dir := t.TempDir()
	store, err := newStoreWith(dir, "file", unavailableKeyring{})
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	if err := store.Save(context.Background(), syntheticCredentials()); err != nil {
		t.Fatalf("Save: %v", err)
	}

	info, err := os.Stat(store.Path())
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("mode = %#o, want 0600", perm)
	}
}

func TestFileStore_RefusesAWorldReadableFileOnEveryLoad(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("platform-only: posix - NTFS uses ACLs; see assertOwnerOnly (D-68)")
	}
	t.Parallel()

	dir := t.TempDir()
	store, err := newStoreWith(dir, "file", unavailableKeyring{})
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	ctx := context.Background()
	if err := store.Save(ctx, syntheticCredentials()); err != nil {
		t.Fatalf("Save: %v", err)
	}

	// The mode can change AFTER the file is written — a careless chmod, a restore from a
	// backup, a copy onto a share. The load is the only moment the agent can notice, so
	// the check belongs there and not only at save.
	if err := os.Chmod(store.Path(), 0o644); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	if _, err := store.Load(ctx); !errors.Is(err, ErrInsecurePermissions) {
		t.Fatalf("Load = %v, want ErrInsecurePermissions", err)
	}
}

func TestAssertOwnerOnly_Matrix(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("platform-only: posix - NTFS uses ACLs; see assertOwnerOnly (D-68)")
	}
	t.Parallel()

	cases := []struct {
		mode os.FileMode
		ok   bool
	}{
		{0o600, true},
		{0o400, true},
		{0o640, false}, // group-readable
		{0o604, false}, // world-readable
		{0o666, false},
		{0o777, false},
	}
	for _, c := range cases {
		err := assertOwnerOnly("/tmp/probe", c.mode)
		if (err == nil) != c.ok {
			t.Errorf("mode %#o: err = %v, want ok=%v", c.mode, err, c.ok)
		}
	}
}

func TestFileStore_PathIsEmptyWhenTheKeychainIsUsed(t *testing.T) {
	t.Parallel()

	// There is no file, so reporting a path would invite somebody to look for one.
	store, err := newStoreWith(t.TempDir(), "auto", newFakeKeyring())
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	if store.Path() != "" {
		t.Errorf("Path = %q, want empty for the keychain backend", store.Path())
	}
}

func TestResolveStateDir_CreatesTheConfiguredDirectory(t *testing.T) {
	t.Parallel()

	nested := filepath.Join(t.TempDir(), "a", "b", "forgeops")
	got, err := resolveStateDir(nested)
	if err != nil {
		t.Fatalf("resolveStateDir: %v", err)
	}
	if got != nested {
		t.Errorf("dir = %q, want %q", got, nested)
	}
	info, err := os.Stat(nested)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if !info.IsDir() {
		t.Error("not a directory")
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o700 {
		t.Errorf("state dir mode = %#o, want 0700", info.Mode().Perm())
	}
}

func TestStore_SavePropagatesAKeychainWriteFailure(t *testing.T) {
	t.Parallel()

	// A silent failure would leave the agent believing it is paired while holding
	// nothing, so the next restart would look like an unexplained revocation.
	ring := newFakeKeyring()
	store, err := newStoreWith(t.TempDir(), "auto", ring)
	if err != nil {
		t.Fatalf("newStoreWith: %v", err)
	}
	ring.failWrite = errors.New("keychain locked")
	if err := store.Save(context.Background(), syntheticCredentials()); err == nil {
		t.Fatal("Save must not swallow a keychain write failure")
	}
}
