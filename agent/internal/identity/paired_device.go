// SPDX-License-Identifier: Apache-2.0

package identity

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"fmt"
	"time"
)

// CredentialSource supplies the credential PairedDevice presents.
//
// Declared here, in the CONSUMER, rather than imported from `session`. That is what keeps
// the dependency pointing one way: `session` imports `identity` to get a Provider, and if
// `identity` imported `session` for its Store the two packages could not compile. The
// session store satisfies this by having the methods, with no import either way.
type CredentialSource interface {
	// ClientCertificatePEM returns the issued leaf certificate, the matching private
	// key, and the CA bundle to verify the backend with. All PEM.
	ClientCertificatePEM(ctx context.Context) (cert, key, caBundle []byte, err error)
}

// PairedDevice presents the client certificate issued during pairing (§3.1).
//
// The private key is generated on this machine and never leaves it: only a CSR is sent.
// That is the property `NewKeyPair`/`BuildCSR` exist to make structural rather than
// procedural — there is no method on this type that returns or serialises a private key
// for transmission, so "the key never leaves" is not a rule somebody has to remember.
type PairedDevice struct {
	source      CredentialSource
	renewBefore time.Duration
	now         func() time.Time
}

// NewPairedDevice builds the laptop-path provider.
//
// `renewBefore` comes from DEVICE_CERT_RENEW_BEFORE_HOURS, which configuration already
// guarantees is smaller than the certificate TTL (§13.1) — so renewal starts before
// expiry rather than after it, which would not be renewal at all.
func NewPairedDevice(source CredentialSource, renewBefore time.Duration) *PairedDevice {
	return &PairedDevice{source: source, renewBefore: renewBefore, now: time.Now}
}

// ClientTLS assembles a dialling config from the issued certificate.
func (p *PairedDevice) ClientTLS(ctx context.Context) (*tls.Config, error) {
	certPEM, keyPEM, caPEM, err := p.source.ClientCertificatePEM(ctx)
	if err != nil {
		return nil, err
	}
	if len(certPEM) == 0 || len(keyPEM) == 0 {
		return nil, ErrNoCredential
	}

	pair, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, fmt.Errorf("identity: certificate and key do not form a pair: %w", err)
	}

	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		return nil, fmt.Errorf("identity: parsing the issued leaf: %w", err)
	}
	// Checked before the config is handed out, not after a connection is attempted: a
	// non-expiring credential must never be USED, and the only way to guarantee that is
	// to refuse to build the config.
	if err := assertShortLived(leaf.NotAfter, p.now()); err != nil {
		return nil, err
	}
	pair.Leaf = leaf

	roots := x509.NewCertPool()
	if len(caPEM) > 0 && !roots.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("identity: CA bundle contains no usable certificate")
	}

	return &tls.Config{
		Certificates: []tls.Certificate{pair},
		RootCAs:      roots,
		MinVersion:   tls.VersionTLS13,
	}, nil
}

// Identity reports who this agent is, for logs and `agent.status`.
func (p *PairedDevice) Identity(ctx context.Context) (Info, error) {
	certPEM, _, _, err := p.source.ClientCertificatePEM(ctx)
	if err != nil {
		return Info{}, err
	}
	if len(certPEM) == 0 {
		return Info{}, ErrNoCredential
	}

	block, _ := pem.Decode(certPEM)
	if block == nil {
		return Info{}, fmt.Errorf("identity: certificate is not PEM")
	}
	leaf, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return Info{}, fmt.Errorf("identity: parsing the issued leaf: %w", err)
	}

	sum := sha256.Sum256(leaf.Raw)
	return Info{
		Kind:        "paired_device",
		Subject:     leaf.Subject.CommonName,
		Fingerprint: "sha256:" + hex.EncodeToString(sum[:]),
		NotAfter:    leaf.NotAfter,
	}, nil
}

// RenewBefore reports how long before expiry renewal should start.
func (p *PairedDevice) RenewBefore() time.Duration { return p.renewBefore }

// ─── key generation and CSR ─────────────────────────────────────────────────

// KeyPair is a freshly generated private key and its PEM encoding.
//
// The private key stays in this struct and in the local store. There is deliberately no
// method that marshals it into anything destined for the network.
type KeyPair struct {
	private   *ecdsa.PrivateKey
	KeyPEM    []byte
	PublicKey *ecdsa.PublicKey
}

// NewKeyPair generates a P-256 key pair in memory.
//
// P-256 rather than RSA: the CSR and the resulting certificate travel over the pairing
// exchange and every mTLS handshake, and an EC key is a fraction of the size at
// equivalent strength. It is also what SPIFFE uses, so both providers present the same
// shape of credential and the backend's verification path does not fork.
func NewKeyPair() (*KeyPair, error) {
	private, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("identity: generating P-256 key: %w", err)
	}
	der, err := x509.MarshalECPrivateKey(private)
	if err != nil {
		return nil, fmt.Errorf("identity: marshalling private key: %w", err)
	}
	return &KeyPair{
		private:   private,
		KeyPEM:    pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der}),
		PublicKey: &private.PublicKey,
	}, nil
}

// SPKISHA256 is the SHA-256 of the public key's SubjectPublicKeyInfo DER, lowercase hex.
//
// This is the `fingerprint` field of §3.1's exchange request. The backend recomputes it
// from the submitted CSR and REJECTS a mismatch rather than storing what it was told, so
// the value has to be computed from the same key the CSR carries — which is why it lives
// on KeyPair rather than being assembled by the caller from whatever is at hand.
//
// The public key, not the private one: this method exists on the type that holds the
// private key precisely so there is one place to see that nothing derived from the
// private half is ever returned.
func (k *KeyPair) SPKISHA256() (string, error) {
	if k == nil || k.PublicKey == nil {
		return "", fmt.Errorf("identity: SPKISHA256 needs a generated key pair")
	}
	der, err := x509.MarshalPKIXPublicKey(k.PublicKey)
	if err != nil {
		return "", fmt.Errorf("identity: marshalling the public key: %w", err)
	}
	sum := sha256.Sum256(der)
	return hex.EncodeToString(sum[:]), nil
}

// BuildCSR produces the certificate request sent during pairing.
//
// Takes the KeyPair and returns PEM. The private key is used to SIGN the request and is
// not included in it — that is what makes "only a CSR is sent" true by construction: the
// caller has nothing else to send.
//
// `commonName` is the device id the backend assigned during the exchange. No SANs are
// requested: this certificate is used for client authentication only, so a DNS or IP SAN
// would be an unused field that a future verifier might start trusting.
func BuildCSR(pair *KeyPair, commonName string) ([]byte, error) {
	if pair == nil || pair.private == nil {
		return nil, fmt.Errorf("identity: BuildCSR needs a generated key pair")
	}
	if commonName == "" {
		return nil, fmt.Errorf("identity: BuildCSR needs a common name")
	}

	template := &x509.CertificateRequest{
		Subject:            pkix.Name{CommonName: commonName},
		SignatureAlgorithm: x509.ECDSAWithSHA256,
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, template, pair.private)
	if err != nil {
		return nil, fmt.Errorf("identity: creating CSR: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}), nil
}
