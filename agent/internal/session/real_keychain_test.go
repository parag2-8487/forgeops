// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"errors"
	"os"
	"runtime"
	"strings"
	"testing"
)

// The REAL operating-system credential store, at the REAL credential size.
//
// WHY A SEPARATE FILE AND A SEPARATE GATE. Every other test in this package drives a fake keyring,
// which is right: a unit test that depended on the developer's Keychain would pass or fail for
// reasons unrelated to the code, and two parallel runs would fight over one entry.
//
// But that is exactly how the Windows defect survived. `zalando/go-keyring` refuses a value over
// 2560 bytes; the stored bundle was 28014; and no test ever asked a real credential store to hold a
// real credential. The fake had no limit, so it accepted what Windows would not. Pairing on Windows
// could never have worked, and the whole suite was green.
//
// So this file exists to be run against the actual store, on the actual platform, in a job that has
// one. `FORGEOPS_REAL_KEYCHAIN=1` opts in — NOT to skip work locally by default, but because a
// keychain write on a developer's machine may raise an interactive unlock prompt on macOS and hang a
// test run forever. CI sets it on `windows-latest` and `macos-latest`, and
// `.github/workflows/ci.yml` is where that is enforced.
//
// THIS IS NOT A SEAM IN PRODUCTION CODE. The env var is read here and nowhere else; the code under
// test has no idea whether it is running in CI.

const realKeychainEnv = "FORGEOPS_REAL_KEYCHAIN"

// requireRealKeychain skips unless the caller has asked for the real store.
//
// A skip rather than a failure, and the reason is in the message: a developer running `go test ./...`
// on a laptop must not have their login keychain written to, or be interrupted by an unlock prompt.
// CI is where this must run, and `TestCIRunsTheRealKeychainSuite` asserts that it does.
func requireRealKeychain(t *testing.T) {
	t.Helper()
	if os.Getenv(realKeychainEnv) != "1" {
		t.Skipf("set %s=1 to exercise this machine's real credential store; CI does this on "+
			"windows-latest and macos-latest", realKeychainEnv)
	}
}

// realStore opens a store backed by whatever credential manager this machine actually provides.
//
// `auto` RATHER THAN `keychain`, and the difference matters. An explicit `keychain` preference fails
// when no OS keychain is usable, which is the normal state of a headless Linux runner: there is no
// Secret Service, and standing one up in CI proved unreliable — `gnome-keyring-daemon --unlock`
// starts and answers on D-Bus, but the default collection does not exist until something creates it,
// so go-keyring reports `failed to unlock correct collection`. Creating it non-interactively is not
// something to depend on for every run.
//
// `auto` is also what the agent itself uses by default, so this exercises the store a real deployment
// on this platform would get: the Credential Manager on Windows, the Keychain on macOS, and the 0600
// file on a headless Linux host — which is the backend every containerised agent already uses, and
// the one OQ-26 exists to accept.
//
// WHAT IS THEREFORE NOT COVERED, said plainly: libsecret. No job and no local run exercises it, so
// its row in `platformKeychainLimits` remains sourced from documentation. That is recorded in the
// table itself rather than left to be discovered.
func realStore(t *testing.T) *FileStore {
	t.Helper()
	store, err := NewStore(t.TempDir(), "auto")
	if err != nil {
		t.Fatalf("no usable credential store at all on %s: %v", runtime.GOOS, err)
	}
	// Named in the output either way, so a run that silently moved to the file backend on a platform
	// that is supposed to have a keychain is visible rather than mistaken for keychain coverage.
	t.Logf("%s: exercising the %q backend", runtime.GOOS, store.Backend())

	// On the two platforms that ship a credential manager, the file backend would mean the keychain
	// probe failed — and this suite exists to test the keychain there, so that is a failure and not a
	// downgrade to accept.
	if runtime.GOOS == "windows" || runtime.GOOS == "darwin" {
		if store.Backend() != BackendKeychain {
			t.Fatalf("%s has an OS credential manager but the store selected %q; this suite would "+
				"prove nothing about the store a real user gets", runtime.GOOS, store.Backend())
		}
	}
	return store
}

func TestRealKeychain_HoldsAFullSizeCredential(t *testing.T) {
	requireRealKeychain(t)

	// THE TEST THAT WOULD HAVE CAUGHT THE DEFECT. `realisticCredentials()` is the size the backend
	// actually returns, policy bundle included. Before the split, this Save failed on Windows with
	// "data passed to Set was too big" — and in the real product that happened AFTER the exchange had
	// burned a single-use pairing code.
	store := realStore(t)
	ctx := context.Background()
	t.Cleanup(func() { _ = store.Wipe(ctx) })

	want := realisticCredentials()
	if err := store.Save(ctx, want); err != nil {
		t.Fatalf("the real %s credential store on %s refused a full-size credential: %v",
			store.Backend(), runtime.GOOS, err)
	}

	got, err := store.Load(ctx)
	if err != nil {
		t.Fatalf("Load from the real store: %v", err)
	}

	// Read back byte for byte. A store that silently truncated would be worse than one that refused:
	// a truncated private key fails at the first handshake with an error that points at the backend.
	for _, f := range []struct {
		name      string
		got, want []byte
	}{
		{"DeviceToken", got.DeviceToken, want.DeviceToken},
		{"EnvelopeKey", got.EnvelopeKey, want.EnvelopeKey},
		{"ClientKey", got.ClientKey, want.ClientKey},
		{"ClientCert", got.ClientCert, want.ClientCert},
		{"CABundle", got.CABundle, want.CABundle},
		{"PolicyBundle", got.PolicyBundle, want.PolicyBundle},
	} {
		if string(f.got) != string(f.want) {
			t.Errorf("%s came back as %d bytes, sent %d: the real store did not round-trip it",
				f.name, len(f.got), len(f.want))
		}
	}
	if got.DeviceID != want.DeviceID {
		t.Errorf("DeviceID = %q, want %q", got.DeviceID, want.DeviceID)
	}
}

