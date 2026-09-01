// SPDX-License-Identifier: Apache-2.0

package app

import (
	"bytes"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/pflag"

	"github.com/parag8487/ForgeOps/agent/internal/config"
)

// `forgeops-agent connect` — the whole first run in one command.
//
// WHAT IT REPLACES. Connecting took: open a terminal, cd into the source tree, know Go is installed,
// `go build -o forgeops-agent.exe ./cmd/agent`, know to prefix `.\`, know the backend URL, set an
// environment variable, `pair`, `scan`, `run` — and beat a five-minute clock throughout.
//
// WHAT MUST REMAIN TRUE. It gains no authority the individual verbs do not have, it refuses rather
// than guessing at every step exactly as they do, and a failure names the stage that produced it.

func connectApp(t *testing.T, overrides map[string]string) *App {
	t.Helper()
	t.Chdir(t.TempDir())

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

// runConnect invokes the command and returns its output and error.
func runConnect(t *testing.T, app *App, args ...string) (string, error) {
	t.Helper()
	cmd := newConnectCmd(app)
	var buf bytes.Buffer
	cmd.SetOut(&buf)
	cmd.SetErr(&buf)
	cmd.SetContext(t.Context())
	cmd.SetArgs(args)
	err := cmd.Execute()
	return buf.String(), err
}

func TestConnect_RequiresACode(t *testing.T) {
	app := connectApp(t, nil)

	_, err := runConnect(t, app)
	if err == nil {
		t.Fatal("connect must refuse without --code")
	}
	// It must say where a code comes from. "required" alone leaves the user looking for it.
	if !strings.Contains(err.Error(), "ForgeOps UI") {
		t.Errorf("the refusal does not say where to get a code: %v", err)
	}
}

func TestConnect_RefusesWithNoBackendAndNamesEverySource(t *testing.T) {
	// The same refusal `pair` gives, and it must be the same message: two commands that disagree
	// about how to find a backend URL would double the number of things a stuck user has to read.
	app := connectApp(t, nil)

	_, err := runConnect(t, app, "--code", "ABC234")
	if err == nil {
		t.Fatal("connect must refuse when no backend URL can be found")
	}
	for _, want := range []string{"--backend", "AGENT_BACKEND_WSS_URL", "BACKEND_PORT", "Onboarding"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not mention %q: %v", want, err)
		}
	}
}

func TestConnect_ReportsTheBackendAndItsSourceBeforeDoingAnything(t *testing.T) {
	app := connectApp(t, map[string]string{
		"AGENT_BACKEND_WSS_URL": "ws://127.0.0.1:1/api/v1/ws/agent",
	})

	// The dial will fail — nothing is listening on port 1 — and that is fine: what this asserts is
	// that the backend and its source are printed BEFORE the first stage runs, so a user who is
	// about to see a connection error already knows which host was tried.
	out, err := runConnect(t, app, "--code", "ABC234")
	if err == nil {
		t.Fatal("connect cannot have succeeded against a closed port")
	}
	if !strings.Contains(out, "ws://127.0.0.1:1/api/v1/ws/agent") {
		t.Errorf("the resolved backend was not printed before the failure:\n%s", out)
	}
	if !strings.Contains(out, string(config.BackendURLFromEnv)) {
		t.Errorf("the source of the backend URL was not printed:\n%s", out)
	}
}

func TestConnect_NamesTheStageThatFailed(t *testing.T) {
	app := connectApp(t, map[string]string{
		"AGENT_BACKEND_WSS_URL": "ws://127.0.0.1:1/api/v1/ws/agent",
	})

	_, err := runConnect(t, app, "--code", "ABC234")
	if err == nil {
		t.Fatal("expected a failure")
	}
	// A single command that fails with one unattributed line is worse than three commands that fail
	// one at a time. The stage is what tells the user whether to look at their code, their network,
	// or their workspace.
	if !strings.Contains(err.Error(), "stage 1 (pair)") {
		t.Errorf("the failure does not name the stage: %v", err)
	}
}

func TestConnect_TheFlagBeatsTheEnvironment(t *testing.T) {
	app := connectApp(t, map[string]string{
		"AGENT_BACKEND_WSS_URL": "ws://127.0.0.1:2/api/v1/ws/agent",
	})

	out, _ := runConnect(t, app,
		"--code", "ABC234", "--backend", "ws://127.0.0.1:1/api/v1/ws/agent")

	if !strings.Contains(out, "127.0.0.1:1") {
		t.Errorf("--backend did not win over the environment:\n%s", out)
	}
	if strings.Contains(out, "127.0.0.1:2") {
		t.Errorf("the environment value was used despite an explicit flag:\n%s", out)
	}
	if !strings.Contains(out, string(config.BackendURLFromFlag)) {
		t.Errorf("the source was not reported as the flag:\n%s", out)
	}
}

func TestConnect_UsesTheWorkspaceItWasGiven(t *testing.T) {
	app := connectApp(t, map[string]string{
		"AGENT_BACKEND_WSS_URL": "ws://127.0.0.1:1/api/v1/ws/agent",
	})
	workspace := t.TempDir()

	out, _ := runConnect(t, app,
		"--code", "ABC234", "--workspace", workspace)

	// Printed, because the agent is about to index it and indexing the wrong directory is a mistake
	// worth catching before it happens rather than after.
	if !strings.Contains(out, workspace) {
		t.Errorf("the workspace was not reported:\n%s", out)
	}
	if app.cfg.Executor.WorkspaceRoot != workspace {
		t.Errorf("WorkspaceRoot = %q, want %q", app.cfg.Executor.WorkspaceRoot, workspace)
	}
}

func TestConnect_DeclaresThreeStagesInItsHelp(t *testing.T) {
	app := connectApp(t, nil)
	cmd := newConnectCmd(app)

	// The help is the contract: a user running one command instead of three has to be able to see
	// what it is going to do, and that the individual verbs still exist.
	long := cmd.Long
	for _, want := range []string{"pair", "scan", "run", "adds no authority"} {
		if !strings.Contains(long, want) {
			t.Errorf("the help does not mention %q:\n%s", want, long)
		}
	}
}

func TestConnect_HasOnlyTheFlagsTheUIRenders(t *testing.T) {
	app := connectApp(t, nil)
	cmd := newConnectCmd(app)

	// `scripts/check-rendered-commands.py` cross-checks this list against the UI's declaration. This
	// asserts the same thing from the Go side, so a flag removed here fails the agent's own suite
	// rather than only the repository gate.
	want := map[string]bool{"code": true, "backend": true, "project": true, "workspace": true}
	got := map[string]bool{}
	cmd.Flags().VisitAll(func(f *pflag.Flag) { got[f.Name] = true })

	for name := range want {
		if !got[name] {
			t.Errorf("connect no longer accepts --%s, which the UI renders", name)
		}
	}
	for name := range got {
		if !want[name] {
			t.Errorf("connect accepts --%s, which the UI does not know about; add it to "+
				"AGENT_COMMANDS or to NOT_RENDERED", name)
		}
	}
}
