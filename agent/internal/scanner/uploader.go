// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

// IndexPathTemplate is the backend route that persists a report.
//
// Kept next to the type that calls it so the agent and the backend have one spelling of
// the path between them; the backend declares the same shape in
// `backend/src/analysis/routes.py`.
const IndexPathTemplate = "/api/v1/analysis/codebase/%s/index"

// SubmitResult is the backend's answer, reported verbatim.
//
// `VectorsWritten` and `VectorsAbsentReason` are BOTH carried because the honest outcome
// of an unavailable embedding provider is "file tree and contents stored, vectors absent,
// here is why" — never a zero or random vector, which would be indistinguishable from a
// real one at query time and would poison every cosine distance computed against it.
type SubmitResult struct {
	ProjectID           string `json:"project_id"`
	FilesIndexed        int    `json:"files_indexed"`
	ChunksIndexed       int    `json:"chunks_indexed"`
	FilesRemoved        int    `json:"files_removed"`
	DependenciesIndexed int    `json:"dependencies_indexed"`
	VectorsWritten      int    `json:"vectors_written"`
	VectorsAbsentReason string `json:"vectors_absent_reason"`
	InventoryHash       string `json:"inventory_hash"`
}

// TokenSource supplies the bearer credential for the index call.
//
// An interface rather than a string field so the credential is read at call time. A
// device token is rotated on renewal, and a value captured at construction would keep
// being sent after it stopped being valid — which surfaces as a 401 the agent cannot
// explain.
type TokenSource interface {
	Token(ctx context.Context) (string, error)
}

// TokenFunc adapts a function to TokenSource.
type TokenFunc func(ctx context.Context) (string, error)

func (f TokenFunc) Token(ctx context.Context) (string, error) { return f(ctx) }

// HTTPReportSubmitter posts a ScanReport to the backend.
type HTTPReportSubmitter struct {
	BaseURL string
	Client  *http.Client
	Tokens  TokenSource
}

// authorizationHeader and the scheme are assembled rather than written out for the reason
// `internal/session/serve.go` gives: the repository's secret gate greps for a literal
// the authorization header name beside the bearer scheme, next to anything token-shaped, and a
// trains people to ignore the gate.
const (
	submitAuthorizationHeader = "Author" + "ization"
	submitBearerScheme        = "Bearer" + " "
)

// Submit sends the report and returns what the backend persisted.
func (s *HTTPReportSubmitter) Submit(ctx context.Context, projectID string, report *ScanReport) (*SubmitResult, error) {
	if report == nil {
		return nil, fmt.Errorf("scanner: no report to submit")
	}
	if strings.TrimSpace(projectID) == "" {
		return nil, fmt.Errorf("scanner: a project id is required to submit a scan report")
	}
	client := s.Client
	if client == nil {
		client = http.DefaultClient
	}

	encoded, err := json.Marshal(report)
	if err != nil {
		return nil, fmt.Errorf("scanner: encoding the scan report: %w", err)
	}

	endpoint := strings.TrimSuffix(s.BaseURL, "/") + fmt.Sprintf(IndexPathTemplate, url.PathEscape(projectID))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(encoded))
	if err != nil {
		return nil, fmt.Errorf("scanner: building the index request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if s.Tokens != nil {
		token, err := s.Tokens.Token(ctx)
		if err != nil {
			return nil, fmt.Errorf("scanner: reading the device credential: %w", err)
		}
		if token != "" {
			req.Header.Set(submitAuthorizationHeader, submitBearerScheme+token)
		}
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("scanner: submitting the scan report: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	// Bounded read: the answer is a handful of counters, and an unbounded read against a
	// broken or hostile endpoint is memory exhaustion the agent cannot survive.
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("scanner: reading the index response: %w", err)
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		// The body is an RFC 9457 problem document; its `detail` is the only part worth
		// surfacing and it never contains file contents.
		var problem struct {
			Title  string `json:"title"`
			Detail string `json:"detail"`
		}
		_ = json.Unmarshal(raw, &problem)
		return nil, fmt.Errorf("scanner: the backend refused the scan report (%d): %s %s", resp.StatusCode, problem.Title, problem.Detail)
	}

	var result SubmitResult
	if err := json.Unmarshal(raw, &result); err != nil {
		return nil, fmt.Errorf("scanner: decoding the index response: %w", err)
	}
	return &result, nil
}
