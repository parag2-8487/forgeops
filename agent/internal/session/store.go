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

	// PolicyBundle and PolicyBundleDigest are the bundle the backend pinned to this device at
	// the pairing exchange (§10.6, D-30).
	//
	// They belong in the credential set and not beside it, because they are pinned by the same
	// single-use exchange that issues the token: the backend writes
	// `agent_devices.policy_bundle_digest` in that transaction, the governance chokepoint then
	// admits a submission only when the device's pin equals the project's active digest, and
	// every minted envelope carries that digest in `policy_context.bundle_digest`. Storing the
	// digest anywhere else would let the two drift, and a drifted digest refuses every command
	// with `policy-bundle-stale` — a correct refusal about a fact the agent got wrong.
	//
	// This does NOT widen what can be replayed, which is the bound the type comment sets. A
	// bundle digest is a public content hash and the bundle is signed policy the backend
	// published; neither is an authority to do anything, unlike the cached envelope D-41
	// refuses to store.
	PolicyBundle       []byte `json:"policy_bundle,omitempty"`
	PolicyBundleDigest string `json:"policy_bundle_digest,omitempty"`

	// SessionWSURL is where this device holds its authenticated session, as stated by the
	// backend that issued the certificate above.
	//
	// WHY IT IS STORED WITH THE CREDENTIAL rather than read from configuration. It is pinned by
	// the same single-use exchange that issues the certificate and the CA bundle, and it is only
	// meaningful together with them: this URL names a listener that requires THIS certificate and
	// is verified against THAT CA. Keeping the three together means an agent cannot be pointed at
	// a listener it holds no certificate for, and re-pairing against a different deployment
	// replaces all three at once.
	//
	// Empty means the backend stated none, which is what a backend older than this field does.
	// `SessionURL` then falls back to the configured URL, so an older deployment keeps working.
	SessionWSURL string `json:"session_ws_url,omitempty"`

	// ProjectID is the project this device was issued for.
	//
	// WHY IT IS STORED AT ALL. A device certificate authorises exactly one project, and the backend
	// refuses a submission for any other with a 403. Without this field the agent could not know
	// which project it belonged to, so `connect --project <other>` took its "already paired"
	// shortcut, scanned the wrong tree and reported
	//
	//     the backend refused the scan report (403): Forbidden You do not have permission to
	//     perform this action
	//
	// which names neither the project nor the credential. The exchange response has always carried
	// `project_id`; it was simply discarded.
	//
	// Empty means a credential written before this field existed. `connect` then cannot compare, so
	// it proceeds exactly as it used to rather than refusing on a fact it does not have.
	ProjectID string `json:"project_id,omitempty"`
}

// Store persists the device credential set.
type Store interface {
	Save(ctx context.Context, c Credentials) error
	Load(ctx context.Context) (Credentials, error)
	Wipe(ctx context.Context) error
	Backend() string // "keychain" | "file(0600)" — surfaced by agent doctor

	// CheckCapacity reports whether a credential of this shape can be persisted at all.
	//
	// On the interface rather than only on the concrete type because the pairing exchange
	// must be able to ask BEFORE it spends the code, and the manager holds a Store.
	CheckCapacity(ctx context.Context, c Credentials) error
}

