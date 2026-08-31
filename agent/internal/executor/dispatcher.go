// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/executor/internal/mutate"
)

// ProgressSink receives `command.progress` (§10.5). One method, because the session's job is to
// forward it and a wider interface would invite an operation to report something else.
type ProgressSink interface {
	Progress(percent int, stage, message string)
}

// SinkFunc adapts a function to ProgressSink.
type SinkFunc func(percent int, stage, message string)

// Progress implements ProgressSink.
func (f SinkFunc) Progress(percent int, stage, message string) {
	if f != nil {
		f(percent, stage, message)
	}
}

// Result is what the session marshals into `command.result` (§7.3, §10.5).
type Result struct {
	Status string `json:"status"`
	Output string `json:"output"`
	// BackupManifest is the rollback handle the backend persists. Present only for an apply.
	BackupManifest json.RawMessage `json:"backup_manifest,omitempty"`
	// Hashes are the post-image hashes of what was written, by relative path.
	Hashes map[string]string `json:"hashes,omitempty"`
}

// Dispatcher runs one verified command.
type Dispatcher interface {
	Execute(ctx context.Context, v *envelope.Verified, sink ProgressSink) (Result, error)
	Operations() []OperationInfo
}

// handler is one operation's body.
//
// It takes the verified envelope rather than the decoded args, because every mutating handler
// has to pass that value on to `mutate` — the compile-time boundary D-45 built means a handler
// physically cannot write a file without it.
type handler func(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error)

// entry is one row of the dispatch table: §7.7's three columns plus the body and the bound.
//
// §10.5 sketches `map[Operation]handler` with the metadata elsewhere. One map carrying all of it
// is a deliberate departure: two parallel maps keyed by the same enum are the shape journal
// pattern H warns about, and the failure mode is specific — an operation added to the handler map
// and forgotten in the mutating-flags map would be a mutation that skips the approval check.
type entry struct {
	mutating         bool
	requiresApproval bool
	timeout          time.Duration
	implemented      bool
	run              handler
}

// The per-operation timeouts. Grouped as constants so the numbers are reviewable together
// rather than scattered through the table.
const (
	// A write is bounded tightly: it is local I/O over a change set the backend already sized.
	timeoutWrite = 2 * time.Minute
	// A read-only traversal of a large repository is the slow one.
	timeoutScan = 15 * time.Minute
	// A validator shells out to a pinned binary; §14's harness bounds it further.
	timeoutValidate = 5 * time.Minute
	// A network operation against a forge.
	timeoutNetwork = 3 * time.Minute
	// Bookkeeping.
	timeoutQuick = 30 * time.Second
)

