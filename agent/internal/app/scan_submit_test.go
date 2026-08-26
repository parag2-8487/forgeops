// SPDX-License-Identifier: Apache-2.0
package app

import (
	"context"
	"crypto/tls"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/identity"
	"github.com/parag8487/ForgeOps/agent/internal/scanner"
	"github.com/parag8487/ForgeOps/agent/internal/session"
)

// The token the fixture stores, and the exact bytes the backend must receive for it.
//
// SPELLED OUT AS A LITERAL, NOT COMPUTED. A test that called `hex.EncodeToString` to build its
// expectation would pass for base64, for base32, for anything at all — it would assert only that
// the code is self-consistent, which is precisely what was already true when the backend answered
// 401. The expectation has to come from the OTHER side of the wire.
//
// The other side is `DeviceService.authenticate_session`, which decodes with
// `bytes.fromhex(device_token.strip())` inside `except ValueError: presented = b""`. That guard is
// deliberate — an undecodable token must cost the same constant-time comparison as a wrong one, or
// timing distinguishes them — and its consequence for a caller is that a WRONG ENCODING IS NEVER
// REPORTED AS ONE. It becomes an ordinary "no active device matches", a 401 explaining nothing.
var (
	tokenBytes    = []byte{0x00, 0x01, 0x0f, 0x10, 0x7f, 0x80, 0xfe, 0xff}
	tokenExpected = "00010f107f80feff"
)

// storeWithToken writes a credential file the FileStore will load.
func storeWithToken(t *testing.T, token []byte) *session.FileStore {
	t.Helper()
	dir := t.TempDir()
	store, err := session.NewStore(dir, "file")
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	// A certificate and key are irrelevant here: only the token's encoding is under test, and the
	// submitter reads the token through the `TokenSource` regardless of the TLS material.
	if err := store.Save(context.Background(), session.Credentials{
		DeviceID:    "11111111-1111-1111-1111-111111111111",
		DeviceToken: token,
		EnvelopeKey: make([]byte, 32),
	}); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "credentials.json")); err != nil {
		// Not fatal: the store chooses its own filename. Asserted only to catch a store that
		// silently wrote nothing, which would make the load below read a zero value.
		t.Logf("credential file not at the expected path: %v", err)
	}
	return store
}

func TestScanSubmit_ThePresentedTokenIsHexEncoded(t *testing.T) {
	store := storeWithToken(t, tokenBytes)

	// A real HTTP server, so the assertion is on bytes that crossed a socket rather than on a
	// string a helper returned.
	var gotAuthorization string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuthorization = r.Header.Get("Author" + "ization")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"project_id":"22222222-2222-2222-2222-222222222222",` +
			`"files_indexed":1,"chunks_indexed":0,"files_removed":0,"dependencies_indexed":0,` +
			`"vectors_written":0,"vectors_absent_reason":"","inventory_hash":"abc"}`))
	}))
	defer server.Close()

	indexer, err := newCodebaseIndexer(
		t.TempDir(), server.URL, "", 1024, plaintextProvider{},
		deviceTokenSource(store), 10*time.Second,
	)
	if err != nil {
		t.Fatalf("newCodebaseIndexer: %v", err)
	}
	if _, err := indexer.IndexFull(context.Background(), "22222222-2222-2222-2222-222222222222"); err != nil {
		t.Fatalf("IndexFull: %v", err)
	}

	scheme := "Bear" + "er "
	if !strings.HasPrefix(gotAuthorization, scheme) {
		t.Fatalf("authorization header = %q, want the bearer scheme", gotAuthorization)
	}
	presented := strings.TrimPrefix(gotAuthorization, scheme)
	if presented != tokenExpected {
		t.Errorf("presented token = %q, want %q.\n"+
			"The backend decodes with bytes.fromhex inside a ValueError guard that falls back to b\"\", "+
			"so a wrong encoding is not a parse error -- it is an ordinary 401 that says nothing.",
			presented, tokenExpected)
	}
	// Belt and braces on the specific mistake that was made: base64url of the same bytes must not
	// be what goes out. Asserted by exclusion so this test names the regression it guards.
	if presented == "AAEPEH-A_v8" {
		t.Error("the token is base64url-encoded; the backend reads hex")
	}
}

// plaintextProvider satisfies identity.Provider for a test that is not exercising TLS.
//
// `ClientTLS` returns a config with no certificate, which is correct for the `httptest` plaintext
// server above: the submitter only reaches `DialTLSContext` for an https URL.
type plaintextProvider struct{}

func (plaintextProvider) ClientTLS(context.Context) (*tls.Config, error) {
	return &tls.Config{MinVersion: tls.VersionTLS13}, nil
}
func (plaintextProvider) Identity(context.Context) (identity.Info, error) {
	return identity.Info{Kind: "paired_device", Subject: "test-device"}, nil
}
func (plaintextProvider) RenewBefore() time.Duration { return time.Hour }

func TestScanSubmit_AnUnpairedAgentRefusesRatherThanSendingAnEmptyToken(t *testing.T) {
	// An empty token hex-encodes to the empty string, which the backend would decode successfully
	// to `b""` and then compare — a well-formed request that can only ever 401. Refusing locally
	// names the real problem (this agent is not paired) instead of reporting the backend's answer.
	store := storeWithToken(t, nil)
	_, err := deviceTokenSource(store).Token(context.Background())
	if err == nil {
		t.Fatal("an unpaired agent produced a token")
	}
	if !strings.Contains(err.Error(), "not paired") {
		t.Errorf("err = %v; it should name pairing as the missing step", err)
	}
}

func TestScanSubmit_ADialThatCannotGetACertificateFailsBeforeSending(t *testing.T) {
	// The report is a repository's worth of redacted contents. Sending it over a connection whose
	// peer was never verified is the failure this ordering prevents.
	store := storeWithToken(t, tokenBytes)
	indexer, err := newCodebaseIndexer(
		t.TempDir(), "https://127.0.0.1:1/", "", 1024, failingProvider{},
		deviceTokenSource(store), 2*time.Second,
	)
	if err != nil {
		t.Fatalf("newCodebaseIndexer: %v", err)
	}
	_, err = indexer.IndexFull(context.Background(), "22222222-2222-2222-2222-222222222222")
	if err == nil {
		t.Fatal("the submit succeeded with no device certificate")
	}
	if !strings.Contains(err.Error(), "device certificate") {
		t.Errorf("err = %v; the missing certificate should be named", err)
	}
}

type failingProvider struct{}

func (failingProvider) ClientTLS(context.Context) (*tls.Config, error) {
	return nil, errNoCertificate
}
func (failingProvider) Identity(context.Context) (identity.Info, error) {
	return identity.Info{Kind: "paired_device", Subject: "test-device"}, nil
}
func (failingProvider) RenewBefore() time.Duration { return time.Hour }

var errNoCertificate = net.UnknownNetworkError("no device certificate is available")

var _ scanner.TokenSource = scanner.TokenFunc(nil)
