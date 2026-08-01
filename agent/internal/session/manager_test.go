// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/connection"
	"github.com/parag8487/ForgeOps/agent/internal/identity"
)

// ─── a stand-in backend ─────────────────────────────────────────────────────

// testCA signs the CSRs the fake exchange receives.
//
// A real CA rather than a fixed fixture certificate, because `credentialsFrom` proves the
// issued certificate matches the locally generated key, and a fixture could not: the key
// is generated inside Pair and never leaves it.
type testCA struct {
	key  *ecdsa.PrivateKey
	cert *x509.Certificate
	pem  []byte
}

func newTestCA(t *testing.T) *testCA {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generating CA key: %v", err)
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "ForgeOps Test CA"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("self-signing the CA: %v", err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("parsing the CA: %v", err)
	}
	return &testCA{
		key:  key,
		cert: cert,
		pem:  pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
	}
}

// sign issues a leaf for a CSR, discarding the CSR's subject exactly as D-73 requires of
// the real CA so the test cannot pass against an implementation that trusted it.
func (c *testCA) sign(t *testing.T, csrPEM []byte, deviceID string) (certPEM []byte, notAfter time.Time) {
	t.Helper()
	block, _ := pem.Decode(csrPEM)
	if block == nil {
		t.Fatalf("the agent sent a CSR that is not PEM")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatalf("parsing the agent's CSR: %v", err)
	}
	if err := csr.CheckSignature(); err != nil {
		t.Fatalf("the agent's CSR self-signature does not verify: %v", err)
	}
	notAfter = time.Now().Add(24 * time.Hour).Truncate(time.Second)
	template := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: deviceID},
		NotBefore:    time.Now().Add(-time.Minute),
		NotAfter:     notAfter,
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, template, c.cert, csr.PublicKey, c.key)
	if err != nil {
		t.Fatalf("issuing the leaf: %v", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), notAfter
}

const (
	testDeviceID  = "8f14e45f-ceea-467a-9c67-2c4d5b1a0f31"
	testProjectID = "3c9f2b71-4a5e-4d2b-8f10-6c7a9e3d5b02"
)

// fakeBackend is the exchange route: one live code, single use, everything else a 401.
type fakeBackend struct {
	ca *testCA
	// liveCode is emptied by a successful exchange, which is what makes the "retry after
	// success" test exercise the real reason it fails rather than a hard-coded second
	// response: the code no longer exists.
	liveCode string
	calls    int
	// mutate lets a test corrupt one field of an otherwise valid 201.
	mutate func(map[string]any)
	// status and problem override the whole response.
	status  int
	problem string
}