// handlerTable is the ONLY dispatch surface (§10.5).
//
// `TestNoHandlerIsReachableOutsideTheTable` parses this package's own source and asserts that
// each handler function is referenced exactly once outside its declaration, inside this literal.
// That is what makes "the only dispatch surface" a checked property rather than a claim: a second
// call site — a convenience wrapper, a retry helper, an "internal" fast path — would fail the
// test rather than quietly become a second way in that skips the approval and timeout rules.
var handlerTable = map[Operation]entry{
	OpProjectRegister:   {timeout: timeoutQuick, run: unimplemented("group 12's workspace registry")},
	OpProjectUnregister: {timeout: timeoutQuick, run: unimplemented("group 12's workspace registry")},

	// `implemented` is deliberately absent here and computed in `Operations` instead: these two
	// are implemented in this package but need a `CodebaseIndexer` supplied by the app layer, so
	// whether the agent can actually scan is a property of the wiring rather than of the table.
	// Advertising them unconditionally would have the backend send a command the agent then
	// refuses, and §7.4 uses the advertised capability set to decide what to send.
	OpScanFull:        {timeout: timeoutScan, run: scanFull},
	OpScanIncremental: {timeout: timeoutScan, run: scanIncremental},

	// The six validators (FR-27). Read-only, so none is `mutating` and none requires an approval:
	// an approval gate on a read would mean a user has to approve finding out whether the artifact
	// they were offered is broken.
	//
	// Every one of these was `unimplemented("group 14's validators")` while Phase 1's criterion
	// "Generated files pass validation pipeline" was ticked — and separately, the `validator` package
	// they would have called was substring matching that returned nil for anything containing
	// `apiVersion:`. So the criterion was green in two incompatible ways at once: the dispatcher said
	// "not built" and the validator said "fine". Each now shells out to the real pinned tool and
	// reports its exit status, its findings and its version.
	OpValidateCompose: {timeout: timeoutValidate, implemented: true, run: validateCompose},
	OpValidateK8s:     {timeout: timeoutValidate, implemented: true, run: validateK8s},
	OpValidateTofu:    {timeout: timeoutValidate, implemented: true, run: validateTofu},
	OpValidateHelm:    {timeout: timeoutValidate, implemented: true, run: validateHelm},
	OpValidateYAML:    {timeout: timeoutValidate, implemented: true, run: validateYAML},
	OpValidateTrivy:   {timeout: timeoutValidate, implemented: true, run: validateTrivy},

	// Also read-only. `readiness.inventory` answers what a readiness score is computed from without
	// paying for a full index rebuild; `secretscan.run` is FR-42, and reports findings by kind, path,
	// line and fingerprint while deliberately never carrying the matched value.
	OpReadinessInventory: {timeout: timeoutScan, implemented: true, run: readinessInventory},
	OpSecretScanRun:      {timeout: timeoutScan, implemented: true, run: secretScanRun},

	OpChangeSetApply: {
		mutating: true, requiresApproval: true, timeout: timeoutWrite, implemented: true,
		run: applyChangeSet,
	},
	OpChangeSetRevert: {
		mutating: true, requiresApproval: true, timeout: timeoutWrite, implemented: true,
		run: revertChangeSet,
	},
	OpGitBranchCommitPush: {
		mutating: true, requiresApproval: true, timeout: timeoutNetwork,
		run: unimplemented("the git operations, which wrap Phase 0's client unchanged"),
	},
	OpGitOpenPR: {
		mutating: true, requiresApproval: true, timeout: timeoutNetwork,
		run: unimplemented("the git operations, which wrap Phase 0's client unchanged"),
	},
	// Mutating and approval-required despite writing no byte: it changes what a later deployment does,
	// which is the thing an approver is being asked about. Classifying it as a read because it happens
	// not to call os.WriteFile would let a production environment change through without a human.
	OpSecretsInject: {
		mutating: true, requiresApproval: true, timeout: timeoutQuick, implemented: true,
		run: secretsInject,
	},
}

// unimplemented builds the body of a catalogued operation whose implementation arrives later.
//
// A named refusal rather than a missing key. A missing key would report `operation-unknown` for
// an operation §7.7 does contain, which is a different and misleading fact (D-85); and a nil
// handler would panic on the first command that reached it.
func unimplemented(owner string) handler {
	return func(_ context.Context, _ *dispatcher, _ *envelope.Verified, _ ProgressSink) (Result, error) {
		return Result{}, fmt.Errorf("%w: it arrives with %s", ErrUnimplemented, owner)
	}
}

// Deps is what the dispatcher needs. A struct, so a later group adding a scanner or a validator
// registry does not change every construction site.
type Deps struct {
	// Root is the workspace root every path is resolved against and confined to.
	Root string
	// Clock is time.Now unless a test replaces it.
	Clock func() time.Time
	// Indexer builds and submits the codebase index. Optional: an agent wired without one
	// advertises `scan.full` and `scan.incremental` as unimplemented and refuses them by name,
	// which is the honest report for a build that cannot reach a backend to submit to.
	Indexer CodebaseIndexer
}

type dispatcher struct {
	root    string
	now     func() time.Time
	indexer CodebaseIndexer
	// secrets holds deploy-time injected values in memory, never on disk (FR-45).
	secrets *secretEnvironment
}

