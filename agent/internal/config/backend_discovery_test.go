// SPDX-License-Identifier: Apache-2.0

package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Finding the backend URL without ever guessing at a remote host.
//
// The first run had no way to learn the value. `pair` refused with "pair needs --backend or
// AGENT_BACKEND_WSS_URL" — two variable names and no value — while the correct answer for the
// development stack sat in the repository's own `.env` as `BACKEND_PORT=18000`.
//
// The refusal is right and stays: a device token is a bearer credential, and an agent that invented
// an `https` host would hand one to whatever answered. What is added is DISCOVERY, bounded by one
// rule that carries the whole safety argument — a value nobody typed may only name loopback.

func writeDotEnv(t *testing.T, dir, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, ".env"), []byte(body), 0o600); err != nil {
		t.Fatalf("writing .env: %v", err)
	}
}

func TestDiscoverBackendURL_AnExplicitValueAlwaysWins(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	writeDotEnv(t, dir, "BACKEND_PORT=18000\n")

	// THE PROPERTY THAT KEEPS THIS CONVENIENCE FROM BECOMING A REDIRECT. A discovered value must
	// never displace one a human named, whichever order they are found in.
	got, source, err := DiscoverBackendURL(
		"wss://named-by-the-operator.example:9000/api/v1/ws/agent",
		"wss://from-the-environment.example:9001/api/v1/ws/agent",
		dir,
	)
	if err != nil {
		t.Fatalf("DiscoverBackendURL: %v", err)
	}
	if got != "wss://named-by-the-operator.example:9000/api/v1/ws/agent" {
		t.Errorf("the flag did not win: got %q", got)
	}
	if source != BackendURLFromFlag {
		t.Errorf("source = %q, want %q", source, BackendURLFromFlag)
	}
}

func TestDiscoverBackendURL_TheEnvironmentBeatsTheDotEnv(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	writeDotEnv(t, dir, "BACKEND_PORT=18000\n")

	got, source, err := DiscoverBackendURL("", "wss://backend.example:9001/api/v1/ws/agent", dir)
	if err != nil {
		t.Fatalf("DiscoverBackendURL: %v", err)
	}
	if got != "wss://backend.example:9001/api/v1/ws/agent" {
		t.Errorf("the environment did not win over .env: got %q", got)
	}
	if source != BackendURLFromEnv {
		t.Errorf("source = %q, want %q", source, BackendURLFromEnv)
	}
}

func TestDiscoverBackendURL_DerivesTheLocalStackFromBackendPort(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	// The value the repository actually ships in its own .env, which is what a developer on the
	// same machine as the stack has.
	writeDotEnv(t, dir, "# comment\nPOSTGRES_PORT=15432\nBACKEND_PORT=18000\nFRONTEND_PORT=13000\n")

	got, source, err := DiscoverBackendURL("", "", dir)
	if err != nil {
		t.Fatalf("DiscoverBackendURL: %v", err)
	}
	want := "ws://localhost:18000/api/v1/ws/agent"
	if got != want {
		t.Errorf("got %q, want %q — this is the value the first run could not find", got, want)
	}
	if source != BackendURLFromDotEnv {
		t.Errorf("source = %q, want %q", source, BackendURLFromDotEnv)
	}
}

func TestDiscoverBackendURL_WalksUpToTheRepositoryRoot(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	writeDotEnv(t, root, "BACKEND_PORT=18000\n")
	deep := filepath.Join(root, "agent", "internal", "session")
	if err := os.MkdirAll(deep, 0o750); err != nil {
		t.Fatalf("MkdirAll: %v", err)
	}

	// A developer runs the agent from wherever they happen to be inside the tree.
	got, _, err := DiscoverBackendURL("", "", deep)
	if err != nil {
		t.Fatalf("DiscoverBackendURL: %v", err)
	}
	if got != "ws://localhost:18000/api/v1/ws/agent" {
		t.Errorf("the upward search did not find the root .env: got %q", got)
	}
}

func TestDiscoverBackendURL_TheUpwardSearchIsBounded(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	writeDotEnv(t, root, "BACKEND_PORT=18000\n")
	// One level deeper than the search allows.
	parts := make([]string, dotEnvSearchDepth+2)
	for i := range parts {
		parts[i] = "d"
	}
	deep := filepath.Join(append([]string{root}, parts...)...)
	if err := os.MkdirAll(deep, 0o750); err != nil {
		t.Fatalf("MkdirAll: %v", err)
	}

	// An unbounded walk can reach a parent belonging to a different project, or to no project,
	// and silently adopt its configuration.
	got, source, err := DiscoverBackendURL("", "", deep)
	if err != nil {
		t.Fatalf("DiscoverBackendURL: %v", err)
	}
	if got != "" {
		t.Errorf("the search went past its bound and found %q", got)
	}
	if source != BackendURLUnset {
		t.Errorf("source = %q, want %q", source, BackendURLUnset)
	}
}

