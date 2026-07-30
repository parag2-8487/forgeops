// SPDX-License-Identifier: Apache-2.0

// Package identity supplies the agent's cryptographic identity for mTLS
// (design §10.2, §14.3, D-36).
//
// Two providers, and the reason is stated rather than implied
// ----------------------------------------------------------
// Research §H31's model is cluster-shaped: attest the workload from its namespace,
// service account and image digest, and issue a short-lived SVID. Phase 1's primary
// reality is a developer laptop, where none of those exist. §14.3 states that gap
// honestly instead of pretending a pairing code is attestation, and D-36 records the
// consequence: `PairedDevice` for the laptop, `SpiffeWorkload` for the cluster, one
// interface so the session manager does not care which it has.
//
// What both providers guarantee
// -----------------------------
// No long-lived agent key. The paired-device certificate lives at most 24 hours and
// renews over the live session; an SVID lives as long as SPIRE says and no longer. That
// is the invariant `ClientTLS` documents and `assertShortLived` enforces at runtime —
// a provider that returned a non-expiring credential would be a design violation, so it
// is refused rather than trusted not to happen.
package identity

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"time"
)

// MaxCertificateLifetime bounds any certificate a provider may present.
//
// 168 hours, not 24. `DEVICE_CERT_TTL_HOURS` is capped at 168 by configuration
// (§13.1), and this check is the backstop for "somebody issued a ten-year cert",
// not a re-implementation of the TTL policy. Making it exactly 24 here would reject
// a legitimately configured 48-hour cert and push operators towards disabling the
// check, which is worse than a looser bound that always holds.
const MaxCertificateLifetime = 168 * time.Hour

var (
	// ErrNoCredential means the provider has nothing to present yet — for
	// PairedDevice, that the agent has not been paired.
	ErrNoCredential = errors.New("identity: no credential available")

	// ErrNonExpiringCredential means a provider produced a certificate with no
	// meaningful expiry. It is a bug in the provider, not a condition to recover
	// from, and it is refused loudly so it cannot become the deployed state.
	ErrNonExpiringCredential = errors.New("identity: refusing a credential that does not expire")
)

// Provider is the agent's identity, whatever its shape.
type Provider interface {
	// ClientTLS returns a config for dialling the backend. The returned config's
	// certificate MUST be short-lived; implementations that would return a
	// non-expiring credential are a design violation, not an option.
	ClientTLS(ctx context.Context) (*tls.Config, error)

	// Identity describes who we are, for logging and for agent.status.
	Identity(ctx context.Context) (Info, error)

	// RenewBefore reports how long before expiry a renewal should start, so the
	// session manager can renew without dropping the connection.
	RenewBefore() time.Duration
}

// Info describes the current identity.
//
// Carries no key material and no token: it is written to logs and returned by
// `agent.status`, so anything in it is effectively public. `Fingerprint` is a hash, and
// `Subject` is an identifier the backend already knows.
type Info struct {
	Kind        string // "paired_device" | "spiffe_workload"
	Subject     string // device id, or the SPIFFE ID
	Fingerprint string // sha256 of the leaf certificate
	NotAfter    time.Time
}

// Expired reports whether the identity is past its validity.
func (i Info) Expired(now time.Time) bool {
	return !i.NotAfter.IsZero() && now.After(i.NotAfter)
}

// ExpiresWithin reports whether the identity expires inside d of now.
func (i Info) ExpiresWithin(now time.Time, d time.Duration) bool {
	if i.NotAfter.IsZero() {
		return false
	}
	return now.Add(d).After(i.NotAfter)
}

// assertShortLived refuses a certificate that does not expire, or expires absurdly far
// away.
//
// The zero time is the dangerous case: a certificate parsed from a malformed source, or
// one built by a provider that forgot to set NotAfter, yields a zero value that every
// "is it expired?" comparison reads as "no, and never will be". Treating that as valid
// would silently grant the agent a permanent credential, which is exactly what D-36 says
// must be impossible.
func assertShortLived(notAfter, now time.Time) error {
	if notAfter.IsZero() {
		return fmt.Errorf("%w: NotAfter is unset", ErrNonExpiringCredential)
	}
	if lifetime := notAfter.Sub(now); lifetime > MaxCertificateLifetime {
		return fmt.Errorf(
			"%w: NotAfter is %s away, which exceeds the %s bound",
			ErrNonExpiringCredential, lifetime.Round(time.Hour), MaxCertificateLifetime,
		)
	}
	return nil
}