// New builds a Dispatcher and refuses to build an unusable one.
func New(deps Deps) (Dispatcher, error) {
	if deps.Root == "" {
		return nil, errors.New("executor: a workspace Root is required")
	}
	clock := deps.Clock
	if clock == nil {
		clock = time.Now
	}
	return &dispatcher{root: deps.Root, now: clock, indexer: deps.Indexer, secrets: newSecretEnvironment()}, nil
}

// Execute runs one verified command (§10.5).
//
// The order of the guards is the leaf's substance. Authority, then catalogue membership, then
// §7.7's approval requirement, and only then the body — so an operation that mutates cannot be
// reached without an approval id, and no argument is even decoded for a command that is going to
// be refused.
func (d *dispatcher) Execute(ctx context.Context, v *envelope.Verified, sink ProgressSink) (Result, error) {
	if v == nil {
		return Result{}, ErrNoAuthority
	}
	op := v.Operation()
	row, known := handlerTable[Operation(op)]
	if !known {
		return Result{}, fmt.Errorf("%w: %q", ErrUnknownOperation, op)
	}
	if row.requiresApproval && v.ApprovalID() == "" {
		return Result{}, fmt.Errorf("%w: %q", ErrApprovalRequired, op)
	}
	if sink == nil {
		// A nil sink is a caller that does not want progress, not an error. Replaced rather
		// than nil-checked at every emission point, because a handler that has to remember to
		// check is a handler that will forget.
		sink = SinkFunc(nil)
	}

	ctx, cancel := context.WithTimeout(ctx, row.timeout)
	defer cancel()

	return row.run(ctx, d, v, sink)
}

// Operations reports the catalogue, derived from the dispatch table.
//
// Derived rather than listed, so `agent doctor` cannot disagree with what dispatch will actually
// do — journal pattern H is two copies of one fact, and a hand-maintained report of a table is
// exactly that.
func (d *dispatcher) Operations() []OperationInfo {
	out := make([]OperationInfo, 0, len(handlerTable))
	for _, op := range allOperations {
		row, ok := handlerTable[op]
		if !ok {
			continue
		}
		out = append(out, OperationInfo{
			Operation:        op,
			Mutating:         row.mutating,
			RequiresApproval: row.requiresApproval,
			Timeout:          row.timeout,
			Implemented:      d.implemented(op, row),
		})
	}
	return out
}

// implemented answers whether THIS dispatcher can run an operation, not merely whether the table
// has a body for it.
//
// The distinction exists because the two scan operations need a collaborator the app layer
// supplies. A table-only answer would advertise a capability the agent then refuses, and §7.4 has
// the backend choose what to send from the advertised set — so the disagreement would surface as a
// failed command rather than as a command never sent.
func (d *dispatcher) implemented(op Operation, row entry) bool {
	switch op {
	case OpScanFull, OpScanIncremental:
		return d.indexer != nil
	default:
		return row.implemented
	}
}

// ── changeset.apply ────────────────────────────────────────────────────────────────────────

// applyArgs is `changeset.apply`'s argument object.
//
// `Root` is deliberately absent: the workspace root comes from the agent's own configuration,
// not from the envelope. A root in the args would let a signed command relocate the write
// boundary, which is the one thing `mutate`'s confinement exists to prevent — the signature
// proves who sent it, not that where it points is inside the workspace.
type applyArgs struct {
	Entries []applyEntry `json:"entries"`
}

type applyEntry struct {
	Path         string `json:"path"`
	Action       string `json:"action"`
	Content      string `json:"content"`
	ExpectedHash string `json:"expected_hash"`
	Mode         uint32 `json:"mode"`
}

