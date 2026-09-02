// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"testing"
)

// A device certificate authorises exactly ONE project. The agent never stored which, so
// `connect --project <other>` took its "already paired" shortcut, scanned a tree, and failed two
// stages later with the backend's correct-but-opaque answer:
//
//	the backend refused the scan report (403): Forbidden You do not have permission to perform
//	this action
//
// The exchange response had always carried `project_id`; it was discarded.

func TestTheProjectSurvivesAStoreRoundTrip(t *testing.T) {
	t.Parallel()

	// In the NON-SECRET half, so it costs nothing against the keychain's 2560-byte budget. Read back
	// through the public interface, because a field wired into `split` but not `join` is silently
	// lost on reload — which is exactly how this class of bug hides.
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("opening a file-backed store: %v", err)
	}
	ctx := context.Background()

	const project = "2a8a7e8f-e6d6-43b2-871a-a244102f3440"
	if err := store.Save(ctx, Credentials{
		DeviceID:    "01J0000000000000000000000C",
		DeviceToken: sizedBlob("token", credentialByteLength),
		EnvelopeKey: sizedBlob("envelope", credentialByteLength),
		ClientKey:   sizedBlob("client-key", 227),
		ClientCert:  sizedBlob("client-cert", 900),
		CABundle:    sizedBlob("ca-bundle", 656),
		ProjectID:   project,
	}); err != nil {
		t.Fatalf("saving: %v", err)
	}

	loaded, err := store.Load(ctx)
	if err != nil {
		t.Fatalf("loading: %v", err)
	}
	if loaded.ProjectID != project {
		t.Fatalf("project after a round trip = %q, want %q", loaded.ProjectID, project)
	}
}

func TestACredentialWrittenBeforeThisFieldReportsNoProject(t *testing.T) {
	t.Parallel()

	// An existing installation has no `project_id`. It must load and report empty, so `connect`
	// cannot compare and therefore behaves exactly as it did before — refusing on a fact it does not
	// have would break a working agent on upgrade.
	store, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("opening a file-backed store: %v", err)
	}
	ctx := context.Background()

	if err := store.Save(ctx, Credentials{
		DeviceID:    "01J0000000000000000000000D",
		DeviceToken: sizedBlob("token", credentialByteLength),
		EnvelopeKey: sizedBlob("envelope", credentialByteLength),
		ClientKey:   sizedBlob("client-key", 227),
		ClientCert:  sizedBlob("client-cert", 900),
		CABundle:    sizedBlob("ca-bundle", 656),
	}); err != nil {
		t.Fatalf("saving a credential with no project: %v", err)
	}

	loaded, err := store.Load(ctx)
	if err != nil {
		t.Fatalf("loading: %v", err)
	}
	if loaded.ProjectID != "" {
		t.Fatalf("project = %q, want empty for a credential that carries none", loaded.ProjectID)
	}
}

func TestTheProjectIsNotInTheSecretHalf(t *testing.T) {
	t.Parallel()

	// A project id is in every URL the operator already sees, and the certificate is what authorises
	// anything — not knowledge of the identifier. Putting it in the keychain would spend part of a
	// 2560-byte budget that a full-size credential already nearly fills, for no benefit.
	secret, public := split(Credentials{ProjectID: "p-1", SessionWSURL: "wss://x/y"})
	if public.ProjectID != "p-1" {
		t.Fatalf("the public half must carry the project; got %q", public.ProjectID)
	}
	// The secret half has no such field at all, which the compiler enforces; assert the shape did not
	// grow one by checking the round trip puts it back from the public side alone.
	rejoined := join(secret, public)
	if rejoined.ProjectID != "p-1" {
		t.Fatalf("join lost the project: %q", rejoined.ProjectID)
	}
}
