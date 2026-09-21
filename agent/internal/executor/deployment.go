// SPDX-License-Identifier: Apache-2.0

// `deployment.apply_manifests`: apply Kubernetes manifests to a real cluster and VERIFY the result.
//
// §2.2's box reads "K8s manifest apply with health check verification", and the second half is the
// reason this is one operation rather than two. An apply that returns as soon as the API server accepts
// the objects has established that the YAML parsed — not that anything is running. Every deployment
// surface built on top of this (2.3's timeline, 2.7a's progressive delivery, 2.12's self-healing) decides
// what to do next from the answer, and "accepted" reported as "deployed" would make all three of them
// act on a deployment that never came up.
//
// So the operation is apply-then-wait, and its result distinguishes three outcomes that a caller must
// not confuse:
//
//	applied + healthy      every workload it waited on reached its desired replica count
//	applied + unhealthy    the objects are in the cluster and at least one workload did not converge
//	                       within the bound. NOT an error: the apply genuinely happened, and rolling it
//	                       back is a decision for the backend and the operator, not for this handler.
//	failed                 the apply itself was refused. Nothing to verify.
//
// THERE IS NO SHELL, like every other operation here. `kubectl` is invoked with an argument vector
// through the same `validator.Runner` the six FR-27 validators use, so there is no string for a manifest
// path or a namespace to be interpolated into.
//
// WHY `--prune` IS NOT OFFERED. Pruning deletes cluster objects that are absent from the manifest set,
// and it selects them by label. A wrong or absent label selector prunes whatever else happens to carry
// it, which is an unbounded delete driven by a field nobody reviewed. If reconciliation-style deletion is
// wanted it belongs in 2.7's GitOps deliverable, where the desired state is a repository and the diff is
// visible before it runs.
//
// WHAT THIS DOES NOT DO, stated because the surrounding boxes name them and a reader will look here:
// there is no image build or push (a separate operation, separate credentials, separate failure modes),
// and no OpenTofu apply (state locking makes it a different problem). Both are §2.2 boxes and neither is
// ticked.

package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

// MaxDeploymentManifests bounds one deployment's manifest set.
//
// DERIVED, not picked: the backend's blast-radius analyser blocks a file change set at 64 points, which
// four destructive file items reach, and the generation path caps one run's artifacts well below this.
// A deployment naming more than 32 manifests is not a deployment this platform produced, and refusing it
// by count is cheaper than discovering halfway through an apply that the envelope was assembled wrongly.
const MaxDeploymentManifests = 32

// DefaultHealthTimeout bounds the wait for each workload, when the envelope does not say.
//
// Two minutes because that is longer than an image pull of a cached layer set and shorter than a human's
// patience for a screen that says "deploying". The envelope may raise it; it may not remove it, because
// an unbounded wait would hold the operation's slot until the dispatcher's own timeout killed it and the
// result would be "the operation timed out" rather than "the workload did not become ready", which are
// different facts with different remedies.
const DefaultHealthTimeout = 2 * time.Minute

// MaxHealthTimeout is the ceiling the envelope cannot exceed. See `timeoutNetwork` on the dispatch row:
// a per-workload wait longer than the operation's own budget could never complete.
const MaxHealthTimeout = 10 * time.Minute

var (
	// ErrNoManifests refuses an empty set rather than reporting a successful deployment of nothing.
	// The honest failure, and the one a mis-assembled envelope produces.
	ErrNoManifests = errors.New("executor: a deployment names no manifests")
	// ErrTooManyManifests names the bound and the count.
	ErrTooManyManifests = errors.New("executor: a deployment names more manifests than the bound allows")
	// ErrKubectlMissing is reported as unimplemented-for-this-host rather than as a failed deployment:
	// nothing was attempted, and an operator needs to install a tool, not debug a cluster.
	ErrKubectlMissing = errors.New("executor: kubectl is not on PATH, so no deployment can be applied")
)

