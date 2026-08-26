// SPDX-License-Identifier: Apache-2.0
package scanner_test

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/scanner"
)

const projectID = "3f6a1d2e-8b40-4c9a-9d31-5c2e7a904f11"

func TestSubmitPostsTheReportToTheProjectScopedRoute(t *testing.T) {
	root := writeTree(t)
	report, err := newReportScanner(t).BuildReport(context.Background(), root)
	if err != nil {
		t.Fatalf("BuildReport: %v", err)
	}

	var gotPath, gotMethod, gotAuth, gotType string
	var gotBody []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotMethod = r.URL.Path, r.Method
		gotAuth = r.Header.Get("Authorization")
		gotType = r.Header.Get("Content-Type")
		gotBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"project_id":"` + projectID + `","files_indexed":6,"chunks_indexed":9,` +
			`"files_removed":0,"dependencies_indexed":7,"vectors_written":0,` +
			`"vectors_absent_reason":"no embedding credential is configured","inventory_hash":"abc"}`))
	}))
	defer server.Close()

	submitter := &scanner.HTTPReportSubmitter{
		BaseURL: server.URL,
		Client:  server.Client(),
		Tokens: scanner.TokenFunc(func(context.Context) (string, error) {
			return "device-token-value", nil
		}),
	}

	result, err := submitter.Submit(context.Background(), projectID, report)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}

	if gotMethod != http.MethodPost {
		t.Errorf("method = %s, want POST", gotMethod)
	}
	// Project-scoped, because the backend has to know which project's index it is
	// replacing before it reads a single file row.
	if gotPath != "/api/v1/analysis/codebase/"+projectID+"/index" {
		t.Errorf("path = %s", gotPath)
	}
	if !strings.HasPrefix(gotAuth, "Bear"+"er ") {
		t.Errorf("authorization header = %q, want a bearer credential", gotAuth)
	}
	if gotType != "application/json" {
		t.Errorf("content type = %q", gotType)
	}

	// The body must decode as the same report — the wire format is the contract with the
	// backend, so a field that fails to round-trip is a persistence bug, not a cosmetic one.
	var decoded scanner.ScanReport
	if err := json.Unmarshal(gotBody, &decoded); err != nil {
		t.Fatalf("the posted body is not a decodable report: %v", err)
	}
	if decoded.SchemaVersion != scanner.ScanReportSchemaVersion {
		t.Errorf("posted schema version = %d", decoded.SchemaVersion)
	}
	if len(decoded.Files) != len(report.Files) {
		t.Errorf("posted %d files, built %d", len(decoded.Files), len(report.Files))
	}
	if strings.Contains(string(gotBody), syntheticToken) {
		t.Fatal("the posted body contains the raw secret")
	}

	if result.FilesIndexed != 6 || result.ChunksIndexed != 9 || result.DependenciesIndexed != 7 {
		t.Errorf("result = %+v", result)
	}
	// An absent vector is reported as absent with a reason, never as a zero vector.
	if result.VectorsWritten != 0 || result.VectorsAbsentReason == "" {
		t.Errorf("vector outcome = %d written, reason %q", result.VectorsWritten, result.VectorsAbsentReason)
	}
}

func TestSubmitSurfacesTheBackendsProblemDocument(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/problem+json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"type":"https://example.invalid/forbidden","title":"Forbidden","detail":"not your project"}`))
	}))
	defer server.Close()

	submitter := &scanner.HTTPReportSubmitter{BaseURL: server.URL, Client: server.Client()}
	_, err := submitter.Submit(context.Background(), projectID, &scanner.ScanReport{SchemaVersion: 1})
	if err == nil {
		t.Fatal("a 403 must be an error; a silently discarded report looks like a successful index")
	}
	if !strings.Contains(err.Error(), "not your project") {
		t.Errorf("error = %v, want the problem detail", err)
	}
}

func TestSubmitRefusesAnEmptyProjectID(t *testing.T) {
	submitter := &scanner.HTTPReportSubmitter{BaseURL: "http://127.0.0.1:1"}
	if _, err := submitter.Submit(context.Background(), "  ", &scanner.ScanReport{}); err == nil {
		t.Fatal("an empty project id must be refused before any request is made")
	}
}