func TestDiscoverBackendURL_RefusesToDiscoverANonLoopbackHost(t *testing.T) {
	t.Parallel()

	// The rule stated directly. There is no `.env` key that produces a remote host today, so this
	// tests the predicate that enforces it — the thing a future change would have to defeat.
	for _, host := range []string{
		"backend.example.com",
		"10.0.0.5",
		"192.168.1.20",
		"0.0.0.0",
	} {
		if err := validateBackendURL("ws://"+host+":18000/api/v1/ws/agent", true); err == nil {
			t.Errorf("a discovered URL naming %q was accepted; a value nobody typed must be "+
				"loopback or the agent could be pointed at another machine by a dropped file", host)
		}
	}

	// And the same hosts are fine when a human named them.
	for _, host := range []string{"backend.example.com", "10.0.0.5"} {
		if err := validateBackendURL("wss://"+host+":18000/api/v1/ws/agent", false); err != nil {
			t.Errorf("an explicit URL naming %q was refused: %v", host, err)
		}
	}
}

func TestValidateBackendURL_AcceptsEveryLoopbackSpelling(t *testing.T) {
	t.Parallel()

	// A developer's stack can be reached by any of these, and refusing one would look like a bug
	// in discovery rather than a deliberate bound.
	for _, host := range []string{"localhost", "127.0.0.1", "[::1]"} {
		if err := validateBackendURL("ws://"+host+":18000/api/v1/ws/agent", true); err != nil {
			t.Errorf("loopback host %q was refused: %v", host, err)
		}
	}
}

func TestReadBackendPort_IgnoresWhatItShould(t *testing.T) {
	t.Parallel()

	for name, body := range map[string]string{
		"commented out":    "# BACKEND_PORT=18000\n",
		"a different key":  "FRONTEND_PORT=13000\n",
		"not a number":     "BACKEND_PORT=eighteen-thousand\n",
		"out of range":     "BACKEND_PORT=99999\n",
		"zero":             "BACKEND_PORT=0\n",
		"prefix collision": "MY_BACKEND_PORT=18000\n",
		"empty":            "",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeDotEnv(t, dir, body)
			if port, ok := readBackendPort(filepath.Join(dir, ".env")); ok {
				t.Errorf("%s produced port %d", name, port)
			}
		})
	}
}

func TestReadBackendPort_AcceptsQuotedAndSpacedValues(t *testing.T) {
	t.Parallel()

	// `.env` belongs to the stack, not to the agent, and people write it by hand.
	for name, body := range map[string]string{
		"quoted":        "BACKEND_PORT=\"18000\"\n",
		"single quoted": "BACKEND_PORT='18000'\n",
		"spaced":        "BACKEND_PORT = 18000\n",
		"trailing":      "BACKEND_PORT=18000   \n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeDotEnv(t, dir, body)
			port, ok := readBackendPort(filepath.Join(dir, ".env"))
			if !ok || port != 18000 {
				t.Errorf("%s gave (%d, %v), want (18000, true)", name, port, ok)
			}
		})
	}
}

func TestBackendURLRemedy_NamesEverySourceAndInventsNoPort(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	remedy := BackendURLRemedy(dir)

	// Every source, in precedence order, because a user who does not know the order cannot tell
	// which one to change.
	for _, want := range []string{"--backend", "AGENT_BACKEND_WSS_URL", "BACKEND_PORT", ".env"} {
		if !strings.Contains(remedy, want) {
			t.Errorf("the remedy does not mention %q:\n%s", want, remedy)
		}
	}
	// And it points at the one place that always knows the answer.
	if !strings.Contains(remedy, "Onboarding") {
		t.Errorf("the remedy does not point at the UI:\n%s", remedy)
	}

	// NO INVENTED PORT. `.env.example` ships 8000 and this repository's `.env` says 18000, so any
	// number printed without reading a real file would be wrong for somebody — and would look
	// authoritative while being wrong.
	if strings.Contains(remedy, "localhost:18000") || strings.Contains(remedy, "localhost:8000") {
		t.Errorf("the remedy invents a port with no .env to read it from:\n%s", remedy)
	}
}

func TestBackendURLRemedy_PrintsTheRealValueWhenItCanReadOne(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	writeDotEnv(t, dir, "BACKEND_PORT=19999\n")

	remedy := BackendURLRemedy(dir)
	// When a real port IS available, the message must give the whole flag rather than a shape to
	// fill in. That is the difference between the original refusal and a usable one.
	if !strings.Contains(remedy, "--backend ws://localhost:19999/api/v1/ws/agent") {
		t.Errorf("the remedy does not print the complete flag from the real .env:\n%s", remedy)
	}
}