// deploymentArgs is what the signed envelope carries.
//
// Every path is WORKSPACE-RELATIVE and resolved through the same confinement `changeset.apply` uses, so
// a signed envelope cannot name `/etc` or climb out with `..`. The cluster context is a name, not a
// kubeconfig: the agent uses the operator's own kubeconfig, so a deployment can only reach a cluster the
// operator can already reach. An envelope that could supply credentials would let the backend widen the
// agent's reach beyond its host's, which is the property the whole mTLS-and-whitelist design protects.
type deploymentArgs struct {
	// DeploymentID ties the result back to the backend's `deployments` row.
	DeploymentID string `json:"deployment_id"`
	// Manifests are workspace-relative paths, applied in the order given.
	Manifests []string `json:"manifests"`
	// Context is the kubectl context to select. Empty means the operator's current context, which is
	// reported in the result so "which cluster did this go to" is never a guess.
	Context string `json:"context,omitempty"`
	// Namespace is passed to every invocation. Empty means the context's default namespace.
	Namespace string `json:"namespace,omitempty"`
	// HealthTimeoutSeconds bounds the wait per workload. Clamped to `MaxHealthTimeout`.
	HealthTimeoutSeconds int `json:"health_timeout_seconds,omitempty"`
	// EnvironmentName is carried for the result and the agent's log only, so an operator reading either
	// can see which environment a deployment belonged to without joining back to the backend.
	EnvironmentName string `json:"environment_name,omitempty"`
}

// WorkloadHealth is one workload's convergence, or the reason it is not known.
type WorkloadHealth struct {
	// Kind and Name as `kubectl rollout status` addresses them, e.g. `deployment/checkout-api`.
	Kind string `json:"kind"`
	Name string `json:"name"`
	// Ready is the question the whole operation exists to answer.
	Ready bool `json:"ready"`
	// Detail is `kubectl`'s own last line, trimmed. Its words, not a paraphrase: "waiting for
	// deployment rollout to finish: 1 of 3 updated replicas are available" tells an operator more than
	// any summary this code could write.
	Detail string `json:"detail"`
	// WaitedSeconds is how long this workload was given, so a timeout is distinguishable from a
	// failure that was immediate.
	WaitedSeconds int `json:"waited_seconds"`
}

// DeploymentReport is the operation's result and the row the backend records.
type DeploymentReport struct {
	DeploymentID string `json:"deployment_id"`
	// Applied lists what the API server accepted, as `kubectl` named it.
	Applied []string `json:"applied"`
	// Workloads is empty when the manifest set declares none — a Service-and-ConfigMap deployment is a
	// real deployment with nothing to wait for, and reporting it as unhealthy would be wrong.
	Workloads []WorkloadHealth `json:"workloads"`
	// Healthy is the AND over `Workloads`, and true for an empty set. Stated as its own field rather
	// than left for a caller to recompute, because three surfaces branch on it and three
	// recomputations would eventually disagree.
	Healthy bool `json:"healthy"`
	// ClusterContext is the context that was actually used, resolved when the envelope named none.
	ClusterContext  string `json:"cluster_context"`
	Namespace       string `json:"namespace,omitempty"`
	EnvironmentName string `json:"environment_name,omitempty"`
	// KubectlVersion, because a pass from an unknown version is not evidence — the same rule the
	// validators follow.
	KubectlVersion string `json:"kubectl_version"`
}

// workloadKinds are the kinds `kubectl rollout status` can wait on.
//
// A kind NOT in this set is applied and not waited for, which is the honest treatment: `rollout status`
// on a Service exits non-zero with "does not have a rollout status", and treating that as an unhealthy
// deployment would fail every manifest set that contains a Service.
var workloadKinds = map[string]struct{}{
	"deployment":  {},
	"statefulset": {},
	"daemonset":   {},
}

