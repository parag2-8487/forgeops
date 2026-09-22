// SPDX-License-Identifier: Apache-2.0

package executor

// `argocd.app_action` — Phase 2 §2.7's sync, as a governed operation.
//
// ONE AUTHORITY OVER AN ARGOCD APPLICATION, with a closed set of verbs. The same judgement the Docker and
// Kubernetes actions make: "may act on this Application" is one permission, and minting `argocd.sync`,
// `argocd.refresh` and `argocd.wait` separately would be three whitelist entries, three policy resources and
// three approval rows describing it.
//
// WHAT IS **NOT** IN THE VERB SET, and this is the part worth reading:
//
//   - `delete`. Deleting an Application cascades through the resources finalizer to everything it deployed.
//     That is a fleet-scale delete behind a single verb, and it is not reachable from this product at all.
//   - `rollback`. `argocd app rollback` deploys a previous revision WITHOUT changing Git, so the cluster and
//     the repository disagree and the next sync undoes it. §2.3's rollback is a deployment of the previous
//     manifests through the chokepoint, which leaves both in agreement.
//   - anything with `--force`. `argocd app sync --force` replaces resources rather than patching them,
//     which deletes and recreates a StatefulSet — losing its volumes on some storage classes.
//
// A SYNC IS MUTATING AND APPROVAL-REQUIRED. It applies whatever Git currently holds to a live cluster; the
// fact that a human wrote it in a repository earlier is not an approval of applying it now.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

var (
	// ErrUnknownArgoAction refuses a verb outside the closed set by name.
	ErrUnknownArgoAction = errors.New("executor: that ArgoCD action is not in the closed set")
	// ErrArgoAppRequired refuses an action naming no Application.
	ErrArgoAppRequired = errors.New("executor: an ArgoCD action must name one Application")
)

// argoActions is the closed set. The value is the argument vector, so a verb cannot smuggle a flag: the
// arguments are assembled here from constants and the Application name, never from caller-supplied text.
var argoActions = map[string][]string{
	// `--prune=false` is EXPLICIT rather than relying on the default. The default is already false, and
	// stating it means a future argocd release that changes the default cannot quietly start deleting.
	"sync": {"app", "sync", "--prune=false", "--timeout", "600"},
	// A refresh re-reads Git and recomputes the diff. It changes nothing in the cluster, but it is here
	// rather than in a read operation because it makes the ArgoCD server do work on the operator's behalf
	// and an unbounded stream of refreshes is a denial of service against their control plane.
	"refresh": {"app", "get", "--refresh", "-o", "json"},
	// Waits for an in-flight sync to settle. Separate from `sync` because a caller sometimes wants to start
	// one and report immediately, and sometimes wants the outcome.
	"wait": {"app", "wait", "--timeout", "600"},
}

// argoAppActionArgs is one action against one named Application.
type argoAppActionArgs struct {
	Action string `json:"action"`
	App    string `json:"app"`
	// Optional. The ArgoCD server address; empty uses whatever the operator's argocd context holds, which
	// is the common case for a machine that has already logged in.
	Server string `json:"server"`
}

// ArgoActionReport is what a sync reports.
type ArgoActionReport struct {
	Action string `json:"action"`
	App    string `json:"app"`
	// SyncStatus and HealthStatus are READ BACK from the server after the action, not parsed out of the
	// action's own output. `argocd app sync` prints a summary of what it intended; `argocd app get` reports
	// what the server now holds, and the difference is the whole reason this field exists.
	SyncStatus   string `json:"sync_status"`
	HealthStatus string `json:"health_status"`
	// Revision is the git SHA now deployed. The one fact that makes a sync auditable: two syncs that both
	// report Synced at the same revision mean the second one changed nothing.
	Revision   string `json:"revision"`
	Output     string `json:"output"`
	ObservedAt string `json:"observed_at"`
}