func applyChangeSet(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args applyArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: %v", ErrBadArgs, err)
	}
	if len(args.Entries) == 0 {
		return Result{}, fmt.Errorf("%w: a change set with no entries", ErrBadArgs)
	}

	entries := make([]mutate.Entry, 0, len(args.Entries))
	for i, raw := range args.Entries {
		action, err := parseAction(raw.Action)
		if err != nil {
			return Result{}, fmt.Errorf("%w: entry %d: %v", ErrBadArgs, i, err)
		}
		entry := mutate.Entry{
			RelPath:      raw.Path,
			Action:       action,
			ExpectedHash: raw.ExpectedHash,
			Mode:         os.FileMode(raw.Mode),
		}
		if action != mutate.Delete {
			// The content is carried as a JSON string, so it is UTF-8 by construction. A
			// base64 member would allow arbitrary bytes and is not needed: every artefact this
			// platform generates is text (§1.2's Compose, Kubernetes, Terraform and CI files).
			entry.Content = []byte(raw.Content)
		}
		entries = append(entries, entry)
	}

	sink.Progress(0, "apply", fmt.Sprintf("applying %d entr(ies)", len(entries)))
	report, err := mutate.ApplyVerified(ctx, v, d.root, entries)
	if err != nil {
		// No progress emission on the failure path. `mutate` has already rolled every write
		// back, so a "50%" left on an SSE stream would describe a state that no longer exists.
		return Result{}, err
	}
	sink.Progress(100, "apply", fmt.Sprintf("wrote %d file(s)", len(report.Written)))

	manifest, err := json.Marshal(report.Backups)
	if err != nil {
		return Result{}, fmt.Errorf("executor: the backup manifest could not be marshalled: %w", err)
	}
	hashes := make(map[string]string, len(report.Written))
	for _, written := range report.Written {
		hashes[written.RelPath] = written.NewHash
	}
	return Result{
		Status:         "succeeded",
		Output:         fmt.Sprintf("applied %d entr(ies) in %s", len(report.Written), report.Duration.Round(time.Millisecond)),
		BackupManifest: manifest,
		Hashes:         hashes,
	}, nil
}

func parseAction(raw string) (mutate.Action, error) {
	switch mutate.Action(raw) {
	case mutate.Create:
		return mutate.Create, nil
	case mutate.Update:
		return mutate.Update, nil
	case mutate.Delete:
		return mutate.Delete, nil
	default:
		return "", fmt.Errorf("action %q is not create, update or delete", raw)
	}
}

// ── changeset.revert ───────────────────────────────────────────────────────────────────────

type revertArgs struct {
	// Manifest is the handle `changeset.apply` returned, round-tripped through the backend.
	//
	// It is passed back rather than looked up locally because the agent keeps no index of
	// manifests: D-41 forbids persisting anything that authorises a mutation, and a local
	// registry of rollback handles would be a store of exactly that. `mutate.Revert` verifies
	// every backup it names against its recorded hash, so a tampered manifest is refused
	// rather than trusted.
	Manifest json.RawMessage `json:"backup_manifest"`
}

func revertChangeSet(ctx context.Context, _ *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args revertArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: %v", ErrBadArgs, err)
	}
	if len(args.Manifest) == 0 || string(args.Manifest) == "null" {
		// `null` and absent are the same fact and different bytes: `{"backup_manifest":null}`
		// decodes to a four-byte RawMessage, so a length check alone would let it through to
		// `mutate`, which would refuse it as an inconsistent entry — a true error naming the
		// wrong layer.
		return Result{}, fmt.Errorf("%w: a revert with no backup_manifest", ErrBadArgs)
	}
	var manifest mutate.BackupManifest
	decoder := json.NewDecoder(bytes.NewReader(args.Manifest))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return Result{}, fmt.Errorf("%w: backup_manifest: %v", ErrBadArgs, err)
	}

	sink.Progress(0, "revert", fmt.Sprintf("reverting %d entr(ies)", len(manifest.Entries)))
	report, err := mutate.Revert(ctx, v, manifest)
	if err != nil {
		return Result{}, err
	}
	sink.Progress(100, "revert", fmt.Sprintf("restored %d, removed %d", len(report.Restored), len(report.Removed)))
	return Result{
		Status: "succeeded",
		Output: fmt.Sprintf("restored %d file(s) and removed %d in %s",
			len(report.Restored), len(report.Removed), report.Duration.Round(time.Millisecond)),
	}, nil
}