func TestRealKeychain_CheckCapacityAgreesWithSave(t *testing.T) {
	requireRealKeychain(t)

	// `doctor` tells the user whether pairing will work, by running exactly this check. If it could
	// disagree with `Save`, it would either predict a failure that would not happen — sending a user
	// to change `AGENT_CREDENTIAL_STORE` for no reason — or promise success and then burn a code.
	store := realStore(t)
	ctx := context.Background()
	t.Cleanup(func() { _ = store.Wipe(ctx) })

	capacityErr := store.CheckCapacity(ctx, CapacityProbeForDoctor())
	saveErr := store.Save(ctx, realisticCredentials())

	switch {
	case capacityErr == nil && saveErr != nil:
		t.Fatalf("CheckCapacity said a credential fits and Save then failed: %v", saveErr)
	case capacityErr != nil && saveErr == nil:
		t.Fatalf("CheckCapacity refused a credential Save accepted: %v", capacityErr)
	}
}

func TestRealKeychain_ProbeLeavesNothingBehind(t *testing.T) {
	requireRealKeychain(t)

	// The probe writes to the user's actual credential manager. Leaving an entry there would be a
	// real mess on a real machine, and would make an unpaired agent look paired.
	store := realStore(t)
	ctx := context.Background()

	if err := store.CheckCapacity(ctx, CapacityProbeForDoctor()); err != nil {
		t.Fatalf("CheckCapacity: %v", err)
	}
	if _, err := store.Load(ctx); !errors.Is(err, ErrNoCredentials) {
		t.Errorf("after a capacity probe against the real store the agent must still be unpaired, "+
			"got %v", err)
	}
}

func TestRealKeychain_WipeRemovesWhatSaveWrote(t *testing.T) {
	requireRealKeychain(t)

	// `pair --wipe` has to actually clear the real store, or a user recovering from a failed pairing
	// is stuck with a credential they cannot replace and cannot see.
	store := realStore(t)
	ctx := context.Background()

	if err := store.Save(ctx, realisticCredentials()); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if err := store.Wipe(ctx); err != nil {
		t.Fatalf("Wipe: %v", err)
	}
	if _, err := store.Load(ctx); !errors.Is(err, ErrNoCredentials) {
		t.Errorf("Wipe left something in the real store: %v", err)
	}

	// Idempotent against the real store too: revocation calls Wipe and must reach the unpaired state
	// whatever it finds.
	if err := store.Wipe(ctx); err != nil {
		t.Errorf("a second Wipe against the real store failed: %v", err)
	}
}

func TestRealKeychain_ReportsItsOwnLimit(t *testing.T) {
	requireRealKeychain(t)

	// Records what THIS machine's store actually accepts, and checks it against the table in
	// `store_capacity_test.go`. The table is the basis for `secretHalfBudget`, so if a platform's real
	// limit is smaller than the table claims, the budget is wrong and the split may not be enough.
	store := realStore(t)
	ctx := context.Background()
	t.Cleanup(func() { _ = store.Wipe(ctx) })

	limit, known := platformKeychainLimits[runtime.GOOS]
	if !known {
		t.Fatalf("no recorded limit for %s; add one with its source before running here", runtime.GOOS)
	}

	// The budget must fit. Proven by writing it, not by arithmetic.
	if err := store.CheckCapacity(ctx, CapacityProbeForDoctor()); err != nil {
		t.Fatalf("this machine's %s store refused a credential inside the %d-byte budget, so the "+
			"recorded limit for %s (%d bytes: %s) is wrong: %v",
			store.Backend(), secretHalfBudget, runtime.GOOS, limit.bytes, limit.source, err)
	}

	// The backend is named, because "linux accepted it" means the FILE backend on a headless runner
	// and the Secret Service on a desktop, and those are different claims.
	t.Logf("%s: real store %q accepted a %d-byte-budget credential; recorded limit for this platform "+
		"is %d (%s)", runtime.GOOS, store.Backend(), secretHalfBudget, limit.bytes, limit.source)
}

func TestRealKeychain_TheOptInIsNamedInCI(t *testing.T) {
	// NOT gated on the env var: this one must run everywhere, because it is the test that stops the
	// file above from being dead code. A suite that only runs when a variable is set, and that
	// nothing sets, is worse than no suite — it reads as coverage and provides none. That is the exact
	// shape of the Q-19 defect, where a runner pinned the wrong Python and four property tests raised
	// on every run while the harness read the crash as a kill.
	workflow, err := os.ReadFile("../../../.github/workflows/ci.yml")
	if err != nil {
		t.Fatalf("reading the workflow: %v", err)
	}
	text := string(workflow)

	if !strings.Contains(text, realKeychainEnv+": \"1\"") && !strings.Contains(text, realKeychainEnv+": '1'") {
		t.Errorf("no CI job sets %s=1, so the real-credential-store suite never runs anywhere. "+
			"It is the only suite that can catch a store refusing a full-size credential, which is "+
			"the defect that made pairing on Windows impossible", realKeychainEnv)
	}
	for _, runner := range []string{"windows-latest", "macos-latest"} {
		if !strings.Contains(text, runner) {
			t.Errorf("no job runs on %s, so that platform's credential store is never exercised", runner)
		}
	}
}
