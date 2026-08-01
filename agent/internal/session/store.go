// SPDX-License-Identifier: Apache-2.0

// Package session implements the agent half of phases.md §1.1.
//
// This file is the credential store (design §10.3, §10.10, OQ-26).
package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"

	"github.com/zalando/go-keyring"

	"github.com/parag8487/ForgeOps/agent/internal/identity"
)

// Credentials is everything the agent needs to be itself (§10.3).
//
// The private key is in here and the CA bundle is in here; the ENVELOPE KEY is too,
// because the agent must verify signatures offline. Nothing else: no approval id, no
// authority, no cached envelope. What can be stored bounds what can be replayed, which is
// the same reasoning D-41 applies to the journal.
type Credentials struct {
	DeviceID    string `json:"device_id"`
	DeviceToken []byte `json:"device_token"` // opaque 32 bytes; the backend stores only its HMAC
	EnvelopeKey []byte `json:"envelope_key"` // 32 bytes, shared with the backend for HMAC-SHA256
	ClientCert  []byte `json:"client_cert"`  // PEM, <=24h
	ClientKey   []byte `json:"client_key"`   // PEM, generated locally, never transmitted
	CABundle    []byte `json:"ca_bundle"`    // PEM
}

// Store persists the device credential set.
type Store interface {
	Save(ctx context.Context, c Credentials) error
	Load(ctx context.Context) (Credentials, error)
	Wipe(ctx context.Context) error
	Backend() string // "keychain" | "file(0600)" — surfaced by agent doctor
}

var (
	// ErrNoCredentials means nothing has been stored: the agent is unpaired.
	ErrNoCredentials = errors.New("session: no stored credentials")

	// ErrInsecurePermissions means the credential file is readable by somebody other
	// than its owner. Refused rather than repaired, because by the time it is observed
	// the secret has already been exposed and rewriting the mode would hide that.
	ErrInsecurePermissions = errors.New("session: credential file is not owner-only")
)

const (
	// BackendKeychain is the OS credential manager: macOS Keychain, Windows Credential
	// Manager, Secret Service on Linux (Research §2).
	BackendKeychain = "keychain"
	// BackendFile is the 0600 fallback, used when no keychain is available — headless
	// Linux without a Secret Service, which is the common CI and server case.
	BackendFile = "file(0600)"

	keyringService = "forgeops-agent"
	keyringUser    = "device-credentials"
	credentialFile = "credentials.json"
)

// keyringAPI is the slice of go-keyring this package uses, as an interface so the
// fallback decision is testable without a live Secret Service.
//
// Declared here rather than taking the library's package functions directly: a test that
// cannot simulate "no keychain available" cannot assert the fallback, and the fallback is
// the whole subject of OQ-26.
type keyringAPI interface {
	Set(service, user, password string) error
	Get(service, user string) (string, error)
	Delete(service, user string) error
}

type realKeyring struct{}

func (realKeyring) Set(service, user, password string) error {
	return keyring.Set(service, user, password)
}
func (realKeyring) Get(service, user string) (string, error) {
	return keyring.Get(service, user)
}
func (realKeyring) Delete(service, user string) error {
	return keyring.Delete(service, user)
}

// FileStore is the store, whichever backend it ended up using.
//
// One type rather than two implementations, because the choice is made once at
// construction and the caller must not be able to pick: an agent that silently used a
// file when a keychain was available would be a downgrade nobody asked for, and an agent
// that failed because no keychain existed would be unusable in CI.
type FileStore struct {
	dir     string
	backend string
	ring    keyringAPI
}

// NewStore selects a backend and reports which one it got.
//
// `preference` is AGENT_CREDENTIAL_STORE: "auto" | "keychain" | "file".
//
//   - "keychain" fails if no keychain is available, because an operator who asked for one
//     explicitly should not silently get a file;
//   - "file" never touches the keychain;
//   - "auto" probes the keychain and falls back, REPORTING the fallback through
//     Backend() so `agent doctor` says so rather than pretending (§10.10, OQ-26).
func NewStore(stateDir, preference string) (*FileStore, error) {
	return newStoreWith(stateDir, preference, realKeyring{})
}

