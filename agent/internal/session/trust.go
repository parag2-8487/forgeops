// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"net"
	"net/url"
	"strings"
	"time"
)

// TrustSource says where the material verifying the backend's listener came from.
type TrustSource string

const (
	// TrustFromPairing is the normal case: the CA bundle the backend handed over in the same
	// single-use exchange that issued this device's certificate. The narrowest trust available —
	// it verifies exactly one issuer, and nothing about the machine's own trust store matters.
	TrustFromPairing TrustSource = "the CA bundle stored at pairing"

	// TrustNone means no CA bundle is stored, so the listener cannot be verified at all. It is a
	// refusal, never a fallback to the system trust store: the internal CA is not in any public
	// root store, so "fall back" would mean "fail with a confusing error" at best and, if the
	// system store did happen to contain something that chained, would mean trusting an issuer
	// nobody chose.
	TrustNone TrustSource = "nothing — no CA bundle is stored"
)

// : How long the trust probe waits. Short: `doctor` is interactive and a hanging diagnostic is
// : worse than an inconclusive one, and the listener is normally on the same machine.
const trustProbeTimeout = 5 * time.Second

// TrustReport is what `doctor` prints about the agent's ability to reach its session endpoint.
type TrustReport struct {
	// Endpoint is the address the session will dial, after `SessionURL` has chosen between the
	// stored and the configured value.
	Endpoint string

	// EndpointFromPairing is true when the backend stated the endpoint, false when it fell back
	// to configuration. Worth printing: the two mean different things when something is wrong.
	EndpointFromPairing bool

	// Source says what verifies the listener.
	Source TrustSource

	// Issuers names the subjects in the stored bundle, so a user can see WHICH CA is trusted
	// rather than only that one is.
	Issuers []string

	// NotAfter is the earliest expiry among the stored CA certificates. A CA that has expired
	// verifies nothing, and the failure it produces names the handshake rather than the date.
	NotAfter time.Time

	// Err is the result of actually attempting the handshake. Nil means the listener was reached
	// and verified against the stored bundle.
	Err error
}

// DescribeTrust reports how this agent verifies its backend's session listener, and PROVES the
// answer by completing a TLS handshake rather than by inspecting configuration.
//
// WHY A REAL HANDSHAKE. Every part of this can be individually present and collectively wrong: a
// CA bundle that verifies a listener on a different port, a listener that is not running, a
// certificate whose SAN list omits the name the agent dials. Each produces the same shrug from a
// configuration check and a different error from a handshake. The credential store's capacity check
// established the principle — do the real operation against the real machine — and this is the same
// argument for the same reason.
//
// It deliberately does NOT fall back to the system trust store when no bundle is stored, and offers
// no flag to skip verification. The client certificate IS the authentication for this endpoint; an
// unverified channel to it is not a degraded version of the feature, it is a different feature
// nobody asked for.
func DescribeTrust(ctx context.Context, creds Credentials, configuredURL string) TrustReport {
	report := TrustReport{Source: TrustNone}

	endpoint, err := SessionURL(creds.SessionWSURL, configuredURL)
	if err != nil {
		report.Err = err
		return report
	}
	report.Endpoint = endpoint
	report.EndpointFromPairing = strings.TrimSpace(creds.SessionWSURL) != ""

	roots := x509.NewCertPool()
	if len(creds.CABundle) == 0 {
		report.Err = errors.New(
			"no CA bundle is stored, so the backend's listener cannot be verified. Pair this " +
				"agent — the exchange supplies the CA alongside the client certificate")
		return report
	}
	if !roots.AppendCertsFromPEM(creds.CABundle) {
		report.Err = errors.New("the stored CA bundle contains no usable certificate; re-pair this agent")
		return report
	}
	report.Source = TrustFromPairing
	report.Issuers, report.NotAfter = describeBundle(creds.CABundle)

	// The client certificate too, because this endpoint requires one: verifying the server while
	// presenting nothing would report success for a connection the backend will refuse.
	var certs []tls.Certificate
	if len(creds.ClientCert) > 0 && len(creds.ClientKey) > 0 {
		pair, pairErr := tls.X509KeyPair(creds.ClientCert, creds.ClientKey)
		if pairErr != nil {
			report.Err = fmt.Errorf("the stored certificate and key do not form a pair: %w", pairErr)
			return report
		}
		certs = append(certs, pair)
	}

	report.Err = probeTLS(ctx, endpoint, &tls.Config{
		Certificates: certs,
		RootCAs:      roots,
		MinVersion:   tls.VersionTLS13,
	})
	return report
}

// describeBundle names the subjects in a PEM bundle and reports the earliest expiry.
func describeBundle(pemBytes []byte) (subjects []string, earliest time.Time) {
	rest := pemBytes
	for {
		var block *pem.Block
		block, rest = pem.Decode(rest)
		if block == nil {
			return subjects, earliest
		}
		if block.Type != "CERTIFICATE" {
			continue
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			continue
		}
		subjects = append(subjects, cert.Subject.CommonName)
		if earliest.IsZero() || cert.NotAfter.Before(earliest) {
			earliest = cert.NotAfter
		}
	}
}

// probeTLS completes a handshake and closes it. No request is sent: the question is whether this
// agent and this listener can authenticate each other, and the handshake is the whole of that.
func probeTLS(ctx context.Context, endpoint string, cfg *tls.Config) error {
	parsed, err := url.Parse(endpoint)
	if err != nil {
		return fmt.Errorf("the session endpoint is not a URL: %w", err)
	}
	switch parsed.Scheme {
	case "wss", "https":
	case "ws", "http":
		// Reported rather than probed. A plaintext endpoint cannot carry a client certificate, so
		// the backend will refuse the session with "client certificate and bearer device token are
		// both required". Saying so here is the diagnosis; attempting a TLS handshake against a
		// plaintext port would produce a protocol error that names none of it.
		return fmt.Errorf(
			"the session endpoint is %s, which cannot carry a client certificate — the backend "+
				"will refuse the session. Re-pair against a backend that states its mTLS "+
				"listener, or set AGENT_BACKEND_WSS_URL to it", parsed.Scheme)
	default:
		return fmt.Errorf("the session endpoint has scheme %q, which the agent does not dial", parsed.Scheme)
	}

	host := parsed.Host
	if parsed.Port() == "" {
		host = net.JoinHostPort(parsed.Hostname(), "443")
	}

	ctx, cancel := context.WithTimeout(ctx, trustProbeTimeout)
	defer cancel()

	dialer := &tls.Dialer{Config: cfg}
	conn, err := dialer.DialContext(ctx, "tcp", host)
	if err != nil {
		return err
	}
	return conn.Close()
}
