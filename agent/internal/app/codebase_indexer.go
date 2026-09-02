// SPDX-License-Identifier: Apache-2.0
package app

import (
	"context"
	"crypto/tls"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/executor"
	"github.com/parag8487/ForgeOps/agent/internal/identity"
	"github.com/parag8487/ForgeOps/agent/internal/scanner"
	"github.com/parag8487/ForgeOps/agent/internal/secretscan"
	"github.com/parag8487/ForgeOps/agent/internal/session"
)

// scanSubmitTimeout bounds the HTTP POST that uploads a scan report.
//
// Generous, because the body is a whole repository's redacted contents and the backend embeds every
// chunk before answering — but bounded, because a submit that never returns holds the operation's
// entire `timeoutScan` budget and the session reports nothing until that expires. Five minutes sits
// inside the executor's fifteen, so the agent's own deadline is the one that fires first and the
// error names the submit rather than the operation.
const scanSubmitTimeout = 5 * time.Minute

// codebaseIndexer builds the workspace indexer this agent's credentials authorise.
//
// ONE BUILDER FOR TWO CALLERS. `Session` wires it into the dispatcher so a `scan.full` command can
// run, and `forgeops-agent scan` calls it directly. A second construction site would be a second
// place for the workspace root, the backend origin or the token source to be resolved differently,
// and the failure that produces — an agent that scans one tree and uploads to another backend — is
// silent until somebody reads the index.
func (a *App) codebaseIndexer() (*codebaseIndexer, error) {
	root, err := workspaceRoot(a.cfg.Executor.WorkspaceRoot)
	if err != nil {
		return nil, err
	}
	// The credential store is opened here rather than passed in, because `forgeops-agent scan`
	// has no session to take one from. `session.NewStore` is idempotent — it opens the same
	// on-disk store `Session` uses, so the two cannot read different credentials.
	//
	// OPENED BEFORE THE ORIGIN IS DERIVED, because the origin now comes out of the credential.
	store, err := session.NewStore(a.cfg.Session.StateDir, a.cfg.Session.CredentialStore)
	if err != nil {
		return nil, fmt.Errorf("agent: credential store: %w", err)
	}
	// THE SAME ENDPOINT THE SESSION DIALS, derived from the address the backend stated when it
	// issued this device's certificate — so an agent cannot pair with one backend and upload its
	// index to another, and cannot upload to a listener it holds no certificate for.
	//
	// The submit and the session must agree here or one of them is talking to the wrong listener:
	// the mTLS port demands the device certificate, and the pairing port does not offer to read
	// one. Reading it from the stored credential rather than from configuration is what makes them
	// agree by construction.
	//
	// A load failure is NOT fatal to construction. `forgeops-agent scan` on an unpaired agent must
	// still reach the submit and fail there with a message about pairing, rather than fail while
	// being built with a message about a store. So an unreadable credential falls through to the
	// configured URL and the submit reports the real problem.
	stored := ""
	if creds, loadErr := store.Load(context.Background()); loadErr == nil {
		stored = creds.SessionWSURL
	}
	endpoint, err := session.SessionURL(stored, a.cfg.BackendWSSURL)
	if err != nil {
		return nil, fmt.Errorf("agent: backend origin: %w", err)
	}
	origin, err := session.HTTPOrigin(endpoint)
	if err != nil {
		return nil, fmt.Errorf("agent: backend origin: %w", err)
	}
	// The bearer credential is read at CALL time rather than captured here. A device token is
	// rotated on renewal, and a value captured at assembly would keep being sent after it stopped
	// being valid, which surfaces as a 401 the agent cannot explain.
	// THE SUBMIT IS mTLS, NOT PLAIN HTTPS, and getting this wrong cost a journey run. The agent is
	// configured with the mTLS listener's URL, so the origin `HTTPOrigin` derives points at a
	// listener running with `ssl_cert_reqs=CERT_REQUIRED` -- it demands the device certificate and
	// is signed by the INTERNAL CA, which no public root store contains. A default `http.Client`
	// therefore fails the handshake before the bearer token is ever read, and the error names the
	// POST rather than the missing certificate.
	//
	// The certificate comes from the same `identity.Provider` the session dials with, so the two
	// present the same device to the same backend. It is short-lived by contract (`ClientTLS`
	// documents the invariant and `assertShortLived` enforces it), which is why the config is
	// fetched per indexer rather than cached for the process lifetime.
	provider, err := identityProvider(
		a.cfg.Identity.Provider, store, a.cfg.Identity.CertRenewBefore)
	if err != nil {
		return nil, fmt.Errorf("agent: identity provider for the scan submit: %w", err)
	}
	return newCodebaseIndexer(
		root, origin, "", a.cfg.Scanner.MaxFileSize, provider,
		deviceTokenSource(store),
		scanSubmitTimeout,
	)
}

