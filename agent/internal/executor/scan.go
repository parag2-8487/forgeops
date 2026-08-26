// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
)

// CodebaseIndexer builds a scan report for a workspace and submits it to the backend.
//
// AN INTERFACE HERE, NOT AN IMPORT OF `scanner`. `executor` depends on two methods, and the app
// layer supplies the concrete `scanner.ReportScanner` plus its HTTP submitter ??? the same shape
// `session.CommandRunner` uses to reach this package. Importing `scanner` directly would put the
// tree-sitter grammars, the redactor and an HTTP client behind every `executor` test binary, and
// would make the dispatcher's own tests depend on a scanner they do not exercise.
//
// The report is built AND submitted behind this one call rather than returned for the dispatcher
// to forward. A report is up to a megabyte per file across a whole repository; handing it back
// through `Result.Output` would put the entire index into a `command.result` frame, and ??7.3 sizes
// that frame for a status, not for a payload.
type CodebaseIndexer interface {
	// IndexFull scans the whole workspace and replaces the project's index.
	IndexFull(ctx context.Context, projectID string) (IndexSummary, error)
	// IndexChanged rescans only `changed` and merges the result, following the dependency
	// closure so an importer whose import changed is re-parsed too (Q-10).
	IndexChanged(ctx context.Context, projectID string, changed []string) (IndexSummary, error)
}

// IndexSummary is what a scan reports back through `command.result`.
//
// Counts and a hash, never the content. `RedactionCount` is included because an operator needs to
// see that redaction ran at all: `file_contents` is a redacted-only store (??6.3, ??7.11), and a
// zero here on a repository that does contain credentials is the signal that something is wrong
// with the scanner rather than with the repository.
type IndexSummary struct {
	FilesIndexed   int    `json:"files_indexed"`
	ChunksIndexed  int    `json:"chunks_indexed"`
	Dependencies   int    `json:"dependencies"`
	RedactionCount int    `json:"redaction_count"`
	InventoryHash  string `json:"inventory_hash"`
	// VectorsAbsentReason is non-empty when the tree and contents were stored but no vectors
	// were: the honest outcome when no embedding provider is configured. Retrieval is
	// sparse-only in that state, and saying so is the difference between a degraded index and
	// one an operator believes is complete.
	VectorsAbsentReason string `json:"vectors_absent_reason,omitempty"`
}

// ErrNoIndexer is the refusal when a scan is asked of an agent with no indexer wired.
//
// A NAMED REFUSAL RATHER THAN A DEGRADED SCAN. The alternative ??? returning an empty report ??? would
// have the backend prune the project's whole index and record the result as a successful scan,
// which is worse than not scanning: the operator would see an empty index and no error.
// `Operations` advertises these two as unimplemented in the same state, so a backend that reads
// capabilities never sends the command at all; this guard is for the case where it does anyway.
var ErrNoIndexer = errors.New("executor: no codebase indexer is wired")

// scanArgs is the argument object for both scan operations.
//
// `Root` is absent for the same reason `applyArgs` omits it: the workspace comes from the agent's
// configuration, and a root in a signed envelope would let the sender relocate what gets read.
// `ProjectID` is required because the index is per project and the agent may hold several.
type scanArgs struct {
	ProjectID string `json:"project_id"`
	// Paths is the changed set for an incremental rescan. Ignored by `scan.full`.
	Paths []string `json:"paths,omitempty"`
}

func decodeScanArgs(v *envelope.Verified) (scanArgs, error) {
	var args scanArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return scanArgs{}, fmt.Errorf("executor: undecodable scan arguments: %w", err)
	}
	if args.ProjectID == "" {
		return scanArgs{}, errors.New("executor: a scan needs a project_id to index against")
	}
	return args, nil
}

func summaryResult(summary IndexSummary) (Result, error) {
	// The summary travels as the Output string rather than a new Result field: every other
	// operation reports through the same two columns, and a field only one operation ever sets
	// would have to be explained at every other call site.
	encoded, err := json.Marshal(summary)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable scan summary: %w", err)
	}
	return Result{Status: "succeeded", Output: string(encoded)}, nil
}

// scanFull is `scan.full` (??7.7, phases.md ??1.3).
func scanFull(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	if d.indexer == nil {
		return Result{}, ErrNoIndexer
	}
	args, err := decodeScanArgs(v)
	if err != nil {
		return Result{}, err
	}
	// Progress before the work, not after: a full scan of a large repository runs for minutes
	// under `timeoutScan`, and a session with no frame in that window looks hung. `Execute`
	// has already substituted a non-nil sink, so this needs no guard.
	sink.Progress(0, "scan", "scanning the workspace")
	summary, err := d.indexer.IndexFull(ctx, args.ProjectID)
	if err != nil {
		return Result{}, fmt.Errorf("executor: full scan failed: %w", err)
	}
	sink.Progress(100, "scan", fmt.Sprintf("indexed %d file(s), %d chunk(s)", summary.FilesIndexed, summary.ChunksIndexed))
	return summaryResult(summary)
}

// scanIncremental is `scan.incremental` ??? the watch-mode rescan.
func scanIncremental(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	if d.indexer == nil {
		return Result{}, ErrNoIndexer
	}
	args, err := decodeScanArgs(v)
	if err != nil {
		return Result{}, err
	}
	// An incremental rescan with no paths is refused rather than treated as a full scan. The
	// report carries a `Partial` flag that tells the backend whether to prune paths it did not
	// see; an empty partial report would prune nothing and index nothing, so the command would
	// succeed having done nothing at all, and the caller would have no way to tell.
	if len(args.Paths) == 0 {
		return Result{}, errors.New("executor: an incremental scan needs at least one changed path")
	}
	sink.Progress(0, "scan", fmt.Sprintf("rescanning %d changed path(s)", len(args.Paths)))
	summary, err := d.indexer.IndexChanged(ctx, args.ProjectID, args.Paths)
	if err != nil {
		return Result{}, fmt.Errorf("executor: incremental scan failed: %w", err)
	}
	sink.Progress(100, "scan", fmt.Sprintf("reindexed %d file(s)", summary.FilesIndexed))
	return summaryResult(summary)
}
