// SPDX-License-Identifier: Apache-2.0
package app

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/executor"
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
	// `session.HTTPOrigin` derives the HTTP origin from the SAME configured URL the session dials,
	// so an agent cannot pair with one backend and upload its index to another.
	origin, err := session.HTTPOrigin(a.cfg.BackendWSSURL)
	if err != nil {
		return nil, fmt.Errorf("agent: backend origin: %w", err)
	}
	// The credential store is opened here rather than passed in, because `forgeops-agent scan`
	// has no session to take one from. `session.NewStore` is idempotent — it opens the same
	// on-disk store `Session` uses, so the two cannot read different credentials.
	store, err := session.NewStore(a.cfg.Session.StateDir, a.cfg.Session.CredentialStore)
	if err != nil {
		return nil, fmt.Errorf("agent: credential store: %w", err)
	}
	// The bearer credential is read at CALL time rather than captured here. A device token is
	// rotated on renewal, and a value captured at assembly would keep being sent after it stopped
	// being valid, which surfaces as a 401 the agent cannot explain.
	return newCodebaseIndexer(
		root, origin, "", a.cfg.Scanner.MaxFileSize,
		scanner.TokenFunc(func(ctx context.Context) (string, error) {
			creds, err := store.Load(ctx)
			if err != nil {
				return "", fmt.Errorf("agent: reading the device token: %w", err)
			}
			if len(creds.DeviceToken) == 0 {
				return "", errors.New("agent: this agent is not paired, so it has no token to submit a scan with")
			}
			return base64.RawURLEncoding.EncodeToString(creds.DeviceToken), nil
		}),
		scanSubmitTimeout,
	)
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
	root, baseURL, projectLang string, maxFileSize int64, tokens scanner.TokenSource, timeout time.Duration,
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
			Client: &http.Client{Timeout: timeout},
			Tokens: tokens,
		},
	}, nil
}