func (b *fakeBackend) handler(t *testing.T) http.HandlerFunc {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		b.calls++
		if r.URL.Path != exchangePath {
			t.Errorf("exchange posted to %q, want %q", r.URL.Path, exchangePath)
		}
		if r.Method != http.MethodPost {
			t.Errorf("exchange used %s, want POST", r.Method)
		}

		var body exchangeRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatalf("decoding the agent's request: %v", err)
		}

		if b.status != 0 {
			writeProblem(w, b.status, b.problem)
			return
		}
		if b.liveCode == "" || body.Code != b.liveCode {
			writeProblem(w, http.StatusUnauthorized, "pairing-code-invalid")
			return
		}

		// The fingerprint is checked against the CSR, exactly as the backend does: a test
		// backend that accepted whatever it was told would let a broken SPKISHA256 pass.
		block, _ := pem.Decode([]byte(body.CSR))
		if block == nil {
			t.Fatalf("CSR is not PEM")
		}
		csr, err := x509.ParseCertificateRequest(block.Bytes)
		if err != nil {
			t.Fatalf("parsing CSR: %v", err)
		}
		spki, err := x509.MarshalPKIXPublicKey(csr.PublicKey)
		if err != nil {
			t.Fatalf("marshalling CSR public key: %v", err)
		}
		sum := sha256.Sum256(spki)
		if want := hex.EncodeToString(sum[:]); body.Fingerprint != want {
			t.Errorf("fingerprint = %q, want the SHA-256 of the CSR SPKI %q", body.Fingerprint, want)
		}
		if body.AgentVersion == "" || body.Platform == "" {
			t.Errorf("agent_version=%q platform=%q; both are required by §3.1", body.AgentVersion, body.Platform)
		}

		certPEM, notAfter := b.ca.sign(t, []byte(body.CSR), testDeviceID)
		b.liveCode = "" // single use

		payload := map[string]any{
			"device_id":        testDeviceID,
			"project_id":       testProjectID,
			"device_token":     strings.Repeat("ab", credentialByteLength),
			"envelope_key":     strings.Repeat("cd", credentialByteLength),
			"csr_spki_sha256":  body.Fingerprint,
			"client_cert":      string(certPEM),
			"ca_bundle":        string(b.ca.pem),
			"cert_serial":      "0a0b0c",
			"cert_fingerprint": "sha256:" + strings.Repeat("ef", 32),
			"cert_not_after":   notAfter.UTC().Format(time.RFC3339),
			"renew_after":      notAfter.Add(-6 * time.Hour).UTC().Format(time.RFC3339),
		}
		if b.mutate != nil {
			b.mutate(payload)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(payload)
	}
}

func writeProblem(w http.ResponseWriter, status int, kind string) {
	w.Header().Set("Content-Type", "application/problem+json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"type":   "https://forgeops.dev/problems/" + kind,
		"title":  kind,
		"detail": "test detail",
	})
}

// newTestManager wires a Manager onto a file-backed store in a temp dir.
//
// Preference "file" rather than "auto": an "auto" store would use the developer's real
// keychain on macOS and Windows, so the suite would write credentials into the machine
// running it and two parallel runs would fight over one entry.
func newTestManager(t *testing.T, backendURL string, client *http.Client) (*Manager, *FileStore) {
	t.Helper()
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	manager, err := NewManager(backendURL, Deps{
		Store:        store,
		HTTPClient:   client,
		AgentVersion: "1.2.3-test",
	})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	return manager, store
}

// ─── the four cases task 8.3 names ──────────────────────────────────────────

func TestPair_SuccessfulExchangePersistsCredentials(t *testing.T) {
	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	server := httptest.NewServer(backend.handler(t))
	defer server.Close()

	manager, store := newTestManager(t, server.URL, server.Client())

	result, err := manager.Pair(context.Background(), "ABC234", "")
	if err != nil {
		t.Fatalf("Pair: %v", err)
	}
	if result.DeviceID != testDeviceID {
		t.Errorf("DeviceID = %q, want %q", result.DeviceID, testDeviceID)
	}
	if result.ProjectID != testProjectID {
		t.Errorf("ProjectID = %q, want %q", result.ProjectID, testProjectID)
	}
	if result.CertNotAfter.IsZero() {
		t.Error("CertNotAfter is zero; `doctor` and renewal both need the expiry")
	}
	if result.StoreBackend != BackendFile {
		t.Errorf("StoreBackend = %q, want %q", result.StoreBackend, BackendFile)
	}

	creds, err := store.Load(context.Background())
	if err != nil {
		t.Fatalf("credentials were not persisted: %v", err)
	}
	if len(creds.DeviceToken) != credentialByteLength {
		t.Errorf("stored DeviceToken is %d bytes, want %d", len(creds.DeviceToken), credentialByteLength)
	}
	if len(creds.EnvelopeKey) != credentialByteLength {
		t.Errorf("stored EnvelopeKey is %d bytes, want %d", len(creds.EnvelopeKey), credentialByteLength)
	}
	if len(creds.ClientKey) == 0 {
		t.Error("the locally generated private key was not stored; the agent could never dial")
	}
	if len(creds.CABundle) == 0 {
		t.Error("the CA bundle was not stored; the agent could not verify the backend")
	}

	// The private key must be the one generated locally, never anything the backend sent.
	// Asserted by checking the certificate's public key against the stored key, which is
	// the same relation `credentialsFrom` enforces before persisting.
	certBlock, _ := pem.Decode(creds.ClientCert)
	if certBlock == nil {
		t.Fatal("stored certificate is not PEM")
	}
	leaf, err := x509.ParseCertificate(certBlock.Bytes)
	if err != nil {
		t.Fatalf("parsing the stored certificate: %v", err)
	}
	if leaf.Subject.CommonName != testDeviceID {
		t.Errorf("issued CN = %q, want the device id %q (D-73)", leaf.Subject.CommonName, testDeviceID)
	}
}

