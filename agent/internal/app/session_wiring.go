// SPDX-License-Identifier: Apache-2.0

// This file assembles `Serve`'s collaborators (leaves 8.5 and 8.7, design §10.1–§10.6).
//
// WHAT WAS WRONG BEFORE IT EXISTED
// `App.Session()` built `session.Deps{Store, Logger, AgentVersion}` and nothing else, and
// `Deps.Identity`, `Deps.Verifier`, `Deps.Runner` and `Deps.Journal` were assembled ONLY in
// `session/serve_test.go`. So the production agent dialled and refused with
// `session: Serve needs an identity.Provider; pass Deps.Identity` — correctly, since every one
// of those refusals is a control rather than a defect. Nothing downstream of an approval could
// happen: the backend minted and signed the command, and no device was ever there to run it.
//
// WHY THE ASSEMBLY IS HERE
// The composition root is the only place allowed to know all four packages. `session` must not
// import `executor` (D-59: `session -> executor -> mutate -> envelope` closes a cycle), which is
// why `session.CommandRunner` is declared by `session` as a one-method interface and adapted
// here — `serve.go`'s own docstring says the adapter "belongs in the app wiring". Keeping it
// here is what lets `session` depend on a method instead of on a package.
package app

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"time"

	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/executor"
	"github.com/parag8487/ForgeOps/agent/internal/identity"
	"github.com/parag8487/ForgeOps/agent/internal/scanner"
	"github.com/parag8487/ForgeOps/agent/internal/session"
)

// workspaceRoot resolves AGENT_WORKSPACE_ROOT, defaulting to the process working directory.
//
// Resolved here rather than in `config.Load`, following StateDir's precedent: configuration that
// touches the filesystem cannot be loaded and validated without one. The container sets the
// value explicitly (`/workspace`, where the project is mounted), so this default is for a
// developer running the binary inside a checkout.
func workspaceRoot(configured string) (string, error) {
	if configured != "" {
		return configured, nil
	}
	cwd, err := os.Getwd()
	if err != nil {
		return "", fmt.Errorf(
			"agent: AGENT_WORKSPACE_ROOT is unset and the working directory is unreadable: %w", err)
	}
	return cwd, nil
}

// replayNonceCapacity bounds the agent's nonce set (§7.6's uniqueness condition).
//
// Sized against the rate the backend can mint at, not guessed. Every envelope carries a
// strictly increasing per-device `seq` allocated by a single durable compare-and-set
// (`UPDATE agent_devices SET last_seq = :seq WHERE id = :id AND last_seq < :seq`), and commands
// are executed serially by one worker, so an agent cannot see thousands of distinct envelopes
// inside one `ENVELOPE_MAX_AGE_SECONDS` window. 8192 is far above that and still small enough
// to be irrelevant in memory.
//
// The bound must exceed what maxAge can contain, because a nonce evicted while its envelope is
// still fresh is a nonce that can be replayed — `NewMemoryReplayGuard` documents that the other
// way round, and this is the caller honouring it rather than trusting a default.
const replayNonceCapacity = 8192

// commandRunner adapts `executor.Dispatcher` onto `session.CommandRunner`.
//
// A type rather than a closure so the two shapes that cross the boundary — the progress
// callback and the outcome — are converted in one named place. `executor` reports progress
// through a `ProgressSink` interface and the session wants a `func(session.Progress)`; both
// describe the same `command.progress` frame, and this is the only translation between them.
type commandRunner struct{ dispatcher executor.Dispatcher }

