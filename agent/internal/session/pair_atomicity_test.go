// SPDX-License-Identifier: Apache-2.0

package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

// A HALF-PAIRED STATE MUST NOT BE REACHABLE.
//
// `pair` is not atomic across the network and the local credential store. The exchange burns a
// single-use code, issues a 24-hour certificate and marks the device `active`; only then does the
// agent try to write what it received. On Windows that write could never succeed — the full bundle
// is past the Credential Manager's 2560-byte ceiling — so every attempt left the backend holding
// an `active` device whose token existed nowhere and no operator had reason to look at.
//
// Two guarantees are tested here, in the order they apply:
//
//  1. capacity is checked BEFORE the exchange, so the ordinary failure never spends the code;
//  2. if the write fails anyway, the device is surrendered, so nothing is left active.

// countingStore wraps a real FileStore and records the order of operations.
//
// A real store rather than a mock: the question is whether `Pair` calls CheckCapacity before it
// calls the network, and a mock store that accepted any call sequence could not answer it.
type countingStore struct {
	*FileStore
	calls         []string
	capacityErr   error
	saveErr       error
	exchangeCount *int32
}

func (c *countingStore) CheckCapacity(ctx context.Context, creds Credentials) error {
	c.calls = append(c.calls, "CheckCapacity")
	if c.capacityErr != nil {
		return c.capacityErr
	}
	return c.FileStore.CheckCapacity(ctx, creds)
}

func (c *countingStore) Save(ctx context.Context, creds Credentials) error {
	c.calls = append(c.calls, "Save")
	if c.saveErr != nil {
		return c.saveErr
	}
	return c.FileStore.Save(ctx, creds)
}

func TestPair_CapacityIsCheckedBeforeTheCodeIsSpent(t *testing.T) {
	var exchanges int32
	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	inner := backend.handler(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, exchangePath) {
			atomic.AddInt32(&exchanges, 1)
		}
		inner.ServeHTTP(w, r)
	}))
	defer server.Close()

	base, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	store := &countingStore{
		FileStore:   base,
		capacityErr: fmt.Errorf("%w: simulated 64-byte store", ErrStoreTooSmall),
	}
	manager, err := NewManager(server.URL, Deps{
		Store:        store,
		HTTPClient:   server.Client(),
		AgentVersion: "1.2.3-test",
	})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	_, err = manager.Pair(context.Background(), "ABC234", server.URL)
	if err == nil {
		t.Fatal("Pair must fail when the credential cannot be stored")
	}
	if !errors.Is(err, ErrStoreTooSmall) {
		t.Errorf("error is not ErrStoreTooSmall: %v", err)
	}

	// THE POINT OF THE WHOLE CHANGE. The code is still unspent, so the user can retry with the
	// same code after setting AGENT_CREDENTIAL_STORE=file. Before this, the code was gone.
	if n := atomic.LoadInt32(&exchanges); n != 0 {
		t.Errorf("the exchange was called %d time(s); capacity must be checked first so the "+
			"single-use code is not spent on a credential that cannot be kept", n)
	}
	if backend.liveCode != "ABC234" {
		t.Errorf("the pairing code was consumed (liveCode = %q)", backend.liveCode)
	}
	if len(store.calls) == 0 || store.calls[0] != "CheckCapacity" {
		t.Errorf("call order was %v; CheckCapacity must come first", store.calls)
	}
}