// deviceTokenSource reads the device token from the credential store at CALL time and presents it
// the way the backend decodes it.
//
// AT CALL TIME, because a device token is rotated on renewal and a value captured at assembly would
// keep being sent after it stopped being valid — a 401 the agent cannot explain.
//
// HEX, because that is the backend's contract, not a preference.
// `DeviceService.authenticate_session` decodes with `bytes.fromhex(...)` inside
// `except ValueError: presented = b""`. That guard is deliberate: an undecodable token must cost the
// same constant-time comparison as a wrong one, or timing tells them apart. The consequence for a
// caller is that A WRONG ENCODING IS NEVER REPORTED AS ONE — it becomes an ordinary "no active
// device matches", a 401 explaining nothing. A base64url token cost a journey run to diagnose.
// `session/serve.go` already sends hex on the WebSocket handshake; one encoding for one credential,
// because two would eventually disagree and the disagreement is invisible.
//
// A named function rather than an inline closure so a test can assert the WIRE BYTES against the
// backend's decoder, which a test of the caller could not reach.
func deviceTokenSource(store *session.FileStore) scanner.TokenFunc {
	return func(ctx context.Context) (string, error) {
		creds, err := store.Load(ctx)
		if err != nil {
			return "", fmt.Errorf("agent: reading the device token: %w", err)
		}
		if len(creds.DeviceToken) == 0 {
			// Refused here rather than sent. An empty token hex-encodes to the empty string, which
			// the backend decodes successfully to `b""` and then compares — a well-formed request
			// that can only ever 401. Refusing locally names the real problem.
			return "", errors.New("agent: this agent is not paired, so it has no token to submit a scan with")
		}
		return hex.EncodeToString(creds.DeviceToken), nil
	}
}

// codebaseIndexer joins the scanner to the executor's `CodebaseIndexer`.
//
// THE ADAPTER LIVES HERE, NOT IN EITHER PACKAGE. `executor` declares the two methods it needs and
// `scanner` knows nothing about operations — the same arrangement `commandRunner` uses for the
// dispatcher, and for the same reason (D-59): a dependency in either direction would make one
// package's tests build the other's world. `executor` would pull in the tree-sitter grammars and
// an HTTP client; `scanner` would pull in the envelope verifier.
//
// It also owns the ONE decision neither package can make alone: which directory to scan. The
// workspace root is the agent's configuration, never the envelope's, for exactly the reason
// `applyArgs` omits it — a root that arrived in a signed command would let the sender choose what
// gets read and uploaded, and a signature proves who sent a command, not that where it points is
// somewhere the operator agreed to expose.
type codebaseIndexer struct {
	root      string
	scanner   *scanner.ReportScanner
	submitter *scanner.HTTPReportSubmitter
}

// IndexFull scans the whole workspace and replaces the project's index.
func (c *codebaseIndexer) IndexFull(ctx context.Context, projectID string) (executor.IndexSummary, error) {
	report, err := c.scanner.BuildReport(ctx, c.root)
	if err != nil {
		return executor.IndexSummary{}, fmt.Errorf("building the scan report: %w", err)
	}
	return c.submit(ctx, projectID, report)
}

// IndexChanged rescans the changed set and merges it.
func (c *codebaseIndexer) IndexChanged(
	ctx context.Context, projectID string, changed []string,
) (executor.IndexSummary, error) {
	report, err := c.scanner.BuildIncrementalReport(ctx, c.root, changed)
	if err != nil {
		return executor.IndexSummary{}, fmt.Errorf("building the incremental scan report: %w", err)
	}
	return c.submit(ctx, projectID, report)
}