var (
	// ErrNoCredentials means nothing has been stored: the agent is unpaired.
	ErrNoCredentials = errors.New("session: no stored credentials")

	// ErrInsecurePermissions means the credential file is readable by somebody other
	// than its owner. Refused rather than repaired, because by the time it is observed
	// the secret has already been exposed and rewriting the mode would hide that.
	ErrInsecurePermissions = errors.New("session: credential file is not owner-only")

	// ErrStoreTooSmall means the OS credential store refused the credential for its size.
	//
	// Distinguished from any other write failure because it is the one failure with a
	// remedy the user can apply — AGENT_CREDENTIAL_STORE=file — and because `agent doctor`
	// reports it as a prediction before the user spends a pairing code on it.
	ErrStoreTooSmall = errors.New("session: the credential store cannot hold this credential")

	// ErrCredentialsIncomplete means one half of the split credential is present and the
	// other is not: a token with no certificate, which no amount of retrying will fix.
	ErrCredentialsIncomplete = errors.New("session: stored credentials are incomplete")
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

	// publicFile holds the half of the credential set that is not secret.
	//
	// SPLIT BECAUSE THE WHOLE SET CANNOT FIT IN A KEYCHAIN ENTRY, AND PAIRING ON WINDOWS
	// COULD THEREFORE NEVER SUCCEED.
	//
	// `zalando/go-keyring` refuses a value over 2560 bytes on Windows before it calls
	// `CredWriteW`, because `CRED_MAX_CREDENTIAL_BLOB_SIZE` is 2560 — and it counts RAW
	// UTF-8 BYTES, not UTF-16 code units. Measured on Windows 11 with go-keyring v0.2.6:
	// 2560 single-byte characters are accepted and 2561 are refused; 1280 two-byte runes
	// are accepted and 1281 are refused. Both are the same 2560-byte ceiling.
	//
	// The full set is far past that, and the policy bundle alone settles it: it is a
	// gzipped tar of the project's rego, base64-encoded into JSON, against ~64 KB of
	// source. The two PEM chains add roughly a kilobyte more. So `pair` on Windows failed
	// AFTER the exchange with "data passed to Set was too big", having consumed the
	// single-use code.
	//
	// What goes where follows from what is actually secret. The device token, the envelope
	// key and the client PRIVATE key are secrets and stay in the keychain — 485 bytes
	// measured with a real P-256 key, comfortably inside 2560 with room for a longer
	// device id. The client certificate, the CA bundle and the policy bundle are public by
	// construction: a certificate is published to whoever asks for it during a handshake,
	// and the policy bundle is signed policy the backend serves to every device pinned to
	// that digest. Storing them beside the keychain rather than inside it removes nothing
	// an attacker did not already have.
	//
	// Chunking across several keychain entries was the alternative and was rejected: a
	// partial write becomes a new half-paired state, which is the failure this change
	// exists to remove rather than relocate.
	publicFile = "credentials-public.json"
)

// secretPart is the half held in the OS keychain: everything whose disclosure would let
// somebody else be this device.
type secretPart struct {
	DeviceID    string `json:"device_id"`
	DeviceToken []byte `json:"device_token"`
	EnvelopeKey []byte `json:"envelope_key"`
	ClientKey   []byte `json:"client_key"`
}

// publicPart is the half held in the 0600 state file: material that is already public, or
// that is published to any peer that completes a handshake.
type publicPart struct {
	ClientCert         []byte `json:"client_cert"`
	CABundle           []byte `json:"ca_bundle"`
	PolicyBundle       []byte `json:"policy_bundle,omitempty"`
	PolicyBundleDigest string `json:"policy_bundle_digest,omitempty"`
	// An address, not a secret: it names a listener that demands a client certificate, so
	// knowing it grants nothing. It also belongs on this side because the keychain half is
	// budgeted to 2560 bytes and a URL there would spend that budget for no benefit.
	SessionWSURL string `json:"session_ws_url,omitempty"`
	// Also not a secret: the project id is in every URL the operator already sees, and the
	// certificate is what authorises anything, not knowledge of the identifier.
	ProjectID string `json:"project_id,omitempty"`
}

func split(c Credentials) (secretPart, publicPart) {
	return secretPart{
			DeviceID:    c.DeviceID,
			DeviceToken: c.DeviceToken,
			EnvelopeKey: c.EnvelopeKey,
			ClientKey:   c.ClientKey,
		}, publicPart{
			ClientCert:         c.ClientCert,
			CABundle:           c.CABundle,
			PolicyBundle:       c.PolicyBundle,
			PolicyBundleDigest: c.PolicyBundleDigest,
			SessionWSURL:       c.SessionWSURL,
			ProjectID:          c.ProjectID,
		}
}

func join(s secretPart, p publicPart) Credentials {
	return Credentials{
		DeviceID:           s.DeviceID,
		DeviceToken:        s.DeviceToken,
		EnvelopeKey:        s.EnvelopeKey,
		ClientKey:          s.ClientKey,
		ClientCert:         p.ClientCert,
		CABundle:           p.CABundle,
		SessionWSURL:       p.SessionWSURL,
		ProjectID:          p.ProjectID,
		PolicyBundle:       p.PolicyBundle,
		PolicyBundleDigest: p.PolicyBundleDigest,
	}
}

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

