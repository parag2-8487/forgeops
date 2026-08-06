// SPDX-License-Identifier: Apache-2.0

package identity

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"math/big"
	"strings"
	"testing"
	"time"
)

// The identity seam (design §10.2, §14.3, D-36).
//
// Three things are asserted, and the third is the one that keeps D-36 honest:
// CSR shape (the key never leaves), TLS assembly from an issued certificate, and that a
// credential which does not expire is REFUSED rather than accepted.

// ─── test doubles: a real source, not a mock ────────────────────────────────

// staticSource is a real CredentialSource over fixed PEM. A mock would accept any call
// shape at all, which is the D-23 hole; a two-line struct cannot drift from the
// interface.
type staticSource struct {
	cert, key, ca []byte
	err           error
}

func (s staticSource) ClientCertificatePEM(context.Context) (cert, key, caBundle []byte, err error) {
	return s.cert, s.key, s.ca, s.err
}

// issueCertificate mints a self-signed leaf for `commonName` valid until notAfter, and
// returns the leaf PEM, its key PEM, and a CA bundle (itself, since it is self-signed).
//
// Generated per test rather than committed as a fixture: .antigravity/steering/secret-safety.md
// forbids a committed key, and a per-run key also means a test cannot accidentally
// depend on a specific serial or fingerprint.
func issueCertificate(t *testing.T, commonName string, notBefore, notAfter time.Time) (certPEM, keyPEM []byte) {
	t.Helper()

	private, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generating key: %v", err)
	}

	template := &x509.Certificate{
		SerialNumber:          big.NewInt(time.Now().UnixNano()),
		Subject:               pkix.Name{CommonName: commonName},
		NotBefore:             notBefore,
		NotAfter:              notAfter,
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &private.PublicKey, private)
	if err != nil {
		t.Fatalf("creating certificate: %v", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(private)
	if err != nil {
		t.Fatalf("marshalling key: %v", err)
	}

	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
}

// ─── CSR shape ──────────────────────────────────────────────────────────────

func TestNewKeyPair_GeneratesP256(t *testing.T) {
	t.Parallel()

	pair, err := NewKeyPair()
	if err != nil {
		t.Fatalf("NewKeyPair: %v", err)
	}
	if pair.PublicKey.Curve != elliptic.P256() {
		t.Errorf("curve = %v, want P-256", pair.PublicKey.Curve.Params().Name)
	}
	if !strings.Contains(string(pair.KeyPEM), "EC PRIVATE KEY") {
		t.Error("key PEM is not an EC private key")
	}
}

func TestNewKeyPair_ProducesADistinctKeyEachTime(t *testing.T) {
	t.Parallel()

	// A provider that reused a key would give every device on a machine the same
	// identity, so revoking one would revoke all of them.
	first, err := NewKeyPair()
	if err != nil {
		t.Fatalf("NewKeyPair: %v", err)
	}
	second, err := NewKeyPair()
	if err != nil {
		t.Fatalf("NewKeyPair: %v", err)
	}
	if string(first.KeyPEM) == string(second.KeyPEM) {
		t.Fatal("two calls produced the same private key")
	}
}

func TestBuildCSR_ContainsNoPrivateKey(t *testing.T) {
	t.Parallel()

	// The property D-36 rests on: only a CSR is sent. Asserted on the bytes, because
	// that is what the network sees.
	pair, err := NewKeyPair()
	if err != nil {
		t.Fatalf("NewKeyPair: %v", err)
	}
	csr, err := BuildCSR(pair, "device-01JBQ8Z0")
	if err != nil {
		t.Fatalf("BuildCSR: %v", err)
	}

	text := string(csr)
	for _, forbidden := range []string{"PRIVATE KEY", "EC PRIVATE", "RSA PRIVATE"} {
		if strings.Contains(text, forbidden) {
			t.Errorf("the CSR contains %q", forbidden)
		}
	}
	if !strings.Contains(text, "CERTIFICATE REQUEST") {
		t.Error("the CSR is not a PEM certificate request")
	}
}

func TestBuildCSR_ShapeAndSignature(t *testing.T) {
	t.Parallel()

	pair, err := NewKeyPair()
	if err != nil {
		t.Fatalf("NewKeyPair: %v", err)
	}
	csrPEM, err := BuildCSR(pair, "device-01JBQ8Z0")
	if err != nil {
		t.Fatalf("BuildCSR: %v", err)
	}

	block, _ := pem.Decode(csrPEM)
	if block == nil {
		t.Fatal("CSR did not decode as PEM")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatalf("parsing CSR: %v", err)
	}
	// The signature proves possession of the private key without transmitting it, which
	// is the entire point of a CSR. A request the backend cannot verify is one it must
	// refuse, so this is asserted here rather than discovered during pairing.
	if err := csr.CheckSignature(); err != nil {
		t.Fatalf("CSR signature does not verify: %v", err)
	}
	if csr.Subject.CommonName != "device-01JBQ8Z0" {
		t.Errorf("CommonName = %q", csr.Subject.CommonName)
	}
	if csr.SignatureAlgorithm != x509.ECDSAWithSHA256 {
		t.Errorf("SignatureAlgorithm = %v, want ECDSAWithSHA256", csr.SignatureAlgorithm)
	}
	// No SANs requested: this certificate authenticates a client, so a DNS or IP SAN
	// would be an unused field a future verifier might start trusting.
	if len(csr.DNSNames)+len(csr.IPAddresses)+len(csr.EmailAddresses)+len(csr.URIs) != 0 {
		t.Errorf("the CSR requests SANs it does not need: %v %v", csr.DNSNames, csr.IPAddresses)
	}
}

func TestBuildCSR_RejectsMissingInputs(t *testing.T) {
	t.Parallel()

	pair, err := NewKeyPair()
	if err != nil {
		t.Fatalf("NewKeyPair: %v", err)
	}
	if _, err := BuildCSR(nil, "device"); err == nil {
		t.Error("BuildCSR(nil, ...) must fail")
	}
	if _, err := BuildCSR(pair, ""); err == nil {
		t.Error("BuildCSR with no common name must fail")
	}
}

// ─── TLS assembly ───────────────────────────────────────────────────────────

func TestPairedDevice_ClientTLSAssemblesTheIssuedCertificate(t *testing.T) {
	t.Parallel()

	now := time.Now()
	certPEM, keyPEM := issueCertificate(t, "device-abc", now.Add(-time.Minute), now.Add(20*time.Hour))
	provider := NewPairedDevice(staticSource{cert: certPEM, key: keyPEM, ca: certPEM}, 6*time.Hour)

	cfg, err := provider.ClientTLS(context.Background())
	if err != nil {
		t.Fatalf("ClientTLS: %v", err)
	}
	if len(cfg.Certificates) != 1 {
		t.Fatalf("expected one certificate, got %d", len(cfg.Certificates))
	}
	if cfg.Certificates[0].Leaf == nil {
		t.Error("Leaf was not populated; every handshake would re-parse the certificate")
	}
	if cfg.RootCAs == nil {
		t.Error("RootCAs is nil; the backend would not be verified")
	}
	// TLS 1.3 floor: §14.1 requires mTLS, and negotiating down to 1.2 would permit
	// cipher suites this project has no reason to accept.
	if cfg.MinVersion != tls.VersionTLS13 {
		t.Errorf("MinVersion = %x, want TLS 1.3", cfg.MinVersion)
	}
}

func TestPairedDevice_ClientTLSRefusesAMismatchedPair(t *testing.T) {
	t.Parallel()

	now := time.Now()
	certPEM, _ := issueCertificate(t, "device-abc", now, now.Add(time.Hour))
	_, otherKeyPEM := issueCertificate(t, "device-xyz", now, now.Add(time.Hour))

	provider := NewPairedDevice(staticSource{cert: certPEM, key: otherKeyPEM, ca: certPEM}, time.Hour)
	if _, err := provider.ClientTLS(context.Background()); err == nil {
		t.Fatal("a certificate and an unrelated key must not form a usable config")
	}
}

func TestPairedDevice_ClientTLSReportsNoCredential(t *testing.T) {
	t.Parallel()

	// An unpaired agent must be distinguishable from a broken one, because `agent
	// doctor` tells the user to run `pair` in the first case and something else in the
	// second (§10.10).
	provider := NewPairedDevice(staticSource{}, time.Hour)
	_, err := provider.ClientTLS(context.Background())
	if !errors.Is(err, ErrNoCredential) {
		t.Fatalf("err = %v, want ErrNoCredential", err)
	}
}

func TestPairedDevice_ClientTLSPropagatesSourceFailure(t *testing.T) {
	t.Parallel()

	sentinel := errors.New("keychain locked")
	provider := NewPairedDevice(staticSource{err: sentinel}, time.Hour)
	if _, err := provider.ClientTLS(context.Background()); !errors.Is(err, sentinel) {
		t.Fatalf("err = %v, want the source's error", err)
	}
}

// ─── the no-long-lived-key invariant ────────────────────────────────────────

func TestPairedDevice_RefusesANonExpiringCertificate(t *testing.T) {
	t.Parallel()

	// D-36's invariant, enforced rather than documented. A ten-year certificate is a
	// long-lived agent key by another name.
	now := time.Now()
	certPEM, keyPEM := issueCertificate(t, "device-forever", now, now.Add(10*365*24*time.Hour))
	provider := NewPairedDevice(staticSource{cert: certPEM, key: keyPEM, ca: certPEM}, time.Hour)

	_, err := provider.ClientTLS(context.Background())
	if !errors.Is(err, ErrNonExpiringCredential) {
		t.Fatalf("err = %v, want ErrNonExpiringCredential", err)
	}
}

func TestAssertShortLived_RefusesTheZeroTime(t *testing.T) {
	t.Parallel()

	// The subtle case. A zero NotAfter makes every "is it expired?" comparison answer
	// "no, and never will be", so treating it as valid would silently grant a permanent
	// credential — the precise thing D-36 says must be impossible.
	if err := assertShortLived(time.Time{}, time.Now()); !errors.Is(err, ErrNonExpiringCredential) {
		t.Fatalf("err = %v, want ErrNonExpiringCredential", err)
	}
}

func TestAssertShortLived_AcceptsAConfiguredMaximumTtl(t *testing.T) {
	t.Parallel()

	// 168h is the configuration ceiling (§13.1), so it must be accepted: a backstop that
	// rejected a legitimately configured value would push operators towards disabling it.
	now := time.Now()
	if err := assertShortLived(now.Add(MaxCertificateLifetime-time.Minute), now); err != nil {
		t.Fatalf("a 168h certificate must be accepted: %v", err)
	}
	if err := assertShortLived(now.Add(MaxCertificateLifetime+time.Hour), now); err == nil {
		t.Fatal("a certificate beyond the bound must be refused")
	}
}

// ─── Identity and RenewBefore ───────────────────────────────────────────────

func TestPairedDevice_IdentityReportsTheLeaf(t *testing.T) {
	t.Parallel()

	now := time.Now().Truncate(time.Second)
	notAfter := now.Add(20 * time.Hour)
	certPEM, keyPEM := issueCertificate(t, "device-info", now.Add(-time.Minute), notAfter)

	provider := NewPairedDevice(staticSource{cert: certPEM, key: keyPEM, ca: certPEM}, 6*time.Hour)
	info, err := provider.Identity(context.Background())
	if err != nil {
		t.Fatalf("Identity: %v", err)
	}

	if info.Kind != "paired_device" {
		t.Errorf("Kind = %q", info.Kind)
	}
	if info.Subject != "device-info" {
		t.Errorf("Subject = %q", info.Subject)
	}
	if !strings.HasPrefix(info.Fingerprint, "sha256:") || len(info.Fingerprint) != len("sha256:")+64 {
		t.Errorf("Fingerprint = %q, want sha256: plus 64 hex characters", info.Fingerprint)
	}
	if !info.NotAfter.Equal(notAfter.Truncate(time.Second)) {
		t.Errorf("NotAfter = %v, want %v", info.NotAfter, notAfter)
	}
	_ = keyPEM
}

func TestInfo_ExpiryHelpers(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 7, 30, 12, 0, 0, 0, time.UTC)
	info := Info{NotAfter: now.Add(5 * time.Hour)}

	cases := []struct {
		name    string
		got     bool
		want    bool
		because string
	}{
		{"not yet expired", info.Expired(now), false, "five hours remain"},
		{"expired after NotAfter", info.Expired(now.Add(6 * time.Hour)), true, "past NotAfter"},
		{"renewal not due at 1h window", info.ExpiresWithin(now, time.Hour), false, "5h > 1h"},
		{"renewal due at 6h window", info.ExpiresWithin(now, 6*time.Hour), true, "5h < 6h"},
		{"renewal due exactly at the boundary", info.ExpiresWithin(now, 5*time.Hour), false, "add(5h) == NotAfter, not after it"},
	}
	for _, c := range cases {
		if c.got != c.want {
			t.Errorf("%s = %v, want %v (%s)", c.name, c.got, c.want, c.because)
		}
	}

	// A zero NotAfter must never look like "renewal due": it would make the session
	// manager renew in a tight loop.
	var unset Info
	if unset.Expired(now) || unset.ExpiresWithin(now, time.Hour) {
		t.Error("an unset NotAfter must not report expiry or pending renewal")
	}
}