// Execute implements session.CommandRunner.
//
// The `*envelope.Verified` is passed straight through, unexamined. That is the boundary D-45
// built: only `envelope.Verify` can construct one, so a command that reaches an operation has
// necessarily had its signature, freshness, ordering, uniqueness and policy binding checked.
// This adapter deliberately adds no check of its own — the authorisation half (§7.7's
// `approval_id` requirement and the closed operation catalogue) belongs to the dispatcher,
// which is the only layer that knows which operations mutate.
func (r commandRunner) Execute(
	ctx context.Context,
	v *envelope.Verified,
	progress func(session.Progress),
) (session.CommandOutcome, error) {
	sink := executor.SinkFunc(func(percent int, stage, message string) {
		if progress == nil {
			return
		}
		progress(session.Progress{Percent: percent, Stage: stage, Message: message})
	})

	result, err := r.dispatcher.Execute(ctx, v, sink)
	if err != nil {
		// Returned unwrapped so `session` can classify it. The executor's typed errors
		// (ErrUnknownOperation, ErrApprovalRequired, ErrUnimplemented) are what distinguish
		// "this agent cannot do that" from "the apply failed and was rolled back", and
		// wrapping them in an adapter-shaped message would erase the distinction.
		return session.CommandOutcome{}, err
	}
	return session.CommandOutcome{
		Status:         result.Status,
		Output:         result.Output,
		BackupManifest: result.BackupManifest,
		Hashes:         result.Hashes,
	}, nil
}

// identityProvider builds the §10.2 provider the mTLS dial needs.
//
// D-36 splits this two ways on purpose: `PairedDevice` for the laptop, where a pairing code is
// the only thing available, and a SPIFFE workload for the cluster. Only the first is built.
// `identity.SPIFFEIdentityProvider` exists but is an ID *formatter* — it has no `ClientTLS`,
// `Identity` or `RenewBefore` and therefore does not satisfy `identity.Provider` — so selecting
// it here would not compile, and pretending otherwise by falling back to the paired device
// would hand a cluster workload a laptop credential.
//
// So an operator who asks for `spiffe_workload` is told it is not built. That is §14.3's
// instruction applied literally: state the gap rather than pretend a pairing code is
// attestation.
func identityProvider(provider string, source identity.CredentialSource, renewBefore time.Duration) (identity.Provider, error) {
	switch provider {
	case "", "paired_device":
		return identity.NewPairedDevice(source, renewBefore), nil
	case "spiffe_workload":
		return nil, errors.New(
			"agent: AGENT_IDENTITY_PROVIDER=spiffe_workload is not implemented; " +
				"the SVID-backed provider arrives with the cluster path (§14.3, D-36)")
	default:
		return nil, fmt.Errorf("agent: unknown AGENT_IDENTITY_PROVIDER %q", provider)
	}
}

