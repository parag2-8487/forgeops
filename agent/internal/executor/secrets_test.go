// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// `secrets.inject` (FR-45), and the negatives that matter more than the positive.
//
// FR-45 has three clauses: secrets reach a deployment as environment variables, are never written into a
// generated file, and are never included in LLM context. The third is the backend's (`secrets/redaction.py`
// and `test_secret_injection_negatives.py` cover it). The first two are here, and the second is why this
// operation keeps values in process memory rather than writing a `.env`: a file satisfies "environment
// variables" and breaks "never written into a generated file", and it is readable by every process the
// user runs, survives a reboot, and gets committed by accident.

// : Assembled from fragments so no line of this file carries a credential shape —
// : `scripts/check-added-shapes.py` rejects one on any added line, and it is right to.
func syntheticSecret() string {
	prefix := "AK" + "IA"
	return prefix + strings.Repeat("D", 16)
}

// countFiles counts every regular file under root, so "wrote nothing" is a measurement.
func countFiles(t *testing.T, root string) int {
	t.Helper()
	count := 0
	err := filepath.WalkDir(root, func(_ string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !entry.IsDir() {
			count++
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walking %s: %v", root, err)
	}
	return count
}

func TestSecretsInject_IsMutatingAndNeedsAnApproval(t *testing.T) {
	// It writes no byte, and it is still a mutation: it changes what a later deployment does, which is
	// what an approver is being asked about. Classifying it as a read because it does not call
	// os.WriteFile would let a production environment change through without a human.
	row, ok := handlerTable[OpSecretsInject]
	if !ok {
		t.Fatal("secrets.inject left the catalogue")
	}
	if !row.implemented {
		t.Fatal("secrets.inject is still unimplemented")
	}
	if !row.mutating || !row.requiresApproval {
		t.Errorf("secrets.inject is mutating=%v requiresApproval=%v; both must be true",
			row.mutating, row.requiresApproval)
	}
}

func TestSecretsInject_ReportsKeysAndNeverValues(t *testing.T) {
	secret := syntheticSecret()
	d := newDispatcher(t, t.TempDir())
	res, err := d.Execute(context.Background(), verified(t, OpSecretsInject, "approval-si-1", map[string]any{
		"project_id": "proj-1",
		"values":     map[string]string{"AWS_ACCESS_KEY_ID": secret, "DATABASE_PASSWORD": "hunter2"},
	}, 41), nil)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Status != "injected" {
		t.Fatalf("status %q", res.Status)
	}
	// THE ASSERTION THIS OPERATION EXISTS FOR. A `command.result` travels over the websocket, is
	// persisted, and reaches an append-only hash-chained audit trail — so a value in it would make the
	// tamper-evident log the most durable copy of the secret.
	if strings.Contains(res.Output, secret) || strings.Contains(res.Output, "hunter2") {
		t.Fatalf("the result carried a secret value: %s", res.Output)
	}

	var report SecretInjectionReport
	if err := json.Unmarshal([]byte(res.Output), &report); err != nil {
		t.Fatalf("output is not an injection report: %v", err)
	}
	if report.Count != 2 {
		t.Errorf("Count = %d, want 2", report.Count)
	}
	// Keys travel and are sorted, so two identical injections produce an identical report.
	if got := strings.Join(report.Keys, ","); got != "AWS_ACCESS_KEY_ID,DATABASE_PASSWORD" {
		t.Errorf("keys = %q", got)
	}
}

func TestSecretsInject_WritesNothingToTheWorkspace(t *testing.T) {
	// FR-45's second clause, asserted rather than described: the workspace is unchanged afterwards.
	root := t.TempDir()
	before := countFiles(t, root)
	d := newDispatcher(t, root)
	if _, err := d.Execute(context.Background(), verified(t, OpSecretsInject, "approval-si-2", map[string]any{
		"project_id": "proj-1",
		"values":     map[string]string{"TOKEN": syntheticSecret()},
	}, 42), nil); err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if after := countFiles(t, root); after != before {
		t.Fatalf("the workspace gained %d file(s); secrets must never be written to disk", after-before)
	}
}

func TestSecretsInject_ProgressNeverNamesAValue(t *testing.T) {
	secret := syntheticSecret()
	sink := &recordingSink{}
	d := newDispatcher(t, t.TempDir())
	if _, err := d.Execute(context.Background(), verified(t, OpSecretsInject, "approval-si-3", map[string]any{
		"project_id": "proj-1",
		"values":     map[string]string{"TOKEN": secret},
	}, 43), sink); err != nil {
		t.Fatalf("Execute: %v", err)
	}
	joined := strings.Join(sink.events, " | ")
	if strings.Contains(joined, secret) {
		t.Fatalf("a progress event carried the value: %s", joined)
	}
	// The NAME is expected to appear: an operator watching a deployment needs to know which credentials
	// were supplied, and a name is not a secret.
	if !strings.Contains(joined, "TOKEN") {
		t.Errorf("no progress event named the injected key: %s", joined)
	}
}

func TestSecretsInject_RefusesVariablesThatChooseWhichBinaryRuns(t *testing.T) {
	// The escalation this operation would otherwise be. `secrets.inject` is approval-gated, and a human
	// approving "inject the database password" has not approved replacing PATH — which decides which
	// binary every later command runs.
	for _, key := range []string{"PATH", "LD_PRELOAD", "NODE_OPTIONS", "PYTHONPATH", "path"} {
		d := newDispatcher(t, t.TempDir())
		_, err := d.Execute(context.Background(), verified(t, OpSecretsInject, "approval-si-4", map[string]any{
			"project_id": "proj-1",
			"values":     map[string]string{key: "/tmp/evil"},
		}, 44), nil)
		if err == nil {
			t.Errorf("%q was accepted as an injectable variable", key)
			continue
		}
		if !strings.Contains(err.Error(), "binary") {
			t.Errorf("refusal for %q does not explain why: %v", key, err)
		}
	}
}

func TestSecretsInject_RefusesMalformedRequests(t *testing.T) {
	cases := map[string]map[string]any{
		"no project":     {"values": map[string]string{"A": "b"}},
		"no values":      {"project_id": "proj-1", "values": map[string]string{}},
		"empty name":     {"project_id": "proj-1", "values": map[string]string{"": "b"}},
		"padded name":    {"project_id": "proj-1", "values": map[string]string{" A ": "b"}},
		"equals in name": {"project_id": "proj-1", "values": map[string]string{"A=B": "c"}},
	}
	for name, args := range cases {
		d := newDispatcher(t, t.TempDir())
		if _, err := d.Execute(context.Background(),
			verified(t, OpSecretsInject, "approval-si-5", args, 45), nil); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestSecretEnvironment_KeepsProjectsApart(t *testing.T) {
	store := newSecretEnvironment()
	store.put("proj-a", map[string]string{"TOKEN": "value-a"})
	store.put("proj-b", map[string]string{"TOKEN": "value-b"})

	if got := store.env("proj-a")["TOKEN"]; got != "value-a" {
		t.Errorf("proj-a TOKEN = %q", got)
	}
	if got := store.env("proj-b")["TOKEN"]; got != "value-b" {
		t.Errorf("proj-b TOKEN = %q", got)
	}
	if store.env("proj-c") != nil {
		t.Error("a project with no injection returned an environment")
	}
}

func TestSecretEnvironment_ReturnsACopySoTheStoreCannotBeMutatedThroughIt(t *testing.T) {
	store := newSecretEnvironment()
	store.put("proj-a", map[string]string{"TOKEN": "original"})
	handed := store.env("proj-a")
	handed["TOKEN"] = "tampered"
	handed["INJECTED_BY_CALLER"] = "x"

	fresh := store.env("proj-a")
	if fresh["TOKEN"] != "original" {
		t.Error("mutating the handed-out map changed the store")
	}
	if _, present := fresh["INJECTED_BY_CALLER"]; present {
		t.Error("a caller added a variable to the store by mutating its copy")
	}
}

func TestSecretEnvironment_ReportsWhatItReplaced(t *testing.T) {
	store := newSecretEnvironment()
	store.put("proj-a", map[string]string{"TOKEN": "first", "OTHER": "x"})
	replaced := store.put("proj-a", map[string]string{"TOKEN": "second", "NEW": "y"})
	if len(replaced) != 1 || replaced[0] != "TOKEN" {
		t.Errorf("replaced = %v, want [TOKEN]", replaced)
	}
	// A silent overwrite of a production credential is exactly the event worth naming.
	if got := store.env("proj-a")["TOKEN"]; got != "second" {
		t.Errorf("TOKEN = %q after replacement", got)
	}
}

func TestSecretEnvironment_KeysNeverExposeValues(t *testing.T) {
	store := newSecretEnvironment()
	secret := syntheticSecret()
	store.put("proj-a", map[string]string{"TOKEN": secret})
	for _, key := range store.keys("proj-a") {
		if strings.Contains(key, secret) {
			t.Fatal("keys() returned a value")
		}
	}
	store.forget("proj-a")
	if store.env("proj-a") != nil {
		t.Error("forget() left the values behind")
	}
}