func TestPairedDevice_RenewBeforeIsTheConfiguredWindow(t *testing.T) {
	t.Parallel()

	provider := NewPairedDevice(staticSource{}, 6*time.Hour)
	if got := provider.RenewBefore(); got != 6*time.Hour {
		t.Errorf("RenewBefore = %v, want 6h", got)
	}
}

func TestPairedDevice_RenewalIsDueBeforeExpiryNotAfterIt(t *testing.T) {
	t.Parallel()

	// The whole point of RenewBefore: the session manager must start renewing while the
	// current certificate still works, so the connection is never dropped (§10.2).
	now := time.Now()
	const ttl = 24 * time.Hour
	const window = 6 * time.Hour

	certPEM, keyPEM := issueCertificate(t, "device-renew", now, now.Add(ttl))
	provider := NewPairedDevice(staticSource{cert: certPEM, key: keyPEM, ca: certPEM}, window)

	info, err := provider.Identity(context.Background())
	if err != nil {
		t.Fatalf("Identity: %v", err)
	}

	// Just outside the window: no renewal yet.
	if info.ExpiresWithin(now, provider.RenewBefore()) {
		t.Error("renewal reported due 24h before expiry with a 6h window")
	}
	// Inside the window, and still valid: this is when renewal must happen.
	atRenewal := now.Add(ttl - window + time.Minute)
	if !info.ExpiresWithin(atRenewal, provider.RenewBefore()) {
		t.Error("renewal not reported due inside the window")
	}
	if info.Expired(atRenewal) {
		t.Error("the certificate is already expired at the renewal point; the window is too small")
	}
}