// buildSessionDeps assembles everything `Serve` needs, refusing to return a partial set.
//
// Every collaborator is required here even though `Deps` tolerates nil for most of them,
// because a nil one does not degrade gracefully — it refuses. A nil Verifier refuses every
// frame, a nil Runner answers `operation-unknown`, a nil Bundle refuses every mutation. Those
// refusals are right as last-resort defences and wrong as a deployed state, and the difference
// between the two is whether construction was allowed to succeed without them.
func (a *App) buildSessionDeps(store *session.FileStore) (session.Deps, error) {
	var zero session.Deps

	provider, err := identityProvider(
		a.cfg.Identity.Provider, store, a.cfg.Identity.CertRenewBefore)
	if err != nil {
		return zero, err
	}

	// The bundle view is ONE object serving two interfaces: `session.BundleState` for the
	// mutation gate and `envelope.BundleDigestSource` for Q-07's binding check. Two objects
	// would be two answers to "which bundle does this agent hold?", and a disagreement between
	// them would present as an intermittent policy failure.
	bundle, err := session.NewCredentialBundleState(store)
	if err != nil {
		return zero, err
	}

	keys, err := session.NewCredentialKeySource(store)
	if err != nil {
		return zero, err
	}

	// §7.6's bounded LRU, in process and not persisted — and that is the design's decision
	// rather than an omission here. §7.6 makes the agent's copy a bounded LRU and the backend's
	// state authoritative; D-41 rejects a persisted nonce set specifically because it would be
	// state an offline agent could be tricked into treating as authority.
	//
	// The residual exposure, stated rather than left to be discovered: a restart resets the
	// `seq` high-water mark, so an envelope captured less than `ENVELOPE_MAX_AGE_SECONDS`
	// before it could pass the ordering check once. It still has to pass FRESHNESS, which is
	// independent and bounds that window to those same 300 seconds, and it has to have been
	// captured off an mTLS-authenticated WSS session in the first place. The minting side does
	// not reset: `agent_devices.last_seq` is a durable Postgres compare-and-set, so no
	// legitimate envelope is ever re-issued with a seq the agent has already seen.
	guard, err := envelope.NewMemoryReplayGuard(a.cfg.Session.EnvelopeMaxAge, replayNonceCapacity)
	if err != nil {
		return zero, fmt.Errorf("agent: replay guard: %w", err)
	}

	verifier, err := envelope.NewVerifier(keys, guard, bundle,
		envelope.WithMaxAge(a.cfg.Session.EnvelopeMaxAge),
		envelope.WithClockSkew(a.cfg.Session.EnvelopeClockSkew),
	)
	if err != nil {
		return zero, fmt.Errorf("agent: envelope verifier: %w", err)
	}

	root, err := workspaceRoot(a.cfg.Executor.WorkspaceRoot)
	if err != nil {
		return zero, err
	}

	// The codebase indexer, which is what makes `scan.full` and `scan.incremental` real rather
	// than a named refusal (phases.md §1.3). It reads the workspace and POSTs the report to the
	// SAME backend the session dials — `session.HTTPOrigin` derives both from the one configured
	// URL, so an agent cannot pair with one backend and upload its index to another.
	//
	// The bearer credential is read at CALL time from the credential store rather than captured
	// here. A device token is rotated on renewal, and a value captured at assembly would keep
	// being sent after it stopped being valid, which surfaces as a 401 the agent cannot explain.
	origin, err := session.HTTPOrigin(a.cfg.BackendWSSURL)
	if err != nil {
		return zero, fmt.Errorf("agent: backend origin: %w", err)
	}
	indexer, err := newCodebaseIndexer(
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
	if err != nil {
		return zero, fmt.Errorf("agent: codebase indexer: %w", err)
	}

	dispatcher, err := executor.New(executor.Deps{Root: root, Indexer: indexer})
	if err != nil {
		return zero, fmt.Errorf("agent: executor: %w", err)
	}

	// D-41's outbound queue. Wired even when `AGENT_JOURNAL_MAX_BYTES=0` disables it: the
	// journal itself answers that configuration with ErrJournalDisabled, so appends fail
	// loudly. Passing nil instead would make the drain silently do nothing, which is the same
	// observable state for two different operator intentions.
	journal, err := session.NewJournal(
		a.cfg.Session.StateDir, a.cfg.Journal.MaxBytes, a.cfg.Journal.MaxAge)
	if err != nil {
		return zero, fmt.Errorf("agent: journal: %w", err)
	}

	a.logger.Debug("session collaborators assembled",
		zap.String("identity", a.cfg.Identity.Provider),
		zap.String("workspace_root", a.cfg.Executor.WorkspaceRoot),
		zap.String("journal", journal.Path()),
		zap.String("credential_store", store.Backend()))

	return session.Deps{
		Store:        store,
		Logger:       a.logger.Named("session"),
		AgentVersion: a.bi.Version,
		Identity:     provider,
		Verifier:     verifier,
		Runner:       commandRunner{dispatcher: dispatcher},
		Journal:      journal,
		Bundle:       bundle,
		Capabilities: agentCapabilities(dispatcher),
	}, nil
}

// agentCapabilities reports what this build can actually do, derived from the dispatch table.
//
// Derived and not listed. `Operations()` is itself derived from `handlerTable`, so a capability
// advertised in `session.connect` cannot disagree with what dispatch will do — a hand-written
// list is journal pattern H, and the failure mode is a backend that routes a command to an
// agent which then answers `operation-unknown`.
//
// Only IMPLEMENTED operations are advertised. The catalogue contains rows whose bodies are
// `unimplemented(...)` placeholders for later groups; advertising those would be claiming a
// capability this binary does not have.
func agentCapabilities(dispatcher executor.Dispatcher) []string {
	var out []string
	for _, info := range dispatcher.Operations() {
		if info.Implemented {
			out = append(out, string(info.Operation))
		}
	}
	return out
}