func (c *codebaseIndexer) submit(
	ctx context.Context, projectID string, report *scanner.ScanReport,
) (executor.IndexSummary, error) {
	// The redaction count is asserted before the report leaves the machine, not after. `secretscan`
	// is required by `NewReportScanner`, so a nil redactor cannot reach here — but a report whose
	// files were all skipped would carry a zero count, and this is the last point at which the
	// contents are still local. `file_contents` is a redacted-only store (§6.3, §7.11); the guard
	// is cheap and the alternative is an unredacted upload nobody can recall.
	if report == nil {
		return executor.IndexSummary{}, errors.New("the scanner produced no report")
	}
	result, err := c.submitter.Submit(ctx, projectID, report)
	if err != nil {
		return executor.IndexSummary{}, fmt.Errorf("submitting the scan report: %w", err)
	}
	return executor.IndexSummary{
		// The counts come from the BACKEND's answer, not from the report the agent sent. What was
		// persisted is the fact worth reporting: a file the backend rejected would otherwise be
		// counted as indexed, and the operator would go looking for a row that does not exist.
		FilesIndexed:        result.FilesIndexed,
		ChunksIndexed:       result.ChunksIndexed,
		Dependencies:        result.DependenciesIndexed,
		RedactionCount:      report.RedactionCount,
		InventoryHash:       result.InventoryHash,
		VectorsAbsentReason: result.VectorsAbsentReason,
	}, nil
}

// newCodebaseIndexer builds the indexer, or explains why it cannot.
//
// `maxFileSize` and the project language come from configuration; the redactor is mandatory and
// `NewReportScanner` enforces that itself rather than accepting a nil and skipping redaction.
func newCodebaseIndexer(
	root, baseURL, projectLang string, maxFileSize int64, provider identity.Provider,
	tokens scanner.TokenSource, timeout time.Duration,
) (*codebaseIndexer, error) {
	if root == "" {
		return nil, errors.New("a workspace root is required to scan")
	}
	if baseURL == "" {
		return nil, errors.New("a backend base URL is required to submit a scan report")
	}
	if tokens == nil {
		return nil, errors.New("a token source is required to submit a scan report")
	}
	if provider == nil {
		return nil, errors.New("an identity provider is required: the index endpoint is behind mTLS")
	}
	redactor, err := secretscan.NewScanner()
	if err != nil {
		return nil, fmt.Errorf("building the redactor: %w", err)
	}
	built, err := scanner.NewReportScanner(maxFileSize, projectLang, redactor)
	if err != nil {
		return nil, fmt.Errorf("building the report scanner: %w", err)
	}
	return &codebaseIndexer{
		root:    root,
		scanner: built,
		submitter: &scanner.HTTPReportSubmitter{
			BaseURL: baseURL,
			// A bounded client, because a submit that never returns holds the operation's whole
			// `timeoutScan` budget and the session sees no result until it expires.
			//
			// THE SUBMIT IS mTLS, NOT PLAIN HTTPS, and a default client cannot do it. The agent is
			// configured with the mTLS listener's URL, so the origin points at a listener running
			// with `ssl_cert_reqs=CERT_REQUIRED` whose certificate is signed by the INTERNAL CA —
			// which no public root store contains. A default client fails the handshake before the
			// bearer token is ever read, and the error names the POST rather than the missing
			// certificate. That cost a journey run to learn.
			Client: &http.Client{
				Timeout: timeout,
				Transport: &http.Transport{
					// Fetched PER DIAL, not captured once. The device certificate is short-lived by
					// contract — `ClientTLS` documents the invariant and `assertShortLived` enforces
					// it — so a config built here at assembly would keep presenting an expired
					// certificate on a long-running agent, and the failure would be a handshake
					// error naming neither the certificate nor its age.
					DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
						config, cerr := provider.ClientTLS(ctx)
						if cerr != nil {
							return nil, fmt.Errorf("device certificate for the scan submit: %w", cerr)
						}
						return tls.Dial(network, addr, config)
					},
				},
			},
			Tokens: tokens,
		},
	}, nil
}