func newStoreWith(stateDir, preference string, ring keyringAPI) (*FileStore, error) {
	dir, err := resolveStateDir(stateDir)
	if err != nil {
		return nil, err
	}

	store := &FileStore{dir: dir, ring: ring}

	switch preference {
	case "file":
		store.backend = BackendFile
	case "keychain":
		if err := probeKeyring(ring); err != nil {
			return nil, fmt.Errorf(
				"session: AGENT_CREDENTIAL_STORE=keychain but no keychain is usable: %w", err)
		}
		store.backend = BackendKeychain
	case "auto", "":
		if err := probeKeyring(ring); err == nil {
			store.backend = BackendKeychain
		} else {
			store.backend = BackendFile
		}
	default:
		return nil, fmt.Errorf("session: unknown credential store %q", preference)
	}

	return store, nil
}

// probeKeyring writes and removes a marker to find out whether the keychain actually
// works, rather than whether the platform theoretically has one.
//
// A read-only probe is not enough: on headless Linux `Get` fails with "not found" whether
// the Secret Service is missing or simply empty, and those need different answers. Writing
// is the only way to tell, and the marker is removed immediately.
func probeKeyring(ring keyringAPI) error {
	const probeUser = "forgeops-keychain-probe"
	if err := ring.Set(keyringService, probeUser, "probe"); err != nil {
		return err
	}
	_ = ring.Delete(keyringService, probeUser)
	return nil
}

// resolveStateDir returns the directory the file backend uses, creating it 0700.
//
// Resolved here rather than in config so the value in the config stays the value the
// operator wrote, and so the OS default is computed once, at the point of use.
func resolveStateDir(configured string) (string, error) {
	if configured != "" {
		if err := os.MkdirAll(configured, 0o700); err != nil {
			return "", fmt.Errorf("session: creating state dir: %w", err)
		}
		return configured, nil
	}
	base, err := os.UserConfigDir()
	if err != nil {
		return "", fmt.Errorf("session: no state directory available: %w", err)
	}
	dir := filepath.Join(base, "forgeops")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("session: creating state dir: %w", err)
	}
	return dir, nil
}

// Backend reports which backend is in use, for `agent doctor`.
func (s *FileStore) Backend() string { return s.backend }

// Path is the credential file's location. Empty when the keychain is in use.
func (s *FileStore) Path() string {
	if s.backend != BackendFile {
		return ""
	}
	return filepath.Join(s.dir, credentialFile)
}

// Save persists the credential set.
func (s *FileStore) Save(_ context.Context, c Credentials) error {
	encoded, err := json.Marshal(c)
	if err != nil {
		return fmt.Errorf("session: encoding credentials: %w", err)
	}

	if s.backend == BackendKeychain {
		if err := s.ring.Set(keyringService, keyringUser, string(encoded)); err != nil {
			return fmt.Errorf("session: writing to keychain: %w", err)
		}
		return nil
	}

	// 0600 written by WriteFile's mode argument AND re-applied by Chmod, because the
	// process umask masks WriteFile's mode: with umask 0 the file would be 0600 anyway,
	// but under an unusual umask it can be created MORE permissively than requested.
	// Chmod is not subject to the umask.
	path := filepath.Join(s.dir, credentialFile)
	if err := os.WriteFile(path, encoded, 0o600); err != nil {
		return fmt.Errorf("session: writing credential file: %w", err)
	}
	if err := os.Chmod(path, 0o600); err != nil {
		return fmt.Errorf("session: setting credential file mode: %w", err)
	}
	return nil
}

