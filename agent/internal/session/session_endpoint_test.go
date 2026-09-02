// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/connection"
)

// The defect these tests pin: a host agent paired successfully and then never received an apply
// command, because ONE configured URL drove two listeners with different authentication
// requirements. Pointed at the pairing port the session was refused for having no client
// certificate; pointed at the mTLS port the pairing exchange failed the TLS handshake. The backend
// now states the session address in the pairing response and the agent dials that.

func TestSessionURL_PrefersTheEndpointTheBackendStatedOverAPlaintextConfiguredOne(t *testing.T) {
	t.Parallel()

	// THE HOST CASE, and the defect. The user supplied the ordinary port because that is where
	// pairing happens; the backend answered with its mTLS listener. The stored value must win, or
	// the agent dials a listener that cannot authenticate it and the session is refused.
	got, err := SessionURL(
		"wss://localhost:18443/api/v1/ws/agent",
		"ws://localhost:18000/api/v1/ws/agent",
	)
	if err != nil {
		t.Fatalf("resolving the session URL: %v", err)
	}
	if want := "wss://localhost:18443/api/v1/ws/agent"; got != want {
		t.Fatalf("session URL = %q, want the stored endpoint %q", got, want)
	}
}

func TestSessionURL_AnExplicitlyConfiguredTLSEndpointWins(t *testing.T) {
	t.Parallel()

	// THE CONTAINER CASE, and a regression this nearly caused. A containerised agent is configured
	// with `wss://backend-agent:8443/...`, which is reachable only over the Compose network, while
	// the backend advertises the address a HOST agent needs. One deployment has two audiences and no
	// single advertised address serves both, so an explicit `wss` value — which only a TLS endpoint
	// can be, and therefore only a session endpoint — must not be silently overridden.
	got, err := SessionURL(
		"wss://localhost:18443/api/v1/ws/agent",
		"wss://backend-agent:8443/api/v1/ws/agent",
	)
	if err != nil {
		t.Fatalf("resolving the session URL: %v", err)
	}
	if want := "wss://backend-agent:8443/api/v1/ws/agent"; got != want {
		t.Fatalf("session URL = %q, want the explicitly configured endpoint %q", got, want)
	}
}

func TestSessionURL_ASchemeThatMerelyStartsWithWssIsNotATLSEndpoint(t *testing.T) {
	t.Parallel()

	// The precedence turns on the scheme, so the scheme is PARSED rather than prefix-matched.
	// A prefix match would treat this as an explicit session endpoint and ignore the real one.
	got, err := SessionURL(
		"wss://localhost:18443/api/v1/ws/agent",
		"wsseverywhere://localhost:9/api/v1/ws/agent",
	)
	if err != nil {
		t.Fatalf("resolving the session URL: %v", err)
	}
	if want := "wss://localhost:18443/api/v1/ws/agent"; got != want {
		t.Fatalf("session URL = %q, want the stored endpoint %q", got, want)
	}
}

func TestSessionURL_FallsBackToTheConfiguredURLWhenTheBackendStatedNone(t *testing.T) {
	t.Parallel()

	// A backend older than the field sends nothing. The agent must keep working against it rather
	// than refuse, so the configured value is used.
	got, err := SessionURL("", "wss://backend-agent:8443/api/v1/ws/agent")
	if err != nil {
		t.Fatalf("resolving the session URL: %v", err)
	}
	if want := "wss://backend-agent:8443/api/v1/ws/agent"; got != want {
		t.Fatalf("session URL = %q, want the configured URL %q", got, want)
	}
}

func TestSessionURL_NeverInventsAnAddress(t *testing.T) {
	t.Parallel()

	// THE PROPERTY THAT MATTERS. With neither source the answer is a refusal, not a guess. An
	// agent that derived "the configured host with 8443 substituted" would be inventing
	// infrastructure, and a wrong guess sends a device token to whatever answers. Every address
	// this function can return was named by a human or by the backend that issued the certificate.
	_, err := SessionURL("   ", "\t\n")
	if err == nil {
		t.Fatal("with no stored and no configured endpoint the resolver must refuse, not guess")
	}
	if !errors.Is(err, connection.ErrDisabled) {
		t.Fatalf("error must wrap connection.ErrDisabled so callers can classify it; got %v", err)
	}
	// The remedy has to be actionable: it must name the setting and the alternative.
	for _, want := range []string{"AGENT_BACKEND_WSS_URL", "re-pair"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("refusal must mention %q so a user knows what to do; got %q", want, err.Error())
		}
	}
}