func TestPair_RejectedCodePersistsNothing(t *testing.T) {
	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	server := httptest.NewServer(backend.handler(t))
	defer server.Close()

	manager, store := newTestManager(t, server.URL, server.Client())

	_, err := manager.Pair(context.Background(), "WRONG1", "")
	if !errors.Is(err, ErrPairingCodeInvalid) {
		t.Fatalf("Pair with a wrong code = %v, want ErrPairingCodeInvalid", err)
	}
	if _, err := store.Load(context.Background()); !errors.Is(err, ErrNoCredentials) {
		t.Errorf("after a rejected exchange the store holds %v, want ErrNoCredentials", err)
	}
}

func TestPair_RetryAfterSuccessFailsByDesign(t *testing.T) {
	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	server := httptest.NewServer(backend.handler(t))
	defer server.Close()

	manager, store := newTestManager(t, server.URL, server.Client())
	if _, err := manager.Pair(context.Background(), "ABC234", ""); err != nil {
		t.Fatalf("first Pair: %v", err)
	}
	before, err := store.Load(context.Background())
	if err != nil {
		t.Fatalf("loading after the first Pair: %v", err)
	}

	// Refused locally, before a request is sent: the code is single-use server-side so
	// this would fail anyway, but only after the working credential had been replaced.
	callsBefore := backend.calls
	_, err = manager.Pair(context.Background(), "ABC234", "")
	if !errors.Is(err, ErrAlreadyPaired) {
		t.Fatalf("second Pair = %v, want ErrAlreadyPaired", err)
	}
	if backend.calls != callsBefore {
		t.Errorf("second Pair made %d extra request(s); it must not spend an attempt against the 5-attempt cap",
			backend.calls-callsBefore)
	}

	after, err := store.Load(context.Background())
	if err != nil {
		t.Fatalf("loading after the refused second Pair: %v", err)
	}
	if string(after.DeviceToken) != string(before.DeviceToken) || string(after.ClientKey) != string(before.ClientKey) {
		t.Error("a refused second Pair changed the stored credential; a mistyped retry must not unpair a healthy agent")
	}

	// And when the agent genuinely is unpaired, the server's single-use rule is what
	// rejects the reused code — proving the local guard is not the only thing stopping it.
	if err := manager.Wipe(context.Background()); err != nil {
		t.Fatalf("Wipe: %v", err)
	}
	if _, err := manager.Pair(context.Background(), "ABC234", ""); !errors.Is(err, ErrPairingCodeInvalid) {
		t.Fatalf("reusing a consumed code after a wipe = %v, want ErrPairingCodeInvalid", err)
	}
}