// Load reads the credential set, refusing a world- or group-readable file.
func (s *FileStore) Load(_ context.Context) (Credentials, error) {
	var c Credentials

	if s.backend == BackendKeychain {
		raw, err := s.ring.Get(keyringService, keyringUser)
		if err != nil {
			if errors.Is(err, keyring.ErrNotFound) {
				return c, ErrNoCredentials
			}
			return c, fmt.Errorf("session: reading from keychain: %w", err)
		}
		if err := json.Unmarshal([]byte(raw), &c); err != nil {
			return c, fmt.Errorf("session: decoding credentials: %w", err)
		}
		return c, nil
	}

	path := filepath.Join(s.dir, credentialFile)
	info, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return c, ErrNoCredentials
		}
		return c, fmt.Errorf("session: stat credential file: %w", err)
	}

	// Checked on EVERY load, not only at save. The mode can change after the file is
	// written — by a careless chmod, a restore from a backup, or a copy onto a share —
	// and the load is the only moment the agent can notice.
	if err := assertOwnerOnly(path, info.Mode().Perm()); err != nil {
		return c, err
	}

	data, err := os.ReadFile(path)
	if err != nil {
		return c, fmt.Errorf("session: reading credential file: %w", err)
	}
	if err := json.Unmarshal(data, &c); err != nil {
		return c, fmt.Errorf("session: decoding credentials: %w", err)
	}
	return c, nil
}

// ClientCertificatePEM satisfies identity.CredentialSource, which is how the mTLS dial
// gets its credential without `identity` importing this package (§10.2).
//
// Returns identity's ErrNoCredential when nothing is stored — not this package's
// ErrNoCredentials — because the caller is the identity provider and its contract is the
// one that has to hold. Both are surfaced by `agent doctor` as "unpaired", and the
// session manager's ErrUnpaired is what distinguishes that from "no backend URL".
func (s *FileStore) ClientCertificatePEM(ctx context.Context) (cert, key, caBundle []byte, err error) {
	c, err := s.Load(ctx)
	if err != nil {
		if errors.Is(err, ErrNoCredentials) {
			return nil, nil, nil, fmt.Errorf("%w: run `forgeops-agent pair`", identity.ErrNoCredential)
		}
		return nil, nil, nil, err
	}
	if len(c.ClientCert) == 0 || len(c.ClientKey) == 0 {
		return nil, nil, nil, fmt.Errorf(
			"%w: stored credentials carry no certificate", identity.ErrNoCredential)
	}
	return c.ClientCert, c.ClientKey, c.CABundle, nil
}

// Wipe removes the stored credentials.
//
// Called on revocation, so it must succeed when there is nothing to remove: an agent
// told it is revoked has to reach the unpaired state regardless of what it finds.
func (s *FileStore) Wipe(_ context.Context) error {
	if s.backend == BackendKeychain {
		if err := s.ring.Delete(keyringService, keyringUser); err != nil {
			if errors.Is(err, keyring.ErrNotFound) {
				return nil
			}
			return fmt.Errorf("session: deleting from keychain: %w", err)
		}
		return nil
	}

	if err := os.Remove(filepath.Join(s.dir, credentialFile)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("session: removing credential file: %w", err)
	}
	return nil
}

// assertOwnerOnly refuses a credential file any other user can read.
//
// Skipped on Windows, and the reason is recorded rather than the check silently doing
// nothing: NTFS access is governed by ACLs, and Go reports a synthetic 0666 for most
// files, so a permission-bit test there would reject every valid file. Windows protection
// comes from the state directory living under the user's AppData and from the Credential
// Manager being the default backend on that platform.
func assertOwnerOnly(path string, mode fs.FileMode) error {
	if runtime.GOOS == "windows" {
		return nil
	}
	if mode&0o077 != 0 {
		return fmt.Errorf(
			"%w: %s is mode %#o; expected 0600. The credential has been exposed to other "+
				"users on this machine, so re-pair rather than only fixing the mode",
			ErrInsecurePermissions, path, mode,
		)
	}
	return nil
}