func TestPair_APersistenceFailureSurrendersTheDevice(t *testing.T) {
	var abandoned int32
	var abandonAuth atomic.Value
	abandonAuth.Store("")

	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	inner := backend.handler(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, abandonPath) {
			atomic.AddInt32(&abandoned, 1)
			abandonAuth.Store(r.Header.Get(authorizationHeader))
			w.WriteHeader(http.StatusNoContent)
			return
		}
		inner.ServeHTTP(w, r)
	}))
	defer server.Close()

	base, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	// Capacity passes and the write then fails: a full disk, or a keychain locked between the
	// probe and the write. Rare, and the state it would otherwise leave is the worst one.
	store := &countingStore{FileStore: base, saveErr: errors.New("no space left on device")}
	manager, err := NewManager(server.URL, Deps{
		Store:        store,
		HTTPClient:   server.Client(),
		AgentVersion: "1.2.3-test",
	})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	_, err = manager.Pair(context.Background(), "ABC234", server.URL)
	if err == nil {
		t.Fatal("Pair must fail when the credential cannot be written")
	}

	if n := atomic.LoadInt32(&abandoned); n != 1 {
		t.Fatalf("the surrender endpoint was called %d time(s), want 1: a spent code with an "+
			"unstorable credential must not leave an active device", n)
	}

	// Authenticated by the token just issued, which is the only credential for that device and
	// is what makes the call unable to name any other.
	auth, _ := abandonAuth.Load().(string)
	if !strings.HasPrefix(auth, bearerScheme) {
		t.Errorf("the surrender was not authenticated by the device token (header %q)", auth)
	}
	if strings.TrimPrefix(auth, bearerScheme) == "" {
		t.Error("the surrender carried an empty token")
	}

	// The user's error must name the original cause, say the device was given back, and name it.
	for _, want := range []string{"no space left on device", "surrendered", testDeviceID} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error does not mention %q: %v", want, err)
		}
	}

	// And nothing is left locally either.
	if _, loadErr := store.Load(context.Background()); !errors.Is(loadErr, ErrNoCredentials) {
		t.Errorf("credentials were left behind after a failed save: %v", loadErr)
	}
}

func TestPair_AFailedSurrenderSaysTheDeviceMayStillBeActive(t *testing.T) {
	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	inner := backend.handler(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, abandonPath) {
			// The backend is unreachable for the surrender too: the one path that can still
			// leave a row an operator has to clean up.
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		inner.ServeHTTP(w, r)
	}))
	defer server.Close()

	base, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	store := &countingStore{FileStore: base, saveErr: errors.New("no space left on device")}
	manager, err := NewManager(server.URL, Deps{
		Store:        store,
		HTTPClient:   server.Client(),
		AgentVersion: "1.2.3-test",
	})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	_, err = manager.Pair(context.Background(), "ABC234", server.URL)
	if err == nil {
		t.Fatal("Pair must fail")
	}

	// HONESTY IS THE REQUIREMENT HERE. When both the write and the surrender fail, the system IS
	// inconsistent, and the message must say so and name the device rather than implying the
	// state was cleaned up. A reassuring message here would be the worst possible outcome.
	for _, want := range []string{"may still be active", testDeviceID, "Revoke it"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error does not mention %q, so the operator is not told what to clean "+
				"up: %v", want, err)
		}
	}
}

func TestPair_TheSurrenderNamesNoDevice(t *testing.T) {
	var body atomic.Value
	body.Store("")

	ca := newTestCA(t)
	backend := &fakeBackend{ca: ca, liveCode: "ABC234"}
	inner := backend.handler(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, abandonPath) {
			buf := make([]byte, 4096)
			n, _ := r.Body.Read(buf)
			body.Store(string(buf[:n]))
			w.WriteHeader(http.StatusNoContent)
			return
		}
		inner.ServeHTTP(w, r)
	}))
	defer server.Close()

	base, err := NewStore(t.TempDir(), "file")
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	store := &countingStore{FileStore: base, saveErr: errors.New("disk full")}
	manager, err := NewManager(server.URL, Deps{
		Store: store, HTTPClient: server.Client(), AgentVersion: "1.2.3-test",
	})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	_, _ = manager.Pair(context.Background(), "ABC234", server.URL)

	// THE AUTHORITY BOUND. The route takes no device id: the token selects the row, so the call
	// cannot abandon anything but itself. If a device id ever appears in this body, the operation
	// has gained the ability to name another device and the security argument for authenticating
	// it with one factor no longer holds.
	sent, _ := body.Load().(string)
	if strings.Contains(sent, testDeviceID) {
		t.Errorf("the surrender request names a device id (%q); it must identify the device by "+
			"its token alone", sent)
	}
	var decoded map[string]any
	if sent != "" && json.Unmarshal([]byte(sent), &decoded) == nil {
		if _, ok := decoded["device_id"]; ok {
			t.Error("the surrender request carries a device_id field")
		}
	}
}
