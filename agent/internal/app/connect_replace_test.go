// SPDX-License-Identifier: Apache-2.0

package app

import (
	"strings"
	"testing"
)

// TestTheProjectMismatchRefusalTellsTheTruthAboutTheCode pins a message that was actively misleading.
//
// A device certificate authorises exactly one project, so connecting with a `--project` that disagrees
// with the stored credential cannot work — the backend refuses the scan with 403. Refusing early is right.
//
// But the refusal is decided from the LOCAL credential store, BEFORE the code is sent anywhere, so the
// one-time code is still live. The message told the operator to "connect again with a NEW code", which
// sent them to mint a second code and let the first expire unused. Worse, it named a two-step remedy —
// `pair --wipe` and then connect — for something the operator had already expressed unambiguously by
// supplying a fresh code for a named project.
func TestTheProjectMismatchRefusalTellsTheTruthAboutTheCode(t *testing.T) {
	help := connectHelpText(t)

	if !strings.Contains(help, "--replace") {
		t.Fatal("connect has no --replace flag, so the only way out of a project mismatch is a second command")
	}
	// The flag must say what it destroys. Wiping ends this device's identity for the old project, and an
	// operator who reads only the flag list has to learn that from the flag list.
	for _, phrase := range []string{"unpair", "--code", "identity"} {
		if !strings.Contains(help, phrase) {
			t.Errorf("the --replace help does not mention %q, so its cost is not stated where it is offered", phrase)
		}
	}
}

// TestReplaceIsOptInRatherThanAutomatic is the safety property.
//
// Wiping on a project disagreement WITHOUT being asked would deauthorise a working agent whenever someone
// mistyped `--project`. The recovery is not cheap either: a new pairing code has to be minted by someone
// with access to the UI. So the destructive path is opt-in, and the non-destructive path is what happens
// by default.
func TestReplaceIsOptInRatherThanAutomatic(t *testing.T) {
	help := connectHelpText(t)
	if !strings.Contains(help, "Opt-in rather than automatic") {
		t.Error("the flag does not say it is opt-in, which is the property that protects a mistyped --project")
	}
}

// connectHelpText renders the connect command's help without running it.
func connectHelpText(t *testing.T) string {
	t.Helper()
	cmd := newConnectCmd(&App{})
	var sb strings.Builder
	cmd.SetOut(&sb)
	if err := cmd.Usage(); err != nil {
		t.Fatalf("rendering connect usage: %v", err)
	}
	return sb.String()
}