// parseAppliedResource reads `kubectl apply`'s own output line into a kind and a name.
//
// `kubectl` prints `deployment.apps/checkout-api created`. The kind is taken up to the first `.` or `/`
// so `deployment.apps` and `deployment` both resolve, and the name up to the first space. A line this
// cannot parse is returned with an empty kind and is therefore not waited on — the safe direction, since
// the alternative is inventing a workload name and asking the cluster about something that is not there.
func parseAppliedResource(line string) (kind, name string) {
	trimmed := strings.TrimSpace(line)
	slash := strings.Index(trimmed, "/")
	if slash <= 0 {
		return "", ""
	}
	kind = strings.ToLower(trimmed[:slash])
	if dot := strings.Index(kind, "."); dot > 0 {
		kind = kind[:dot]
	}
	rest := trimmed[slash+1:]
	if space := strings.IndexAny(rest, " \t"); space > 0 {
		rest = rest[:space]
	}
	return kind, strings.TrimSpace(rest)
}

// healthTimeout clamps the envelope's request into the allowed band.
func healthTimeout(seconds int) time.Duration {
	if seconds <= 0 {
		return DefaultHealthTimeout
	}
	requested := time.Duration(seconds) * time.Second
	if requested > MaxHealthTimeout {
		return MaxHealthTimeout
	}
	return requested
}

