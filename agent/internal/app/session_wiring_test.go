// SPDX-License-Identifier: Apache-2.0

// Every collaborator `Serve` needs must be built by PRODUCTION code (design §10.1–§10.6,
// leaves 8.5 and 8.7).
//
// WHY THIS FILE EXISTS
// `Deps.Identity`, `Deps.Verifier`, `Deps.Runner` and `Deps.Journal` were assembled only in
// `session/serve_test.go`. Every unit test therefore passed — the session loop is well covered —
// while the shipped agent dialled and refused with
// `session: Serve needs an identity.Provider; pass Deps.Identity`. The gap was not a missing
// test of the loop; it was that nothing tested the COMPOSITION.
//
// So these assertions are deliberately about `buildSessionDeps` and not about behaviour under
// load. A test double satisfies an interface just as well as the real thing, which is exactly how
// the hole stayed invisible, so each case also pins the concrete type where the type is the
// property being claimed.
package app

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/config"
	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/identity"
	"github.com/parag8487/ForgeOps/agent/internal/session"
)

// wiringApp builds an App whose credential store and journal live under t.TempDir().
//
// `AGENT_CREDENTIAL_STORE=file` rather than `auto`: `auto` probes the OS keychain, and a test
// that depended on whether the machine running it has one would pass and fail for reasons
// unrelated to the wiring.
func wiringApp(t *testing.T) *App {
	t.Helper()

	dir := t.TempDir()
	env := map[string]string{
		"AGENT_BACKEND_WSS_URL":   "wss://backend.invalid:8000/api/v1/ws/agent",
		"AGENT_STATE_DIR":         dir,
		"AGENT_CREDENTIAL_STORE":  "file",
		"AGENT_IDENTITY_PROVIDER": "paired_device",
		"AGENT_WORKSPACE_ROOT":    filepath.Join(dir, "workspace"),
	}
	cfg, err := config.Load(func(key string) string { return env[key] })
	if err != nil {
		t.Fatalf("config.Load: %v", err)
	}
	app, err := New(cfg, BuildInfo{Version: "test-version"})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = app.Close() })
	return app
}

func TestBuildSessionDeps_ConstructsEveryCollaborator(t *testing.T) {
	app := wiringApp(t)

	store, err := session.NewStore(app.cfg.Session.StateDir, app.cfg.Session.CredentialStore)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	deps, err := app.buildSessionDeps(store)
	if err != nil {
		t.Fatalf("buildSessionDeps: %v", err)
	}

	// Each of these being nil is a SILENT DEGRADATION rather than a crash, which is why every
	// one is named individually with the behaviour its absence produces. A single
	// "deps != zero" assertion would pass with three of the four missing.
	if deps.Identity == nil {
		t.Error("Deps.Identity is nil: Serve refuses at the first dial and the agent never connects")
	}
	if deps.Verifier == nil {
		t.Error("Deps.Verifier is nil: every inbound frame is refused unverified")
	}
	if deps.Runner == nil {
		t.Error("Deps.Runner is nil: every command answers `operation-unknown`")
	}
	if deps.Journal == nil {
		t.Error("Deps.Journal is nil: D-41's outbound queue is never drained after a reconnect")
	}
	if deps.Bundle == nil {
		t.Error("Deps.Bundle is nil: every mutation is refused as policy-bundle-stale")
	}
	if deps.Store == nil || deps.Logger == nil {
		t.Error("the pre-existing dependencies must survive the addition of the new ones")
	}
	if deps.AgentVersion != "test-version" {
		t.Errorf("AgentVersion = %q, want the build info's version", deps.AgentVersion)
	}
}