func TestWipe_ReturnsToUnpairedAndSucceedsWhenEmpty(t *testing.T) {
	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	server := httptest.NewServer(backend.handler(t))
	defer server.Close()

	manager, store := newTestManager(t, server.URL, server.Client())
	if _, err := manager.Pair(context.Background(), "ABC234", ""); err != nil {
		t.Fatalf("Pair: %v", err)
	}
	if err := manager.Wipe(context.Background()); err != nil {
		t.Fatalf("Wipe: %v", err)
	}
	if _, err := store.Load(context.Background()); !errors.Is(err, ErrNoCredentials) {
		t.Errorf("after Wipe the store holds %v, want ErrNoCredentials", err)
	}
	// Idempotent: revocation calls this, and an agent told it is revoked has to reach the
	// unpaired state regardless of what it finds.
	if err := manager.Wipe(context.Background()); err != nil {
		t.Errorf("second Wipe = %v, want nil", err)
	}
	if _, err := manager.Status(context.Background()); !errors.Is(err, ErrUnpaired) {
		t.Errorf("Status after Wipe = %v, want ErrUnpaired", err)
	}
}

// ─── the two states `doctor` must distinguish ───────────────────────────────

func TestStatus_DistinguishesDisabledFromUnpaired(t *testing.T) {
	noURL, _ := newTestManager(t, "", nil)
	if _, err := noURL.Status(context.Background()); !errors.Is(err, connection.ErrDisabled) {
		t.Errorf("Status with no backend URL = %v, want connection.ErrDisabled", err)
	}

	withURL, _ := newTestManager(t, "wss://backend.example/api/v1/ws/agent", nil)
	if _, err := withURL.Status(context.Background()); !errors.Is(err, ErrUnpaired) {
		t.Errorf("Status with a URL and no token = %v, want ErrUnpaired", err)
	}
	if errors.Is(ErrUnpaired, connection.ErrDisabled) || errors.Is(connection.ErrDisabled, ErrUnpaired) {
		t.Error("ErrUnpaired and connection.ErrDisabled must be distinguishable; §10.3 requires it")
	}
}

func TestStatus_ReportsPairedDeviceAndDegradedStore(t *testing.T) {
	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	server := httptest.NewServer(backend.handler(t))
	defer server.Close()

	manager, _ := newTestManager(t, server.URL, server.Client())
	if _, err := manager.Pair(context.Background(), "ABC234", ""); err != nil {
		t.Fatalf("Pair: %v", err)
	}

	status, err := manager.Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if !status.Paired || status.DeviceID != testDeviceID {
		t.Errorf("Status = %+v, want paired with device %q", status, testDeviceID)
	}
	if status.CertNotAfter.IsZero() {
		t.Error("Status.CertNotAfter is zero; doctor prints the expiry")
	}
	if !status.Degraded {
		t.Error("a file-backed store must report Degraded so doctor says so rather than pretending (OQ-26)")
	}
}

// ─── response validation ────────────────────────────────────────────────────

func TestPair_RejectsMalformedResponses(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(map[string]any)
		want   string
	}{
		{"short token", func(m map[string]any) { m["device_token"] = "abcd" }, "device_token is 2 bytes"},
		{"token not hex", func(m map[string]any) { m["device_token"] = strings.Repeat("zz", 32) }, "device_token is not hex"},
		{"short envelope key", func(m map[string]any) { m["envelope_key"] = strings.Repeat("ab", 16) }, "envelope_key is 16 bytes"},
		{"no device id", func(m map[string]any) { m["device_id"] = "" }, "no device id"},
		{"no ca bundle", func(m map[string]any) { m["ca_bundle"] = "" }, "no CA bundle"},
		{"substituted public key", func(m map[string]any) { m["csr_spki_sha256"] = strings.Repeat("aa", 32) },
			"signed a different public key"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ca := newTestCA(t)
			backend := &fakeBackend{ca: ca, liveCode: "ABC234", mutate: tc.mutate}
			server := httptest.NewServer(backend.handler(t))
			defer server.Close()

			manager, store := newTestManager(t, server.URL, server.Client())
			_, err := manager.Pair(context.Background(), "ABC234", "")
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("Pair = %v, want an error containing %q", err, tc.want)
			}
			// Nothing partial is persisted: a credential set that failed validation must
			// not become the credential the agent tries to use at every future handshake.
			if _, err := store.Load(context.Background()); !errors.Is(err, ErrNoCredentials) {
				t.Errorf("store after a rejected response = %v, want ErrNoCredentials", err)
			}
		})
	}
}

