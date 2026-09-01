// SPDX-License-Identifier: Apache-2.0

package app

import (
	"bytes"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/parag8487/ForgeOps/agent/internal/config"
)

// `agent doctor` has to answer the question the Windows first run could not: WILL PAIRING WORK?
//
// It could not answer it before, in two ways. It never mentioned the credential store's capacity,
// so a user learned the OS Credential Manager would refuse the credential only after the exchange
// had burned a single-use pairing code. And it reached the store only through the session manager,
// which needs a backend URL — so with none configured it printed "credential store unusable: no
// backend URL configured", naming the wrong one of two independent facts.

// doctorApp builds an App with the given environment overlaid on a working baseline.
func doctorApp(t *testing.T, overrides map[string]string) *App {
	t.Helper()

	dir := t.TempDir()
	env := map[string]string{
		"AGENT_STATE_DIR":         dir,
		"AGENT_CREDENTIAL_STORE":  "file",
		"AGENT_IDENTITY_PROVIDER": "paired_device",
		"AGENT_WORKSPACE_ROOT":    filepath.Join(dir, "workspace"),
	}
	for k, v := range overrides {
		if v == "" {
			delete(env, k)
			continue
		}
		env[k] = v
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

// captureReport runs one of the doctor report functions and returns what it printed.
func captureReport(t *testing.T, app *App, report func(*cobra.Command, *App) []string) (string, []string) {
	t.Helper()

	var buf bytes.Buffer
	cmd := &cobra.Command{}
	cmd.SetOut(&buf)
	cmd.SetContext(t.Context())
	issues := report(cmd, app)
	return buf.String(), issues
}

func TestDoctor_ReportsTheCredentialStoreAndThatACredentialFits(t *testing.T) {
	app := doctorApp(t, nil)

	out, issues := captureReport(t, app, reportCredentialStore)

	// The backend must be NAMED. "ok" would leave a user who is about to hit the 2560-byte
	// Credential Manager ceiling with nothing to go on.
	if !strings.Contains(out, "Credential store:") {
		t.Errorf("doctor does not report the credential store:\n%s", out)
	}
	if !strings.Contains(out, "file(0600)") {
		t.Errorf("the report does not name the backend in use:\n%s", out)
	}
	// And it must say a credential FITS, which is the prediction the Windows defect needed.
	if !strings.Contains(out, "fits") {
		t.Errorf("the report does not say whether a credential fits:\n%s", out)
	}
	if len(issues) != 0 {
		t.Errorf("a working store reported issues: %v", issues)
	}
}

func TestDoctor_NamesThePathWhenCredentialsAreInAFile(t *testing.T) {
	app := doctorApp(t, nil)

	out, _ := captureReport(t, app, reportCredentialStore)

	// Where the credential actually lives is the first thing an operator asks when the file
	// backend is in use, and the state directory is not guessable from the platform alone.
	if !strings.Contains(out, "credentials.json") && !strings.Contains(out, app.cfg.Session.StateDir) {
		t.Errorf("the report does not say where the credential file is:\n%s", out)
	}
}

func TestDoctor_NoBackendURLIsNotACredentialStoreFailure(t *testing.T) {
	// THE MISDIAGNOSIS. With no AGENT_BACKEND_WSS_URL, `doctor` used to print "credential store
	// unusable: … no backend URL configured" — which sent me looking at AGENT_CREDENTIAL_STORE
	// and AGENT_STATE_DIR for a problem that was in neither.
	app := doctorApp(t, map[string]string{"AGENT_BACKEND_WSS_URL": ""})

	out, issues := captureReport(t, app, reportPairing)

	if strings.Contains(out, "credential store unusable") {
		t.Errorf("a missing backend URL is still reported as a credential store failure:\n%s", out)
	}
	// The store is fine and must be reported as fine.
	if !strings.Contains(out, "Credential store:") || !strings.Contains(out, "fits") {
		t.Errorf("the credential store was not reported at all:\n%s", out)
	}
	// And the real fact must be stated, with the remedy.
	if !strings.Contains(out, "no backend configured") {
		t.Errorf("the report does not say the backend is unconfigured:\n%s", out)
	}
	if !strings.Contains(out, "AGENT_BACKEND_WSS_URL") || !strings.Contains(out, "--backend") {
		t.Errorf("the report does not name where the backend URL comes from:\n%s", out)
	}
	// Not an issue: an agent used purely as a local CLI has no backend, and §10.10 exists so
	// `doctor` can tell that apart from a half-configured one.
	for _, issue := range issues {
		if strings.Contains(issue, "credential store") {
			t.Errorf("a missing backend URL was counted as a credential store issue: %q", issue)
		}
	}
}

func TestDoctor_ReportsUnpairedWhenABackendIsConfigured(t *testing.T) {
	app := doctorApp(t, map[string]string{
		"AGENT_BACKEND_WSS_URL": "wss://backend.invalid:8000/api/v1/ws/agent",
	})

	out, issues := captureReport(t, app, reportPairing)

	if !strings.Contains(out, "unpaired") {
		t.Errorf("a configured but unpaired agent is not reported as unpaired:\n%s", out)
	}
	// This one IS an issue, and its remediation is the command to run.
	found := false
	for _, issue := range issues {
		if strings.Contains(issue, "pair --code") {
			found = true
		}
	}
	if !found {
		t.Errorf("the remediation does not name the pair command: %v", issues)
	}
}

func TestDoctor_TheStoreIsReachableWithoutASession(t *testing.T) {
	// The structural fix behind the misdiagnosis: opening the store must not require a backend
	// URL. If these two are ever coupled again, the wrong-message defect comes straight back.
	app := doctorApp(t, map[string]string{"AGENT_BACKEND_WSS_URL": ""})

	store, err := app.CredentialStore()
	if err != nil {
		t.Fatalf("CredentialStore must not need a backend URL: %v", err)
	}
	if store.Backend() == "" {
		t.Error("the store reports no backend")
	}

	// And the session manager must still fail, or the test proves nothing about the decoupling.
	if _, err := app.Session(); err == nil {
		t.Error("Session() succeeded with no backend URL, so this test no longer covers the split")
	}
}

func TestDoctor_TheStoreIsMemoised(t *testing.T) {
	app := doctorApp(t, nil)

	// A keychain probe writes and deletes a marker. Doing it twice per command could reach two
	// different verdicts on a machine where the keychain is intermittently available, so `doctor`
	// would report a backend `pair` is not using.
	first, err := app.CredentialStore()
	if err != nil {
		t.Fatalf("CredentialStore: %v", err)
	}
	second, err := app.CredentialStore()
	if err != nil {
		t.Fatalf("CredentialStore: %v", err)
	}
	if first != second {
		t.Error("CredentialStore returned two different stores; doctor and pair could disagree")
	}
}