// Save persists the credential set, secret half and public half separately.
//
// ORDER MATTERS: the public half is written FIRST. If the secret write then fails, what is
// left on disk is a certificate and a policy bundle with no token and no private key —
// inert material that `Load` reports as unpaired rather than as a usable credential. The
// reverse order would leave a live token in the keychain with no certificate beside it,
// which `Load` cannot distinguish from a keychain the user is expected to trust.
func (s *FileStore) Save(_ context.Context, c Credentials) error {
	secret, public := split(c)

	encodedPublic, err := json.Marshal(public)
	if err != nil {
		return fmt.Errorf("session: encoding public credentials: %w", err)
	}
	if err := s.writeOwnerOnly(filepath.Join(s.dir, publicFile), encodedPublic); err != nil {
		return err
	}

	encodedSecret, err := json.Marshal(secret)
	if err != nil {
		return fmt.Errorf("session: encoding secret credentials: %w", err)
	}

	if s.backend == BackendKeychain {
		if err := s.ring.Set(keyringService, keyringUser, string(encodedSecret)); err != nil {
			return fmt.Errorf("session: writing to keychain (%d bytes): %w", len(encodedSecret), err)
		}
		return nil
	}
	return s.writeOwnerOnly(filepath.Join(s.dir, credentialFile), encodedSecret)
}

// writeOwnerOnly writes 0600.
//
// 0600 is passed to WriteFile AND re-applied by Chmod, because the process umask masks
// WriteFile's mode: with umask 0 the file would be 0600 anyway, but under an unusual umask
// it can be created MORE permissively than requested. Chmod is not subject to the umask.
func (s *FileStore) writeOwnerOnly(path string, data []byte) error {
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return fmt.Errorf("session: writing %s: %w", filepath.Base(path), err)
	}
	if err := os.Chmod(path, 0o600); err != nil {
		return fmt.Errorf("session: setting mode on %s: %w", filepath.Base(path), err)
	}
	return nil
}

// CheckCapacity reports whether this credential set can actually be persisted, by
// performing a real write of the same size to a throwaway slot and removing it.
//
// CALLED BEFORE THE PAIRING EXCHANGE, which is the whole point. The exchange consumes a
// single-use code and issues a certificate; discovering afterwards that the result cannot
// be stored leaves the backend holding an `active` device the agent has no credential for,
// and leaves the user with a burned code and nothing to show for it. That is precisely
// what happened on Windows on every attempt.
//
// A REAL WRITE RATHER THAN A COMPARISON AGAINST A CONSTANT. The 2560-byte Windows ceiling
// is documented, but the numbers that matter are the ones this machine enforces: libsecret
// has no fixed limit and is bounded by D-Bus message size, macOS Keychain accepts far more
// than either, and a constant compiled in here would be a belief about three platforms
// rather than a fact about the one in front of the user. Writing the same number of bytes
// the real save will write is the only check that cannot be wrong.
func (s *FileStore) CheckCapacity(_ context.Context, c Credentials) error {
	secret, public := split(c)

	encodedSecret, err := json.Marshal(secret)
	if err != nil {
		return fmt.Errorf("session: encoding secret credentials: %w", err)
	}
	encodedPublic, err := json.Marshal(public)
	if err != nil {
		return fmt.Errorf("session: encoding public credentials: %w", err)
	}

	// The public half is a file in a directory this process created 0700. Its capacity is
	// disk space, so it is probed by writing the real number of bytes to a temporary name
	// in the same directory and removing it.
	probePath := filepath.Join(s.dir, publicFile+".capacity-probe")
	if err := s.writeOwnerOnly(probePath, encodedPublic); err != nil {
		return fmt.Errorf(
			"session: the state directory cannot hold the %d-byte certificate and policy bundle: %w",
			len(encodedPublic), err)
	}
	if err := os.Remove(probePath); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("session: removing capacity probe: %w", err)
	}

	if s.backend != BackendKeychain {
		return nil
	}

	// A DISTINCT SLOT, so a probe can never overwrite a live credential. `keyringUser` is
	// deliberately not reused: this runs on an unpaired agent, but a concurrent `pair` or a
	// retry must not be able to turn a probe into the stored credential.
	const probeUser = keyringUser + "-capacity-probe"
	if err := s.ring.Set(keyringService, probeUser, string(encodedSecret)); err != nil {
		return fmt.Errorf(
			"%w: this credential is %d bytes and the %s backend refused it. "+
				"Set AGENT_CREDENTIAL_STORE=file to store credentials in a 0600 file under the "+
				"state directory instead: %w",
			ErrStoreTooSmall, len(encodedSecret), s.backend, err)
	}
	_ = s.ring.Delete(keyringService, probeUser)
	return nil
}