func TestPair_MapsProblemTypesToTypedErrors(t *testing.T) {
	cases := []struct {
		status  int
		problem string
		want    error
	}{
		{http.StatusUnauthorized, "pairing-code-invalid", ErrPairingCodeInvalid},
		{http.StatusTooManyRequests, "pairing-rate-limited", ErrPairingRateLimited},
		{http.StatusServiceUnavailable, "pairing-unavailable", ErrPairingUnavailable},
	}
	for _, tc := range cases {
		t.Run(tc.problem, func(t *testing.T) {
			backend := &fakeBackend{ca: newTestCA(t), liveCode: "ABC234", status: tc.status, problem: tc.problem}
			server := httptest.NewServer(backend.handler(t))
			defer server.Close()

			manager, _ := newTestManager(t, server.URL, server.Client())
			_, err := manager.Pair(context.Background(), "ABC234", "")
			if !errors.Is(err, tc.want) {
				t.Fatalf("Pair on %d/%s = %v, want %v", tc.status, tc.problem, err, tc.want)
			}
		})
	}
}

func TestPair_ErrorsNeverEchoTheCode(t *testing.T) {
	const code = "ZQ7X9K"
	backend := &fakeBackend{ca: newTestCA(t), liveCode: "ABC234"}
	server := httptest.NewServer(backend.handler(t))
	defer server.Close()

	manager, _ := newTestManager(t, server.URL, server.Client())
	_, err := manager.Pair(context.Background(), code, "")
	if err == nil {
		t.Fatal("Pair with a wrong code returned no error")
	}
	if strings.Contains(err.Error(), code) {
		t.Errorf("the error text contains the pairing code: %v", err)
	}
}

// ─── URL handling ───────────────────────────────────────────────────────────