// applyManifests is the handler.
func applyManifests(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args deploymentArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("executor: deployment arguments could not be decoded: %w", err)
	}
	if len(args.Manifests) == 0 {
		return Result{}, ErrNoManifests
	}
	if len(args.Manifests) > MaxDeploymentManifests {
		return Result{}, fmt.Errorf("%w: %d named, %d allowed",
			ErrTooManyManifests, len(args.Manifests), MaxDeploymentManifests)
	}

	// CONFINEMENT FIRST, before any tool runs. Every path is resolved through the same helper the
	// validators use, so a signed envelope naming `../../etc` is refused before `kubectl` sees it.
	resolved := make([]string, 0, len(args.Manifests))
	for _, rel := range args.Manifests {
		abs, _, err := d.resolveTarget(rel)
		if err != nil {
			return Result{}, err
		}
		resolved = append(resolved, abs)
	}

	runner := &validator.Runner{Dir: filepath.Dir(resolved[0])}
	if _, err := runner.Look("kubectl"); err != nil {
		return Result{}, fmt.Errorf("%w: %w", ErrKubectlMissing, err)
	}

	base := []string{}
	if args.Context != "" {
		base = append(base, "--context", args.Context)
	}
	if args.Namespace != "" {
		base = append(base, "--namespace", args.Namespace)
	}

	report := DeploymentReport{
		DeploymentID:    args.DeploymentID,
		Namespace:       args.Namespace,
		EnvironmentName: args.EnvironmentName,
		ClusterContext:  args.Context,
		KubectlVersion:  runner.Version(ctx, "kubectl", "version", "--client=true", "-o", "yaml"),
	}
	if report.ClusterContext == "" {
		// RESOLVED, NOT LEFT BLANK. "which cluster did this go to" must never be answered by "whatever
		// was current at the time", because the answer changes when the operator switches context.
		if outcome, err := runner.Run(ctx, "kubectl", "config", "current-context"); err == nil {
			report.ClusterContext = strings.TrimSpace(outcome.Output)
		}
		if report.ClusterContext == "" {
			report.ClusterContext = "unknown (kubectl reported no current context)"
		}
	}

	sink.Progress(20, "deployment.apply_manifests",
		fmt.Sprintf("applying %d manifest(s) to %s", len(resolved), report.ClusterContext))

	for index, abs := range resolved {
		applyArgs := append(append([]string{"apply"}, base...), "-f", abs)
		outcome, err := runner.Run(ctx, "kubectl", applyArgs...)
		if err != nil || !outcome.Passed {
			// THE APPLY FAILED, AND PARTIAL WORK IS REPORTED. Earlier manifests in the set are already
			// in the cluster; saying so is what lets the backend decide between a revert and a retry.
			// Silently returning only the error would leave an operator with objects nobody mentioned.
			return Result{}, fmt.Errorf(
				"executor: kubectl apply refused %s (manifest %d of %d; %d already applied: %s): %w — %s",
				args.Manifests[index], index+1, len(resolved), len(report.Applied),
				strings.Join(report.Applied, ", "), errOrRefused(err), firstLine(outcome.Output))
		}
		for _, line := range strings.Split(outcome.Output, "\n") {
			if strings.TrimSpace(line) == "" {
				continue
			}
			report.Applied = append(report.Applied, strings.TrimSpace(line))
		}
	}

	// ── the half that makes this operation worth having ──
	wait := healthTimeout(args.HealthTimeoutSeconds)
	sink.Progress(60, "deployment.apply_manifests",
		fmt.Sprintf("verifying health, up to %s per workload", wait))

	report.Healthy = true
	for _, line := range report.Applied {
		kind, name := parseAppliedResource(line)
		if kind == "" || name == "" {
			continue
		}
		if _, waitable := workloadKinds[kind]; !waitable {
			continue
		}
		started := time.Now()
		statusArgs := append(append([]string{"rollout", "status"}, base...),
			kind+"/"+name, "--timeout", wait.String())
		outcome, err := runner.Run(ctx, "kubectl", statusArgs...)
		health := WorkloadHealth{
			Kind:          kind,
			Name:          name,
			Ready:         err == nil && outcome.Passed,
			WaitedSeconds: int(time.Since(started).Seconds()),
		}
		// KUBECTL'S OWN WORDS. "waiting for deployment rollout to finish: 1 of 3 updated replicas are
		// available" tells an operator what to look at; any summary written here would lose it.
		health.Detail = firstLine(outcome.Output)
		if !health.Ready {
			if detail := firstLine(outcome.Output); detail != "" {
				health.Detail = detail
			}
			if health.Detail == "" {
				health.Detail = "kubectl reported no reason"
			}
			report.Healthy = false
		}
		report.Workloads = append(report.Workloads, health)
	}

	verdict := "healthy"
	if !report.Healthy {
		verdict = "applied but NOT healthy"
	}
	sink.Progress(100, "deployment.apply_manifests",
		fmt.Sprintf("%d resource(s) applied, %d workload(s) verified: %s",
			len(report.Applied), len(report.Workloads), verdict))

	// UNHEALTHY IS A RESULT, NOT AN ERROR. The apply happened; the cluster is in a state the backend
	// must record and may choose to roll back. Returning an error here would lose the report — and with
	// it which objects landed and which workload failed to converge.
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable deployment report: %w", err)
	}
	// THE STATUS DISTINGUISHES THE TWO SUCCESSFUL OUTCOMES. `applied` and `degraded` are both real
	// applies; collapsing them into `applied` would make the backend record a deployment that never
	// came up as one that did, and 2.3's timeline and 2.12's self-healing both branch on it.
	status := "applied"
	if !report.Healthy {
		status = "degraded"
	}
	return Result{Status: status, Output: string(encoded)}, nil
}

// errOrRefused keeps the message honest when the tool ran and simply said no.
func errOrRefused(err error) error {
	if err != nil {
		return err
	}
	return errors.New("kubectl exited non-zero")
}

// firstLine is the last non-empty line, which is where both tools put their conclusion.
//
// Named `firstLine` for the caller's intent — "the one line worth showing" — and implemented as the LAST
// non-empty one because `kubectl rollout status` streams progress and its verdict is at the end. Taking
// the literal first line would report "Waiting for deployment..." as the outcome of a rollout that
// subsequently succeeded.
func firstLine(text string) string {
	lines := strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n")
	for index := len(lines) - 1; index >= 0; index-- {
		if trimmed := strings.TrimSpace(lines[index]); trimmed != "" {
			return trimmed
		}
	}
	return ""
}