// argoAppAction is the handler.
func argoAppAction(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args argoAppActionArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: argocd action arguments: %v", ErrBadArgs, err)
	}
	if strings.TrimSpace(args.App) == "" {
		return Result{}, ErrArgoAppRequired
	}
	vector, known := argoActions[args.Action]
	if !known {
		return Result{}, fmt.Errorf("%w: %q", ErrUnknownArgoAction, args.Action)
	}

	runner, err := argoRunner(ctx, d.root)
	if err != nil {
		return Result{}, err
	}

	invocation := append([]string{}, vector...)
	invocation = append(invocation, args.App)
	if server := strings.TrimSpace(args.Server); server != "" {
		invocation = append(invocation, "--server", server)
	}

	sink.Progress(30, string(OpArgoAppAction), fmt.Sprintf("%s %s", args.Action, args.App))
	outcome, runErr := runner.Run(ctx, "argocd", invocation...)

	report := ArgoActionReport{
		Action: args.Action, App: args.App,
		Output:     firstOf(tailOutput(outcome.Output)),
		ObservedAt: d.now().UTC().Format(time.RFC3339),
	}

	// THE STATE IS READ BACK EVEN WHEN THE ACTION FAILED, because a failed sync still moves things: a sync
	// that applied four of six manifests before erroring leaves the Application OutOfSync and Progressing,
	// and an operator needs to know that rather than only that the command exited non-zero.
	status, health, revision := argoAppStatus(ctx, runner, args.App, args.Server)
	report.SyncStatus = status
	report.HealthStatus = health
	report.Revision = revision

	encoded, marshalErr := json.Marshal(report)
	if marshalErr != nil {
		return Result{}, fmt.Errorf("executor: unencodable argocd report: %w", marshalErr)
	}

	if runErr != nil || !outcome.Passed {
		// A FAILED SYNC IS A RESULT, like a failing test suite and a failing build. ArgoCD's own diagnostic
		// is the answer to "why did this not sync", and an error would discard it.
		sink.Progress(100, string(OpArgoAppAction), fmt.Sprintf("%s failed", args.Action))
		return Result{Status: "failed", Output: string(encoded)}, nil
	}
	sink.Progress(100, string(OpArgoAppAction), fmt.Sprintf("%s %s", args.Action, args.App))
	return Result{Status: "applied", Output: string(encoded)}, nil
}

// argoAppStatus reads sync status, health and revision with `argocd app get -o json`.
//
// Empty strings when the server cannot be reached. Reported as absence rather than filled in: "unknown" and
// "Synced" must never be confused, and a panel that showed the latter for the former would tell an operator
// their cluster matched Git when nothing had checked.
func argoAppStatus(ctx context.Context, runner *validator.Runner, app string, server string) (
	syncStatus string, health string, revision string,
) {
	vector := []string{"app", "get", app, "-o", "json"}
	if trimmed := strings.TrimSpace(server); trimmed != "" {
		vector = append(vector, "--server", trimmed)
	}
	outcome, err := runner.Run(ctx, "argocd", vector...)
	if err != nil || !outcome.Passed {
		return "", "", ""
	}
	var document struct {
		Status struct {
			Sync struct {
				Status   string `json:"status"`
				Revision string `json:"revision"`
			} `json:"sync"`
			Health struct {
				Status string `json:"status"`
			} `json:"health"`
		} `json:"status"`
	}
	if decodeErr := decodeFirstJSON(outcome.Output, &document); decodeErr != nil {
		return "", "", ""
	}
	return document.Status.Sync.Status, document.Status.Health.Status, document.Status.Sync.Revision
}

// ErrArgocdMissing reports that the CLI is not installed.
var ErrArgocdMissing = errors.New("executor: argocd is not on PATH")

// argoRunner resolves the CLI and refuses early when it is absent.
//
// NO VERSION PROBE, unlike `dockerRunner`'s `docker version`. `argocd version` contacts the SERVER and fails
// when the operator is not logged in -- which is a legitimate state for a machine that only ever runs
// `argocd app get` against a context it holds a token for. Refusing there would refuse a working setup.
func argoRunner(ctx context.Context, dir string) (*validator.Runner, error) {
	runner := &validator.Runner{Dir: dir}
	if _, err := runner.Look("argocd"); err != nil {
		return nil, fmt.Errorf("%w: %w", ErrArgocdMissing, err)
	}
	return runner, nil
}