func TestSessionURL_TreatsWhitespaceAsAbsentOnBothSides(t *testing.T) {
	t.Parallel()

	// A stored value of spaces would otherwise be dialled and fail with a URL parse error naming
	// nothing useful. Absent and blank must mean the same thing.
	got, err := SessionURL("  ", "ws://localhost:18000/api/v1/ws/agent")
	if err != nil {
		t.Fatalf("resolving the session URL: %v", err)
	}
	if want := "ws://localhost:18000/api/v1/ws/agent"; got != want {
		t.Fatalf("a blank stored endpoint must not win; got %q want %q", got, want)
	}
}

func TestTheSessionEndpointSurvivesAStoreRoundTrip(t *testing.T) {
	t.Parallel()

	// It travels in the NON-SECRET half, so this also asserts it did not consume the keychain's
	// 2560-byte budget. Checked through the public Store interface, because that is what `Serve`
	// reads and a field wired into `split` but not `join` would be silently lost on reload.
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("opening a file-backed store: %v", err)
	}
	ctx := context.Background()

	const endpoint = "wss://localhost:18443/api/v1/ws/agent"
	saved := Credentials{
		DeviceID:     "01J0000000000000000000000A",
		DeviceToken:  sizedBlob("token", credentialByteLength),
		EnvelopeKey:  sizedBlob("envelope", credentialByteLength),
		ClientKey:    sizedBlob("client-key", 227),
		ClientCert:   sizedBlob("client-cert", 900),
		CABundle:     sizedBlob("ca-bundle", 656),
		SessionWSURL: endpoint,
	}
	if err := store.Save(ctx, saved); err != nil {
		t.Fatalf("saving: %v", err)
	}

	loaded, err := store.Load(ctx)
	if err != nil {
		t.Fatalf("loading: %v", err)
	}
	if loaded.SessionWSURL != endpoint {
		t.Fatalf("session endpoint after a round trip = %q, want %q", loaded.SessionWSURL, endpoint)
	}
}

func TestAnAgentPairedBeforeThisFieldExistedStillLoads(t *testing.T) {
	t.Parallel()

	// An existing installation's credential has no `session_ws_url`. It must load, report the field
	// empty, and let `SessionURL` fall back — not fail to unmarshal and look like a corrupt store,
	// which would send a working user to `pair --wipe`.
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("opening a file-backed store: %v", err)
	}
	ctx := context.Background()

	if err := store.Save(ctx, Credentials{
		DeviceID:    "01J0000000000000000000000B",
		DeviceToken: sizedBlob("token", credentialByteLength),
		EnvelopeKey: sizedBlob("envelope", credentialByteLength),
		ClientKey:   sizedBlob("client-key", 227),
		ClientCert:  sizedBlob("client-cert", 900),
		CABundle:    sizedBlob("ca-bundle", 656),
	}); err != nil {
		t.Fatalf("saving a credential with no session endpoint: %v", err)
	}

	loaded, err := store.Load(ctx)
	if err != nil {
		t.Fatalf("loading: %v", err)
	}
	if loaded.SessionWSURL != "" {
		t.Fatalf("session endpoint = %q, want empty for a credential that carries none", loaded.SessionWSURL)
	}
	got, err := SessionURL(loaded.SessionWSURL, "wss://backend-agent:8443/api/v1/ws/agent")
	if err != nil {
		t.Fatalf("an older credential must still resolve via the configured URL: %v", err)
	}
	if want := "wss://backend-agent:8443/api/v1/ws/agent"; got != want {
		t.Fatalf("resolved %q, want %q", got, want)
	}
}