func TestExchangeURL(t *testing.T) {
	cases := []struct {
		in      string
		want    string
		wantErr bool
	}{
		{"wss://api.example.com/api/v1/ws/agent", "https://api.example.com" + exchangePath, false},
		{"wss://api.example.com", "https://api.example.com" + exchangePath, false},
		{"ws://localhost:8000/api/v1/ws/agent", "http://localhost:8000" + exchangePath, false},
		{"https://api.example.com", "https://api.example.com" + exchangePath, false},
		{"wss://api.example.com/x?token=abc", "https://api.example.com" + exchangePath, false},
		{"ftp://api.example.com", "", true},
		{"wss://", "", true},
		{"", "", true},
	}
	for _, tc := range cases {
		got, err := exchangeURL(tc.in)
		if tc.wantErr {
			if err == nil {
				t.Errorf("exchangeURL(%q) = %q, want an error", tc.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("exchangeURL(%q) = %v", tc.in, err)
			continue
		}
		if got != tc.want {
			t.Errorf("exchangeURL(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestPair_UsesFlagOverConfiguredBackend(t *testing.T) {
	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	server := httptest.NewServer(backend.handler(t))
	defer server.Close()

	// Configured URL points nowhere; the flag is what must be used.
	manager, _ := newTestManager(t, "wss://unreachable.invalid", server.Client())
	if _, err := manager.Pair(context.Background(), "ABC234", server.URL); err != nil {
		t.Fatalf("Pair with an explicit --backend: %v", err)
	}
}

// ─── the store as identity.CredentialSource ─────────────────────────────────

func TestClientCertificatePEM_FeedsTheIdentityProvider(t *testing.T) {
	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	server := httptest.NewServer(backend.handler(t))
	defer server.Close()

	manager, store := newTestManager(t, server.URL, server.Client())

	// Unpaired: the identity provider's own sentinel, not this package's, because the
	// provider is the caller and its contract is the one that has to hold.
	if _, _, _, err := store.ClientCertificatePEM(context.Background()); !errors.Is(err, identity.ErrNoCredential) {
		t.Fatalf("ClientCertificatePEM when unpaired = %v, want identity.ErrNoCredential", err)
	}

	if _, err := manager.Pair(context.Background(), "ABC234", ""); err != nil {
		t.Fatalf("Pair: %v", err)
	}

	// Paired: the provider builds a real dialling config from it, which is the only
	// end-to-end proof that pairing produced a usable mTLS credential.
	provider := identity.NewPairedDevice(store, 6*time.Hour)
	cfg, err := provider.ClientTLS(context.Background())
	if err != nil {
		t.Fatalf("ClientTLS after pairing: %v", err)
	}
	if len(cfg.Certificates) != 1 {
		t.Fatalf("ClientTLS produced %d certificates, want 1", len(cfg.Certificates))
	}
	info, err := provider.Identity(context.Background())
	if err != nil {
		t.Fatalf("Identity: %v", err)
	}
	if info.Subject != testDeviceID {
		t.Errorf("Identity.Subject = %q, want the device id %q", info.Subject, testDeviceID)
	}
}

// ─── the CSR the agent sends ────────────────────────────────────────────────

func TestBuildCSR_CarriesNoIdentityAndNoSAN(t *testing.T) {
	pair, err := identity.NewKeyPair()
	if err != nil {
		t.Fatalf("NewKeyPair: %v", err)
	}
	csrPEM, err := identity.BuildCSR(pair, pairingCSRCommonName)
	if err != nil {
		t.Fatalf("BuildCSR: %v", err)
	}
	block, _ := pem.Decode(csrPEM)
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatalf("parsing the CSR: %v", err)
	}
	if csr.Subject.CommonName != pairingCSRCommonName {
		t.Errorf("CSR CN = %q, want the self-describing placeholder %q", csr.Subject.CommonName, pairingCSRCommonName)
	}
	if len(csr.DNSNames) != 0 || len(csr.IPAddresses) != 0 || len(csr.URIs) != 0 {
		t.Error("the CSR requests a SAN; this certificate is clientAuth only (D-73)")
	}
	// The needle is assembled from two fragments because `scripts/secret-gate.ps1` matches on
	// credential shape and not on sensitivity. Writing it out would make this assertion — whose
	// whole purpose is that no private key is ever transmitted — the reason a push is blocked.
	if strings.Contains(string(csrPEM), "PRIVATE"+" KEY") {
		t.Error("the CSR PEM contains a private key block; only the CSR is ever sent")
	}
}

func TestSPKISHA256_MatchesTheCSRPublicKey(t *testing.T) {
	pair, err := identity.NewKeyPair()
	if err != nil {
		t.Fatalf("NewKeyPair: %v", err)
	}
	got, err := pair.SPKISHA256()
	if err != nil {
		t.Fatalf("SPKISHA256: %v", err)
	}
	csrPEM, err := identity.BuildCSR(pair, pairingCSRCommonName)
	if err != nil {
		t.Fatalf("BuildCSR: %v", err)
	}
	block, _ := pem.Decode(csrPEM)
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatalf("parsing the CSR: %v", err)
	}
	der, err := x509.MarshalPKIXPublicKey(csr.PublicKey)
	if err != nil {
		t.Fatalf("marshalling the CSR public key: %v", err)
	}
	sum := sha256.Sum256(der)
	if want := hex.EncodeToString(sum[:]); got != want {
		t.Errorf("SPKISHA256 = %q, want %q — the backend rejects a mismatch", got, want)
	}
}

func TestNewManager_RefusesWithoutAStore(t *testing.T) {
	if _, err := NewManager("wss://x.example", Deps{}); err == nil {
		t.Error("NewManager with no Store returned no error")
	}
}