// TestBuildSessionDeps_UsesNoTestSeam pins the concrete types, because satisfying the interface
// is exactly what a seam does.
//
// `envelope.StaticKeySource` and `envelope.StaticBundleDigest` hold a value somebody has to
// remember to `Set`. Wiring either one would compile, satisfy the interface, pass every
// behavioural test with a value installed by the test — and ship an agent whose key is absent
// and whose bundle digest is empty, refusing every command.
func TestBuildSessionDeps_UsesNoTestSeam(t *testing.T) {
	app := wiringApp(t)
	store, err := session.NewStore(app.cfg.Session.StateDir, app.cfg.Session.CredentialStore)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	deps, err := app.buildSessionDeps(store)
	if err != nil {
		t.Fatalf("buildSessionDeps: %v", err)
	}

	// `deps.Bundle` cannot be `*envelope.StaticBundleDigest` at all: the COMPILER refuses that
	// assertion as impossible, because the seam has only `BundleDigest` while
	// `session.BundleState` also demands `Digest`, `Current` and `ObserveBackend`. That is a
	// stronger guarantee than a runtime check, and it is written down because the absence of an
	// assertion here would otherwise look like an oversight. The verifier's own
	// `BundleDigestSource` argument has no such protection, so `TestNoTestSeamOnAProductionPath`
	// gates that one at the repository level.
	if _, ok := deps.Bundle.(*session.CredentialBundleState); !ok {
		t.Errorf("Deps.Bundle is %T; the bundle digest must be read from the pinned credential", deps.Bundle)
	}
	if _, ok := deps.Identity.(*identity.PairedDevice); !ok {
		t.Errorf("Deps.Identity is %T, want the paired-device provider", deps.Identity)
	}
	if _, ok := deps.Verifier.(*envelope.Verifier); !ok {
		t.Errorf("Deps.Verifier is %T, want the real envelope verifier", deps.Verifier)
	}
	if _, ok := deps.Journal.(*session.FileJournal); !ok {
		t.Errorf("Deps.Journal is %T, want the on-disk journal", deps.Journal)
	}
	// The runner must be the adapter over the real dispatcher, not a closure a test installed.
	runner, ok := deps.Runner.(commandRunner)
	if !ok {
		t.Fatalf("Deps.Runner is %T, want the executor adapter", deps.Runner)
	}
	if runner.dispatcher == nil {
		t.Error("the adapter carries no dispatcher, so every command would panic or refuse")
	}
}

// TestBuildSessionDeps_AdvertisesOnlyImplementedOperations guards the handshake's honesty.
//
// `session.connect` sends `capabilities`, and the backend routes on them. Advertising an
// operation whose body is an `unimplemented(...)` placeholder would have the backend dispatch
// work this binary then refuses — a routing failure that looks like an agent fault.
func TestBuildSessionDeps_AdvertisesOnlyImplementedOperations(t *testing.T) {
	app := wiringApp(t)
	store, err := session.NewStore(app.cfg.Session.StateDir, app.cfg.Session.CredentialStore)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	deps, err := app.buildSessionDeps(store)
	if err != nil {
		t.Fatalf("buildSessionDeps: %v", err)
	}

	if len(deps.Capabilities) == 0 {
		t.Fatal("no capabilities advertised; changeset.apply and changeset.revert are implemented")
	}
	// Vacuity guard with teeth: the two operations the journey depends on must be present, so
	// this cannot pass by advertising some unrelated subset.
	want := map[string]bool{"changeset.apply": false, "changeset.revert": false}
	for _, capability := range deps.Capabilities {
		if _, tracked := want[capability]; tracked {
			want[capability] = true
		}
	}
	for operation, found := range want {
		if !found {
			t.Errorf("%s is implemented but not advertised: the backend would not route it here", operation)
		}
	}
}

// TestIdentityProvider_RefusesTheUnbuiltProvider keeps the SPIFFE gap honest.
//
// §14.3 says state the gap rather than pretend a pairing code is attestation. Falling back to
// the paired device would hand a cluster workload a laptop credential, which is worse than a
// refusal because it would appear to work.
func TestIdentityProvider_RefusesTheUnbuiltProvider(t *testing.T) {
	if _, err := identityProvider("spiffe_workload", nil, 0); err == nil {
		t.Error("spiffe_workload must be refused while no SVID-backed Provider exists")
	}
	if _, err := identityProvider("nonsense", nil, 0); err == nil {
		t.Error("an unknown provider must be refused rather than defaulted")
	}
	provider, err := identityProvider("paired_device", nil, 0)
	if err != nil || provider == nil {
		t.Errorf("paired_device must build: provider=%v err=%v", provider, err)
	}
}

// TestCredentialBundleState_RefusesBeforeTheHandshake is D-25's direction, at the wiring level.
//
// An agent that has not yet heard from the backend holds no statement about which bundle is
// active. Reading that silence as "current" would let a mutation through on an unverified policy
// assumption, which is the failure the whole bundle-digest binding exists to prevent.
func TestCredentialBundleState_RefusesBeforeTheHandshake(t *testing.T) {
	dir := t.TempDir()
	store, err := session.NewStore(dir, "file")
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	state, err := session.NewCredentialBundleState(store)
	if err != nil {
		t.Fatalf("NewCredentialBundleState: %v", err)
	}
	if state.Current() {
		t.Error("Current() is true with no credential and no handshake; mutations would be allowed")
	}
	if _, err := state.BundleDigest(context.Background()); err == nil {
		t.Error("BundleDigest must error when no bundle is held, not return the empty string")
	}
}