// Load reads the credential set, refusing a world- or group-readable file.
func (s *FileStore) Load(_ context.Context) (Credentials, error) {
	var c Credentials

	secret, err := s.loadSecret()
	if err != nil {
		return c, err
	}

	public, err := s.loadPublic()
	if err != nil {
		return c, err
	}

	return join(secret, public), nil
}

func (s *FileStore) loadSecret() (secretPart, error) {
	var secret secretPart

	if s.backend == BackendKeychain {
		raw, err := s.ring.Get(keyringService, keyringUser)
		if err != nil {
			if errors.Is(err, keyring.ErrNotFound) {
				return secret, ErrNoCredentials
			}
			return secret, fmt.Errorf("session: reading from keychain: %w", err)
		}
		if err := json.Unmarshal([]byte(raw), &secret); err != nil {
			return secret, fmt.Errorf("session: decoding secret credentials: %w", err)
		}
		return secret, nil
	}

	data, err := s.readOwnerOnly(filepath.Join(s.dir, credentialFile))
	if err != nil {
		return secret, err
	}
	if err := json.Unmarshal(data, &secret); err != nil {
		return secret, fmt.Errorf("session: decoding secret credentials: %w", err)
	}
	return secret, nil
}

// loadPublic reads the certificate half.
//
// A MISSING PUBLIC HALF BESIDE A PRESENT SECRET HALF IS REFUSED BY NAME, not silently
// treated as unpaired and not silently treated as paired. It means the state directory was
// cleared while the keychain entry survived — an ordinary thing to do by hand on Windows,
// where the two live in different places — and the agent cannot mint a certificate for
// itself, so the only way forward is to re-pair. Saying so is the difference between one
// clear instruction and a mTLS handshake failure the user has to work backwards from.
func (s *FileStore) loadPublic() (publicPart, error) {
	var public publicPart

	data, err := s.readOwnerOnly(filepath.Join(s.dir, publicFile))
	if err != nil {
		if errors.Is(err, ErrNoCredentials) {
			return public, fmt.Errorf(
				"%w: the secret half is stored in the %s backend but %s is missing from %s, so "+
					"this agent has a device token and no certificate. Run "+
					"`forgeops-agent pair --wipe` and pair again",
				ErrCredentialsIncomplete, s.backend, publicFile, s.dir)
		}
		return public, err
	}
	if err := json.Unmarshal(data, &public); err != nil {
		return public, fmt.Errorf("session: decoding public credentials: %w", err)
	}
	return public, nil
}

// readOwnerOnly reads a file, refusing one that others can read.
func (s *FileStore) readOwnerOnly(path string) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, ErrNoCredentials
		}
		return nil, fmt.Errorf("session: stat %s: %w", filepath.Base(path), err)
	}

	// Checked on EVERY load, not only at save. The mode can change after the file is
	// written — by a careless chmod, a restore from a backup, or a copy onto a share —
	// and the load is the only moment the agent can notice.
	if err := assertOwnerOnly(path, info.Mode().Perm()); err != nil {
		return nil, err
	}

	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("session: reading %s: %w", filepath.Base(path), err)
	}
	return data, nil
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

// Wipe removes the stored credentials, both halves.
//
// Called on revocation, so it must succeed when there is nothing to remove: an agent told
// it is revoked has to reach the unpaired state regardless of what it finds. BOTH halves
// are attempted even if the first fails, and the first error is returned afterwards —
// stopping at the first would leave the other half behind, and a leftover secret half is
// exactly the state `loadPublic` has to refuse.
func (s *FileStore) Wipe(_ context.Context) error {
	var firstErr error

	if s.backend == BackendKeychain {
		if err := s.ring.Delete(keyringService, keyringUser); err != nil && !errors.Is(err, keyring.ErrNotFound) {
			firstErr = fmt.Errorf("session: deleting from keychain: %w", err)
		}
	} else if err := os.Remove(filepath.Join(s.dir, credentialFile)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		firstErr = fmt.Errorf("session: removing credential file: %w", err)
	}

	if err := os.Remove(filepath.Join(s.dir, publicFile)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		if firstErr == nil {
			firstErr = fmt.Errorf("session: removing public credential file: %w", err)
		}
	}

	return firstErr
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
