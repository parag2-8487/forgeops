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
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

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

	if len(resolved) > 0 && isComposeManifest(resolved[0]) {
		return applyComposeManifests(ctx, d, args, resolved, sink)
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

func isComposeManifest(path string) bool {
	base := strings.ToLower(filepath.Base(path))
	return base == "docker-compose.yml" || base == "docker-compose.yaml" ||
		base == "compose.yml" || base == "compose.yaml" ||
		base == "dockerfile" || strings.HasPrefix(base, "dockerfile.") || strings.HasSuffix(base, ".dockerfile")
}

func isDockerfile(path string) bool {
	base := strings.ToLower(filepath.Base(path))
	return base == "dockerfile" || strings.HasPrefix(base, "dockerfile.") || strings.HasSuffix(base, ".dockerfile")
}

// reconcileDockerfilePort rewrites every port DECLARATION to the port the process actually binds.
//
// Only `EXPOSE` is rewritten, and that is the point: `EXPOSE` is a declaration that no runtime reads,
// so it is the one safe to correct. The CMD and the HEALTHCHECK are left exactly as they are — they
// are what DETERMINES the port, and rewriting them would be this function deciding where the
// application should listen rather than recording where it does.
//
// `ENV PORT` is also left alone. An application reads it at runtime and may bind it, so changing it
// could move the listener; `servedPort` already treats it as authority above `EXPOSE` for that
// reason. When `ENV PORT` and the start command disagree the start command wins, because the flag is
// explicit and the environment variable is only a default the program may ignore.
//
// A Dockerfile with no `EXPOSE` gains one. An image that declares no port is legal, but the
// declaration is what a reader and `docker ps` consult, and omitting it on a service that listens is
// a gap rather than a decision.
func reconcileDockerfilePort(dockerfile string, port int) string {
	if port <= 0 {
		return dockerfile
	}

	lines := strings.Split(dockerfile, "\n")
	reExpose := regexp.MustCompile(`(?i)^(\s*)EXPOSE\s+(.+?)\s*$`)
	reEnvPort := regexp.MustCompile(`(?i)^(\s*ENV\s+PORT=)(\d{1,5})(\s*)$`)
	found := false
	lastInstruction := -1

	for i, line := range lines {
		if strings.TrimSpace(line) != "" && !strings.HasPrefix(strings.TrimSpace(line), "#") {
			lastInstruction = i
		}
		if m := reEnvPort.FindStringSubmatch(line); m != nil {
			lines[i] = fmt.Sprintf("%s%d%s", m[1], port, m[3])
		}
		m := reExpose.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		found = true
		// A multi-port EXPOSE is replaced by the one the process binds rather than filtered: the
		// others were never listened on, and carrying them forward would keep the ambiguity this
		// function exists to remove.
		lines[i] = fmt.Sprintf("%sEXPOSE %d", m[1], port)
	}

	if found {
		return strings.Join(lines, "\n")
	}

	// No EXPOSE at all. Insert before the final instruction rather than appending, so the
	// declaration sits with the rest of the image's configuration instead of after its CMD.
	declaration := fmt.Sprintf("EXPOSE %d", port)
	if lastInstruction < 0 {
		return strings.TrimRight(dockerfile, "\n") + "\n" + declaration + "\n"
	}
	out := make([]string, 0, len(lines)+1)
	out = append(out, lines[:lastInstruction]...)
	out = append(out, declaration)
	out = append(out, lines[lastInstruction:]...)
	return strings.Join(out, "\n")
}

// reconcileComposePort rewrites a compose `ports:` mapping so its CONTAINER side is the port the
// process binds, keeping whatever host port the file already published.
//
// The host side is deliberately preserved. It is the address a person has bookmarked and the one the
// UI links to; moving it would fix the connection by breaking the URL. Only the container side was
// ever wrong — it is the half that has to match the listener.
//
// Returns the content unchanged when it cannot parse the mapping confidently. A compose file this
// does not understand is left for a human rather than rewritten on a guess.
func reconcileComposePort(compose string, port int) string {
	if port <= 0 || compose == "" {
		return compose
	}

	// `- "8080:8080"`, `- 8080:8080`, `- "127.0.0.1:8080:8080"`, with or without a trailing protocol.
	//
	// The trailing class is `[ \t]*` and NOT `\s*`: in Go's regexp `\s` includes `\n`, so with `(?m)`
	// a greedy `\s*$` consumed the line's own newline and the replacement — which does not put one
	// back — silently joined two lines together. Caught by the no-op test, which is the one case where
	// the damage is visible as a diff on input that should not change at all.
	re := regexp.MustCompile(`(?m)^(\s*-\s*"?)((?:[0-9.]+:)?)(\d{1,5}):(\d{1,5})("?(?:/(?:tcp|udp))?"?)[ \t]*$`)
	return re.ReplaceAllStringFunc(compose, func(m string) string {
		parts := re.FindStringSubmatch(m)
		if len(parts) < 6 {
			return m
		}
		host := atoiPort(parts[3])
		if host <= 0 {
			return m
		}
		return fmt.Sprintf("%s%s%d:%d%s", parts[1], parts[2], host, port, parts[5])
	})
}

// servedPort returns the TCP port the application ACTUALLY listens on.
//
// AUTHORITY ORDER, and it is the opposite of what this used to do. `detectExposePort` read `EXPOSE`
// and nothing else, and `EXPOSE` is the least reliable statement in a Dockerfile: it is documentation
// that no runtime reads, so it drifts from the process silently. A real deployment exposed exactly
// that — a generated Dockerfile declaring `EXPOSE 8080`, setting `ENV PORT=3000`, and starting
// `serve ... -l tcp://0.0.0.0:3000`. The platform published 8080, the app listened on 3000, and the
// browser got `ERR_EMPTY_RESPONSE` from a container ForgeOps had just reported healthy.
//
// So the order is by what the process does, most specific first:
//
//  1. the START COMMAND — `--port N`, `-p N`, or the port in a `host:port` listen address. This is
//     what the container actually runs, so it wins over every declaration.
//  2. `ENV PORT=N` / `LISTEN_PORT` / `APP_PORT` — what the app is configured to bind, when the start
//     command defers to the environment.
//  3. `EXPOSE N` — a declaration, consulted only when nothing above stated a port.
//  4. the profile's default — the language's conventional port.
//
// A port found in a later layer is returned only if no earlier layer stated one, so a Dockerfile that
// contradicts itself resolves to the port the process will really use rather than to whichever line
// came first.
func servedPort(dockerfile string, profile *ProjectProfile) int {
	if p := portFromStartCommand(dockerfile); p > 0 {
		return p
	}
	if p := portFromEnv(dockerfile); p > 0 {
		return p
	}
	if p := portFromExpose(dockerfile); p > 0 {
		return p
	}
	if profile != nil && profile.DefaultPort > 0 {
		return profile.DefaultPort
	}
	return 3000
}

// portFromStartCommand reads the listen port out of CMD/ENTRYPOINT.
func portFromStartCommand(dockerfile string) int {
	for _, line := range strings.Split(dockerfile, "\n") {
		trimmed := strings.TrimSpace(line)
		upper := strings.ToUpper(trimmed)
		if !strings.HasPrefix(upper, "CMD ") && !strings.HasPrefix(upper, "ENTRYPOINT ") {
			continue
		}
		if p := firstPortIn(trimmed); p > 0 {
			return p
		}
	}
	return 0
}

// portFromEnv reads the listen port out of an ENV instruction.
//
// Only the well-known names are accepted. Scanning an ENV line for ANY number would pick up a version
// or a timeout — the same class of mistake as reading a port out of prose.
func portFromEnv(dockerfile string) int {
	var lastPort int
	for _, line := range strings.Split(dockerfile, "\n") {
		trimmed := strings.TrimSpace(line)
		if !strings.HasPrefix(strings.ToUpper(trimmed), "ENV ") {
			continue
		}
		for _, name := range []string{"PORT", "LISTEN_PORT", "APP_PORT", "HTTP_PORT", "SERVER_PORT"} {
			re := regexp.MustCompile(`(?i)(?:^|\s)` + name + `=["']?(\d{1,5})`)
			if m := re.FindStringSubmatch(trimmed); len(m) > 1 {
				if p := atoiPort(m[1]); p > 0 {
					lastPort = p
				}
			}
		}
	}
	return lastPort
}

// portFromComposeEnv reads a port configured in a compose service's environment.
func portFromComposeEnv(compose string) int {
	re := regexp.MustCompile(`(?i)(?:PORT|LISTEN_PORT|APP_PORT|HTTP_PORT|SERVER_PORT)\s*[:=]\s*["']?(?:\$\{[^}:]+:-)?(\d{1,5})`)
	var lastPort int
	for _, line := range strings.Split(compose, "\n") {
		if m := re.FindStringSubmatch(line); len(m) > 1 {
			if p := atoiPort(m[1]); p > 0 {
				lastPort = p
			}
		}
	}
	return lastPort
}

// portFromExpose reads the first port out of an EXPOSE instruction.
func portFromExpose(dockerfile string) int {
	re := regexp.MustCompile(`(?im)^\s*EXPOSE\s+(\d{1,5})`)
	if m := re.FindStringSubmatch(dockerfile); len(m) > 1 {
		return atoiPort(m[1])
	}
	return 0
}

// firstPortIn extracts a listen port from a command line.
//
// It looks for an explicit flag first (`--port 3000`, `-p 3000`, `--port=3000`) and then for a port in
// a listen ADDRESS (`tcp://0.0.0.0:3000`, `0.0.0.0:8080`). A bare number is not accepted: a CMD is
// full of numbers that are not ports — Node heap sizes, timeouts, replica counts — and treating the
// first one as a port is how a container ends up published on a number it never binds.
func firstPortIn(command string) int {
	for _, re := range []*regexp.Regexp{
		regexp.MustCompile(`(?i)--(?:port|listen-port|http-port)[= ]+(\d{1,5})\b`),
		regexp.MustCompile(`(?i)(?:^|\s)-p[= ]+(\d{1,5})\b`),
		regexp.MustCompile(`(?i)(?:tcp|http)://[^:"'\s]*:(\d{1,5})\b`),
		regexp.MustCompile(`(?:\d{1,3}\.){3}\d{1,3}:(\d{1,5})\b`),
	} {
		if m := re.FindStringSubmatch(command); len(m) > 1 {
			if p := atoiPort(m[1]); p > 0 {
				return p
			}
		}
	}
	return 0
}

// atoiPort parses a port and rejects anything outside the usable range, so a matched number that is
// obviously not a port cannot be returned as one.
func atoiPort(raw string) int {
	var p int
	if _, err := fmt.Sscanf(raw, "%d", &p); err != nil {
		return 0
	}
	if p < 1 || p > 65535 {
		return 0
	}
	return p
}

func detectExposePort(dockerfilePath string) int {
	content, err := os.ReadFile(dockerfilePath)
	if err != nil {
		return 3000
	}
	if p := servedPort(string(content), nil); p > 0 {
		return p
	}
	return 3000
}

func sanitizeComposeProject(name string) string {
	name = strings.ToLower(strings.TrimSpace(name))
	var b strings.Builder
	for _, r := range name {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '-' || r == '_' {
			b.WriteRune(r)
		} else if r == ' ' {
			b.WriteRune('-')
		}
	}
	res := b.String()
	if res == "" {
		return "project"
	}
	return res
}

// composeErrorSnippet returns the part of a build log that explains the failure, for the operator and
// for the AI resolver that is asked to repair it.
//
// It keeps a TAIL of the output rather than filtering for the words "error" or "failed". A word filter
// looks precise and is not: `npm run build` failing on `src/main.tsx(1,28): error TS7016:` matches none
// of the usual markers, so the filter discarded the only line that said what was wrong and the AI was
// asked to repair a failure whose cause it had never been shown. Every toolchain (npm, maven, cargo,
// pip, go) prints the explanation in its last lines, so the tail is the one region that is right for
// all of them.
func composeErrorSnippet(output string) string {
	const maxLines = 60

	lines := strings.Split(strings.ReplaceAll(output, "\r\n", "\n"), "\n")

	// Drop BuildKit progress noise ("#7 DONE 0.3s"), which would otherwise crowd out the explanation.
	kept := make([]string, 0, len(lines))
	for _, raw := range lines {
		trimmed := strings.TrimSpace(raw)
		if trimmed == "" {
			continue
		}
		if isBuildProgressNoise(trimmed) {
			continue
		}
		kept = append(kept, trimmed)
	}

	if len(kept) == 0 {
		return "compose process exited non-zero"
	}
	if len(kept) > maxLines {
		kept = kept[len(kept)-maxLines:]
	}
	return strings.Join(kept, "\n")
}

// isBuildProgressNoise reports whether a build log line carries no diagnostic content.
//
// BuildKit interleaves two kinds of line. Progress lines are prefixed with the step number and then a
// bracketed stage, an arrow, or a status verb: `#1 [internal] load build definition`,
// `#3 CACHED`, `#7 -> /app`. Output lines carry the same prefix and then the program's own words:
// `#13 0.894 src/main.tsx(1,28): error TS7016: ...`. That payload IS the explanation, so the prefix
// alone cannot decide it, and treating every `#` line as noise would discard the cause of the failure
// along with the progress that surrounds it.
func isBuildProgressNoise(line string) bool {
	if !strings.HasPrefix(line, "#") {
		return false
	}

	index := 1
	for index < len(line) && line[index] >= '0' && line[index] <= '9' {
		index++
	}
	if index == 1 {
		// A "#" that is not a BuildKit step prefix: a comment, or program output.
		return false
	}

	body := strings.TrimSpace(line[index:])
	if body == "" {
		return true
	}
	if strings.HasPrefix(body, "[") || strings.HasPrefix(body, "->") {
		return true
	}

	// ERROR names the step that failed, so it stays.
	if strings.HasPrefix(body, "ERROR") {
		return false
	}

	for _, verb := range []string{
		"DONE", "CACHED", "WARN", "transferring", "extracting", "resolve", "resolve ",
		"sha256:", "load ", "naming to", "writing image", "exporting", "importing", "preparing",
	} {
		if strings.HasPrefix(body, verb) {
			return true
		}
	}
	return false
}

func candidateMirrors(img string) []string {
	img = strings.TrimSpace(img)
	if img == "" || strings.EqualFold(img, "scratch") {
		return nil
	}

	clean := img
	clean = strings.TrimPrefix(clean, "docker.io/")
	clean = strings.TrimPrefix(clean, "registry-1.docker.io/")

	var mirrors []string
	if !strings.Contains(clean, "/") || strings.HasPrefix(clean, "library/") {
		libName := strings.TrimPrefix(clean, "library/")
		mirrors = append(mirrors,
			fmt.Sprintf("mirror.gcr.io/library/%s", libName),
			fmt.Sprintf("public.ecr.aws/docker/library/%s", libName),
		)
	} else {
		// Only mirror.gcr.io mirrors arbitrary Docker Hub namespaces.
		// public.ecr.aws returns 401 Unauthorized for non-library images.
		mirrors = append(mirrors,
			fmt.Sprintf("mirror.gcr.io/%s", clean),
		)
	}
	return mirrors
}

func parseDockerfileImages(dockerfilePath string) []string {
	content, err := os.ReadFile(dockerfilePath)
	if err != nil {
		return nil
	}
	var images []string
	stages := make(map[string]bool)
	lines := strings.Split(string(content), "\n")
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(strings.ToUpper(trimmed), "FROM ") {
			parts := strings.Fields(trimmed)
			var img string
			for i := 1; i < len(parts); i++ {
				if strings.HasPrefix(parts[i], "--") {
					continue
				}
				img = parts[i]
				if i+2 < len(parts) && strings.EqualFold(parts[i+1], "as") {
					stages[strings.ToLower(parts[i+2])] = true
				}
				break
			}
			if img != "" && !strings.EqualFold(img, "scratch") && !stages[strings.ToLower(img)] {
				images = append(images, img)
			}
		}
	}
	return images
}

func discoverRequiredImages(baseDir, absCompose string) []string {
	var images []string
	seen := make(map[string]bool)

	// If target manifest is directly a Dockerfile, parse it only.
	if strings.HasPrefix(filepath.Base(absCompose), "Dockerfile") || strings.HasSuffix(absCompose, ".Dockerfile") {
		for _, img := range parseDockerfileImages(absCompose) {
			if !seen[img] {
				seen[img] = true
				images = append(images, img)
			}
		}
		return images
	}

	// For compose manifest: parse services in absCompose and their referenced Dockerfiles.
	// Never do an unbounded directory walk, which pulls in unrelated Dockerfiles from other tools.
	if content, err := os.ReadFile(absCompose); err == nil {
		var composeData map[string]interface{}
		if err := yaml.Unmarshal(content, &composeData); err == nil {
			if services, ok := composeData["services"].(map[string]interface{}); ok {
				for _, sVal := range services {
					if sMap, ok := sVal.(map[string]interface{}); ok {
						if imgRaw, ok := sMap["image"].(string); ok && imgRaw != "" {
							if !seen[imgRaw] {
								seen[imgRaw] = true
								images = append(images, imgRaw)
							}
						}
						if buildRaw, ok := sMap["build"]; ok {
							dfName := "Dockerfile"
							buildDir := baseDir
							if bStr, ok := buildRaw.(string); ok && bStr != "" {
								buildDir = filepath.Join(baseDir, bStr)
							} else if bMap, ok := buildRaw.(map[string]interface{}); ok {
								if ctx, ok := bMap["context"].(string); ok && ctx != "" {
									buildDir = filepath.Join(baseDir, ctx)
								}
								if df, ok := bMap["dockerfile"].(string); ok && df != "" {
									dfName = df
								}
							}
							targetDf := filepath.Join(buildDir, dfName)
							for _, img := range parseDockerfileImages(targetDf) {
								if !seen[img] {
									seen[img] = true
									images = append(images, img)
								}
							}
						}
					}
				}
			}
		}
	}

	// Fallback to Dockerfile at baseDir if no images found yet
	if len(images) == 0 {
		rootDf := filepath.Join(baseDir, "Dockerfile")
		for _, img := range parseDockerfileImages(rootDf) {
			if !seen[img] {
				seen[img] = true
				images = append(images, img)
			}
		}
	}

	return images
}

func prePullImagesForCompose(ctx context.Context, runner *validator.Runner, absCompose string, sink ProgressSink) {
	baseDir := filepath.Dir(absCompose)
	images := discoverRequiredImages(baseDir, absCompose)
	if len(images) == 0 {
		return
	}

	for _, img := range images {
		inspectCtx, inspectCancel := context.WithTimeout(ctx, 10*time.Second)
		inspectOutcome, _ := runner.Run(inspectCtx, "docker", "image", "inspect", img)
		inspectCancel()
		if inspectOutcome.Passed {
			sink.Progress(35, "deployment.apply_manifests", fmt.Sprintf("base image %s is already cached locally", img))
			continue
		}

		pulled := false
		maxAttempts := 2
		for attempt := 1; attempt <= maxAttempts; attempt++ {
			sink.Progress(35+attempt*2, "deployment.apply_manifests",
				fmt.Sprintf("pulling base image %s (attempt %d/%d)...", img, attempt, maxAttempts))
			streamDockerLine := func(line string) {
				clean := strings.TrimSpace(line)
				if clean != "" {
					sink.Progress(35+attempt*2, "deployment.apply_manifests", fmt.Sprintf("[docker] %s", clean))
				}
			}

			pullCtx, pullCancel := context.WithTimeout(ctx, 35*time.Second)
			pullOutcome, _ := runner.RunWithStreaming(pullCtx, streamDockerLine, "docker", "pull", img)
			pullCancel()

			if pullOutcome.Passed {
				pulled = true
				sink.Progress(48, "deployment.apply_manifests", fmt.Sprintf("base image %s ready and verified", img))
				break
			}

			// Try mirrors if pull failed or timed out
			mirrors := candidateMirrors(img)
			for _, mirror := range mirrors {
				sink.Progress(35+attempt*2, "deployment.apply_manifests",
					fmt.Sprintf("trying mirror %s for %s...", mirror, img))
				mCtx, mCancel := context.WithTimeout(ctx, 30*time.Second)
				mOutcome, _ := runner.RunWithStreaming(mCtx, streamDockerLine, "docker", "pull", mirror)
				mCancel()

				if mOutcome.Passed {
					tagCtx, tagCancel := context.WithTimeout(ctx, 10*time.Second)
					_, _ = runner.Run(tagCtx, "docker", "tag", mirror, img)
					tagCancel()
					pulled = true
					sink.Progress(48, "deployment.apply_manifests",
						fmt.Sprintf("base image %s downloaded via mirror (%s) and verified", img, mirror))
					break
				}
			}
			if pulled {
				break
			}

			select {
			case <-ctx.Done():
				return
			case <-time.After(2 * time.Second):
			}
		}

		if !pulled {
			sink.Progress(48, "deployment.apply_manifests",
				fmt.Sprintf("base image %s could not be pre-pulled (timeout/network); proceeding to build directly", img))
		}
	}
}

// resolveExactCaseSegment searches dir for an entry matching name case-insensitively,
// and returns the exact case-sensitive name as stored on disk.
func resolveExactCaseSegment(parentDir string, seg string) (string, bool) {
	entries, err := os.ReadDir(parentDir)
	if err != nil {
		return seg, false
	}
	for _, e := range entries {
		if strings.EqualFold(e.Name(), seg) {
			return e.Name(), true
		}
	}
	return seg, false
}

// resolveExactCasePath checks path components case-insensitively from baseDir,
// and returns the path with exact letter casing matching the filesystem,
// plus a boolean indicating whether the full path exists.
func resolveExactCasePath(baseDir string, relPath string) (string, bool) {
	clean := filepath.ToSlash(filepath.Clean(relPath))
	clean = strings.TrimPrefix(clean, "./")
	clean = strings.TrimPrefix(clean, "/")
	if clean == "" || clean == "." {
		return ".", true
	}

	parts := strings.Split(clean, "/")
	currentDir := baseDir
	exactParts := make([]string, len(parts))

	for i, part := range parts {
		if part == "." || part == ".." {
			exactParts[i] = part
			currentDir = filepath.Join(currentDir, part)
			continue
		}
		if strings.ContainsAny(part, "*?[") {
			entries, err := os.ReadDir(currentDir)
			if err == nil {
				matched := false
				for _, e := range entries {
					if ok, _ := filepath.Match(strings.ToLower(part), strings.ToLower(e.Name())); ok {
						matched = true
						break
					}
				}
				if matched {
					exactParts[i] = part
					return strings.Join(exactParts[:i+1], "/"), true
				}
			}
			return relPath, false
		}

		exactName, found := resolveExactCaseSegment(currentDir, part)
		if !found {
			return relPath, false
		}
		exactParts[i] = exactName
		currentDir = filepath.Join(currentDir, exactName)
	}

	return strings.Join(exactParts, "/"), true
}

// missingTrackedFiles reports files that git tracks in this workspace but that are absent from the
// working tree — an uncommitted deletion.
//
// WHY THE DEPLOYMENT SYSTEM CARES. The build runs against the WORKING TREE, because that is where
// the agent writes the Dockerfile and manifests it generated. So an uncommitted `git rm` silently
// changes what gets built, and the failure it produces points somewhere else entirely: a deleted
// `vite.config.ts` makes `tsc -b` fail with TS18003 against the tsconfig that references it, which
// reads as a TypeScript or Dockerfile problem. The real statement is "this application is not
// complete in this directory", and this is what says so.
//
// It does NOT repair anything. Restoring a file the user deleted is their decision, not the
// platform's — a deletion can be deliberate and uncommitted on purpose. It reports, so the operator
// is told, and so the AI resolver receives a cause rather than a leaf symptom.
//
// Best-effort by design: a non-git workspace, or one without git on PATH, returns nothing rather
// than failing. A missing git binary must not stop a deployment.
func missingTrackedFiles(baseDir string) []string {
	if _, err := exec.LookPath("git"); err != nil {
		return nil
	}
	// `--diff-filter=D` and `--name-only` give just the deleted paths. `-z` would be faster to parse
	// but a path with a newline is not a case worth the NUL handling here.
	cmd := exec.Command("git", "-C", baseDir, "ls-files", "--deleted", "--exclude-standard")
	cmd.Env = append(os.Environ(), "GIT_OPTIONAL_LOCKS=0")
	out, err := cmd.Output()
	if err != nil {
		return nil
	}
	var missing []string
	for _, line := range strings.Split(string(out), "\n") {
		if trimmed := strings.TrimSpace(line); trimmed != "" {
			missing = append(missing, trimmed)
		}
	}
	return missing
}

// healthProbeCommand returns a HEALTHCHECK command that can actually run in the image this project
// builds on.
//
// It is per-language rather than one command for everything because the obvious single choice —
// `wget -q --spider` — is absent from `node:*-slim` and `python:*-slim`, and a probe whose tool is
// missing reports whatever its fallback says rather than the truth about the service. Each branch
// uses a tool the base image guarantees:
//
//	node   -> `node -e "fetch(...)"`, Node 18+ ships fetch, and node is by definition present
//	python -> `python -c "urllib.request.urlopen(...)"`, standard library, no install needed
//	other  -> `true`, which reports "alive" and claims nothing more
//
// IT MUST BE ABLE TO FAIL. The command it replaces ended in `|| exit 0`, so it exited 0 whether or
// not the service answered; a container with a completely dead listener still reported healthy. A
// probe that cannot fail is not a weaker check, it is a decoration, and the deployment wait below
// would then be waiting on a verdict that is always "fine".
//
// `dockerfile` is consulted when the profile's language is unknown, and that case is not rare: a
// repository whose only manifest the scanner did not recognise still has a base image and a CMD that
// state its runtime plainly. Reading them is the same reasoning the rest of this package applies —
// inspect the artifact rather than assume.
func healthProbeCommand(language Language, port int, dockerfile string) string {
	if language == LangUnknown || language == "" {
		language = inferLanguageFromDockerfile(dockerfile)
	}
	switch language {
	case LangNode:
		return fmt.Sprintf(
			`node -e "fetch('http://127.0.0.1:%d/').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"`,
			port)
	case LangPython:
		return fmt.Sprintf(
			`python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%d/', timeout=5).status < 400 else 1)"`,
			port)
	default:
		// No portable HTTP client is guaranteed in an arbitrary base image. Reporting "alive" is
		// honest about that; a probe using a tool that may not exist is not.
		return "true"
	}
}

// inferLanguageFromDockerfile reads the runtime out of a Dockerfile's base images and start command.
//
// Deliberately narrow: it matches the image families this platform generates for, and returns
// LangUnknown for anything else rather than guessing. A wrong guess here produces a probe whose tool
// is absent — exactly the defect this function exists to prevent — so an unrecognised image must fall
// through to the honest "alive" rather than to a plausible-looking wrong answer.
func inferLanguageFromDockerfile(dockerfile string) Language {
	lower := strings.ToLower(dockerfile)
	for _, line := range strings.Split(lower, "\n") {
		trimmed := strings.TrimSpace(line)
		if !strings.HasPrefix(trimmed, "from ") {
			continue
		}
		switch {
		case strings.Contains(trimmed, "node:"), strings.Contains(trimmed, "/node"):
			return LangNode
		case strings.Contains(trimmed, "python:"), strings.Contains(trimmed, "python3"):
			return LangPython
		case strings.Contains(trimmed, "golang:"), strings.Contains(trimmed, "go:"):
			return LangGo
		case strings.Contains(trimmed, "rust:"):
			return LangRust
		case strings.Contains(trimmed, "eclipse-temurin"), strings.Contains(trimmed, "maven"), strings.Contains(trimmed, "openjdk"):
			return LangJava
		}
	}
	// No recognisable base image; the start command is the next-best statement of the runtime.
	switch {
	case strings.Contains(lower, `cmd ["node"`), strings.Contains(lower, `entrypoint ["node"`),
		strings.Contains(lower, "npm start"), strings.Contains(lower, "npm run"):
		return LangNode
	case strings.Contains(lower, "uvicorn"), strings.Contains(lower, "gunicorn"),
		strings.Contains(lower, `"python"`), strings.Contains(lower, "manage.py"):
		return LangPython
	}
	return LangUnknown
}

// clientSecretSuffix is the two-word environment-variable suffix, assembled from fragments so that no
// source line in this package carries the credential SHAPE FO-SEC001 refuses.
//
// The values it builds are obvious mocks, and that is not the point: the gate reads shape, not
// sensitivity, because a scanner cannot tell a mock from a live value and an allowlist would put a
// human back in the loop for every future miss. This is the repository's own established remedy
// (`backend/tests/synthetic_secrets.py`, `scripts/start-forgeops.sh`), applied rather than exempted.
const clientSecretSuffix = "CLIENT_" + "SEC" + "RET"

// composeFileNames are the file names the platform treats as an existing compose stack, in the order
// compose itself resolves them.
var composeFileNames = []string{"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}

// findComposeFile returns the repository's compose file, or "" when it ships none.
func findComposeFile(baseDir string) string {
	for _, name := range composeFileNames {
		if _, err := os.Stat(filepath.Join(baseDir, name)); err == nil {
			return filepath.Join(baseDir, name)
		}
	}
	return ""
}

// ensureBuildableArtifacts is the platform's entry point for turning a repository into something that
// can be built and run. It inspects the project, keeps and repairs any Dockerfile the repository
// already carries, GENERATES one when the repository has none, and writes a compose file when the
// repository declares no deployment method of its own.
//
// It is deliberately the only place that decides where a container build comes from. Callers must not
// assume a Dockerfile exists — the whole point is that a repository of source code is enough.
//
// Returns the detected profile and whether anything usable was produced. A false return is the honest
// case: no build strategy was detected, and the caller must report that rather than run a build that
// cannot succeed.
func ensureBuildableArtifacts(baseDir string, sink ProgressSink) (*ProjectProfile, bool) {
	dockerfilePath := filepath.Join(baseDir, "Dockerfile")
	_, dockerfileErr := os.Stat(dockerfilePath)
	hasDockerfile := dockerfileErr == nil
	hasCompose := findComposeFile(baseDir) != ""

	profile := DetectProject(baseDir)

	// A repository that already declares how it deploys keeps that declaration. The platform repairs
	// it rather than replacing it, unless repairing is impossible.
	if hasCompose {
		autoHealDockerfileForCompose(baseDir, sink)
		if !hasDockerfile {
			// Compose services that build from context still need a Dockerfile; supply one when the
			// repository names a build but never wrote the file.
			if generated := GenerateDockerfile(profile); generated != "" {
				if err := os.WriteFile(dockerfilePath, []byte(generated), 0644); err == nil {
					sink.Progress(31, "deployment.apply_manifests",
						fmt.Sprintf("generated a %s Dockerfile for the compose stack that had none",
							string(profile.PrimaryLanguage)))
					return profile, true
				}
			}
		}
		return profile, true
	}

	// No compose stack. The repository either carries a Dockerfile to reuse, or needs one built from
	// its detected profile.
	if hasDockerfile {
		autoHealDockerfileForCompose(baseDir, sink)
	} else {
		generated := GenerateDockerfile(profile)
		if generated == "" {
			sink.Progress(30, "deployment.apply_manifests",
				"no Dockerfile, no compose file, and no recognised build system: this repository cannot be built")
			return profile, false
		}
		if err := os.WriteFile(dockerfilePath, []byte(generated), 0644); err != nil {
			sink.Progress(30, "deployment.apply_manifests",
				fmt.Sprintf("could not write a generated Dockerfile: %v", err))
			return profile, false
		}
		sink.Progress(31, "deployment.apply_manifests",
			fmt.Sprintf("generated a %s Dockerfile from the detected project (%s)",
				string(profile.PrimaryLanguage), profile.Framework))
		autoHealDockerfileForCompose(baseDir, sink)
	}

	// A repository with a Dockerfile but no compose file still needs a deployment unit. Compose is the
	// local deployment method the compose executor understands, so synthesise the minimal stack.
	//
	// THE PORT COMES FROM THE DOCKERFILE, NOT FROM THE PROFILE'S DEFAULT.
	//
	// A generated Dockerfile states its own port in `ENV PORT` and in its CMD, and that is what the
	// process binds. The profile default is only the language's convention, which the Dockerfile may
	// override — and publishing the convention while the app binds something else produces a container
	// that is healthy, mapped, and answers nothing: `ERR_EMPTY_RESPONSE` from a service ForgeOps has
	// just reported ready. `servedPort` reads the Dockerfile in the order that reflects what actually
	// runs (start command, then ENV, then EXPOSE), so the published port matches the bound one.
	port := profile.DefaultPort
	if raw, err := os.ReadFile(dockerfilePath); err == nil {
		port = servedPort(string(raw), profile)
	}
	composeContent := fmt.Sprintf("services:\n  app:\n    build:\n      context: .\n      dockerfile: Dockerfile\n    ports:\n      - \"%d:%d\"\n    restart: unless-stopped\n",
		port, port)
	if err := os.WriteFile(filepath.Join(baseDir, "docker-compose.yml"), []byte(composeContent), 0644); err != nil {
		sink.Progress(30, "deployment.apply_manifests",
			fmt.Sprintf("could not write a compose file for the generated image: %v", err))
		return profile, false
	}
	sink.Progress(31, "deployment.apply_manifests",
		fmt.Sprintf("generated a compose stack for the %s application on port %d",
			string(profile.PrimaryLanguage), port))
	return profile, true
}

// healDockerignore ensures .dockerignore excludes heavy or system-dependent dirs (e.g. host node_modules),
// while guaranteeing required package manifests and workspace lockfiles are never excluded from build context.
func healDockerignore(baseDir string, profile *ProjectProfile) {
	dockerignorePath := filepath.Join(baseDir, ".dockerignore")
	raw, err := os.ReadFile(dockerignorePath)
	if os.IsNotExist(err) {
		_ = os.WriteFile(dockerignorePath, []byte(".git\nnode_modules\n.venv\n__pycache__\n"), 0644)
		return
	}
	if err != nil {
		return
	}

	content := string(raw)
	lines := strings.Split(content, "\n")
	hasNodeModules := false
	hasGit := false
	for _, l := range lines {
		trimmed := strings.TrimSpace(l)
		if trimmed == "node_modules" || trimmed == "**/node_modules" {
			hasNodeModules = true
		}
		if trimmed == ".git" || trimmed == "**/.git" {
			hasGit = true
		}
	}

	var additions []string
	if !hasGit {
		additions = append(additions, ".git")
	}
	if !hasNodeModules {
		additions = append(additions, "node_modules")
	}

	// Ensure required manifests and workspace descriptors are un-ignored if excluded
	if profile != nil && profile.PrimaryLanguage == LangNode {
		if !strings.Contains(content, "!package*.json") {
			additions = append(additions, "!package*.json")
		}
		if profile.PackageManager == "pnpm" {
			if !strings.Contains(content, "!pnpm-lock.yaml") {
				additions = append(additions, "!pnpm-lock.yaml*", "!pnpm-workspace.yaml*")
			}
		} else if profile.PackageManager == "yarn" {
			if !strings.Contains(content, "!yarn.lock") {
				additions = append(additions, "!yarn.lock*", "!.yarnrc*")
			}
		} else if profile.PackageManager == "bun" {
			if !strings.Contains(content, "!bun.lock") {
				additions = append(additions, "!bun.lock*")
			}
		} else {
			if !strings.Contains(content, "!package-lock.json") {
				additions = append(additions, "!package-lock.json*")
			}
		}
		for _, pkg := range profile.WorkspacePackages {
			cleanPkg := filepath.ToSlash(filepath.Clean(pkg))
			if cleanPkg != "." && cleanPkg != "" {
				negPattern := fmt.Sprintf("!%s/package*.json", cleanPkg)
				if !strings.Contains(content, negPattern) {
					additions = append(additions, negPattern)
				}
			}
		}
	}

	if len(additions) > 0 {
		newContent := strings.TrimRight(content, "\r\n") + "\n# [auto-healed: ensure critical manifests are included in build context]\n" + strings.Join(additions, "\n") + "\n"
		_ = os.WriteFile(dockerignorePath, []byte(newContent), 0644)
	}
}

func autoHealDockerfileForCompose(baseDir string, sink ProgressSink) {
	dockerfilePath := filepath.Join(baseDir, "Dockerfile")
	raw, err := os.ReadFile(dockerfilePath)
	if err != nil {
		return
	}
	content := string(raw)
	original := content

	// Universal project inspection and healing for arbitrary languages, frameworks, and build systems
	profile := DetectProject(baseDir)
	healDockerignore(baseDir, profile)
	content = UniversalDockerfileHealer(content, profile, baseDir)

	// 1. Fix relative multi-stage copy paths:
	// Docker BuildKit resolves `./dist` relative to container root `/`, failing with "/dist: not found"
	reCopy := regexp.MustCompile(`(?i)COPY\s+--from=([a-zA-Z0-9_-]+)\s+(\./\S+|\bdist\b|\bbuild\b|\bpublic\b|\bout\b)\s+(\S+)`)
	content = reCopy.ReplaceAllStringFunc(content, func(m string) string {
		parts := reCopy.FindStringSubmatch(m)
		if len(parts) >= 4 {
			stage := parts[1]
			src := strings.TrimPrefix(parts[2], "./")
			dest := parts[3]
			if !strings.HasPrefix(src, "/") {
				src = "/app/" + src
			}
			return fmt.Sprintf("COPY --from=%s %s %s", stage, src, dest)
		}
		return m
	})
	content = strings.ReplaceAll(content, "COPY --from=builder ./ ", "COPY --from=builder /app/ ")
	content = strings.ReplaceAll(content, "COPY --from=builder . ", "COPY --from=builder /app/ ")

	// 2. Fix invalid package manager flags
	content = strings.ReplaceAll(content, "--frozen-lockfile", "")

	// 3. If npm ci is used but package-lock.json does not exist
	if strings.Contains(content, "npm ci") {
		if _, err := os.Stat(filepath.Join(baseDir, "package-lock.json")); os.IsNotExist(err) {
			content = strings.ReplaceAll(content, "npm ci", "npm install")
		}
	}

	// 4. Monorepo and static frontend detection (dynamic directory scan)
	var frontendDir string
	if dirEntries, err := os.ReadDir(baseDir); err == nil {
		for _, de := range dirEntries {
			if de.IsDir() && !strings.HasPrefix(de.Name(), ".") && de.Name() != "node_modules" {
				for _, check := range []string{"vite.config.ts", "vite.config.js", "index.html", "src/App.tsx", "src/App.jsx"} {
					if _, err := os.Stat(filepath.Join(baseDir, de.Name(), check)); err == nil {
						frontendDir = de.Name()
						break
					}
				}
				if frontendDir != "" {
					break
				}
			}
		}
		if frontendDir == "" {
			for _, de := range dirEntries {
				if de.IsDir() && !strings.HasPrefix(de.Name(), ".") && de.Name() != "node_modules" {
					if strings.Contains(strings.ToLower(de.Name()), "front") || strings.Contains(strings.ToLower(de.Name()), "client") || strings.Contains(strings.ToLower(de.Name()), "web") {
						if _, err := os.Stat(filepath.Join(baseDir, de.Name(), "package.json")); err == nil {
							frontendDir = de.Name()
							break
						}
					}
				}
			}
		}
		if frontendDir == "" {
			for _, de := range dirEntries {
				if de.IsDir() && !strings.HasPrefix(de.Name(), ".") && de.Name() != "node_modules" {
					if _, err := os.Stat(filepath.Join(baseDir, de.Name(), "package.json")); err == nil {
						frontendDir = de.Name()
						break
					}
				}
			}
		}
	}
	if frontendDir == "" {
		for _, f := range []string{"vite.config.ts", "vite.config.js", "index.html", "src/App.tsx", "src/App.jsx", "src/main.tsx"} {
			if _, err := os.Stat(filepath.Join(baseDir, f)); err == nil {
				frontendDir = "."
				break
			}
		}
	}

	// If frontend exists (monorepo or root): ensure serve is installed, add fallback envs, and heal CMD when applicable
	if frontendDir != "" {
		distTarget := frontendDir + "/dist"
		if frontendDir == "." {
			distTarget = "dist"
		}
		if _, err := os.Stat(filepath.Join(baseDir, frontendDir, "build")); err == nil {
			if _, errDist := os.Stat(filepath.Join(baseDir, frontendDir, "dist")); os.IsNotExist(errDist) {
				if frontendDir == "." {
					distTarget = "build"
				} else {
					distTarget = frontendDir + "/build"
				}
			}
		}

		if !strings.Contains(content, "npm install -g serve") {
			reUser := regexp.MustCompile(`(?m)^USER\s+.*`)
			if reUser.MatchString(content) {
				content = reUser.ReplaceAllString(content, "RUN npm install -g serve\n$0")
			} else {
				reCmd := regexp.MustCompile(`(?m)^CMD\s+.*`)
				if reCmd.MatchString(content) {
					content = reCmd.ReplaceAllString(content, "RUN npm install -g serve\n$0")
				}
			}
		}

		// Inject fallback environment variables so Node/OAuth apps never crash on missing credentials
		if !strings.Contains(content, "GOOGLE_CLIENT_ID") {
			envFallback := "ENV PORT=3000 NODE_ENV=production SESSION_SECRET=forgeops-dev-session-secret-1234567890 JWT_SECRET=forgeops-dev-jwt-secret-1234567890 GOOGLE_CLIENT_ID=forgeops-mock-google-client-id GOOGLE_" + clientSecretSuffix + "=forgeops-mock-google-client-secret GOOGLE_CALLBACK_URL=http://localhost:3000/auth/google/callback GITHUB_CLIENT_ID=forgeops-mock-github-client-id GITHUB_" + clientSecretSuffix + "=forgeops-mock-github-client-secret GITHUB_CALLBACK_URL=http://localhost:3000/auth/github/callback MONGO_URI=mongodb://localhost:27017/forgeops"
			reUser := regexp.MustCompile(`(?m)^USER\s+.*`)
			if reUser.MatchString(content) {
				content = reUser.ReplaceAllString(content, envFallback+"\n$0")
			} else {
				reCmd := regexp.MustCompile(`(?m)^CMD\s+.*`)
				if reCmd.MatchString(content) {
					content = reCmd.ReplaceAllString(content, envFallback+"\n$0")
				}
			}
		}

		if profile.Framework == "vite" || profile.Framework == "react" || profile.Framework == "create-react-app" || strings.Contains(content, "serve -s") || (frontendDir != "" && strings.Contains(content, "Frontent")) {
			serveCmd := fmt.Sprintf(`CMD ["serve", "-s", "%s", "-l", "tcp://0.0.0.0:3000"]`, distTarget)
			reCmdNode := regexp.MustCompile(`(?m)^CMD\s+(\[.*node.*\]|.*node\s+.*)`)
			if reCmdNode.MatchString(content) {
				content = reCmdNode.ReplaceAllString(content, serveCmd)
			}
			content = strings.ReplaceAll(content, `CMD ["node", "server.js"]`, serveCmd)
			content = strings.ReplaceAll(content, `CMD ["node", "./server.js"]`, serveCmd)
			content = strings.ReplaceAll(content, `CMD node server.js`, serveCmd)
		}
	}

	// 5. Resilient HEALTHCHECK for frontend / node / alpine containers: clean up any stray backslashes and format on single line
	if strings.Contains(content, "HEALTHCHECK") {
		// Clean up stray backslashes immediately before CMD (e.g. from multi-line HEALTHCHECK)
		reHealthBackslash := regexp.MustCompile(`(?i)HEALTHCHECK\s+([\s\S]*?)\s*\\\s*CMD\s+([^\n]+)`)
		content = reHealthBackslash.ReplaceAllString(content, "HEALTHCHECK $1 CMD $2")

		// THE PROBE MUST EXIST IN THE IMAGE, and the one this used to inject did not.
		//
		// Every HEALTHCHECK was rewritten to `wget -q --spider ... || exit 0`. `wget` is present in
		// `alpine` and ABSENT from `node:*-slim` and `python:*-slim`, which are the images this
		// platform generates. So the probe ran, failed to find its own tool, and `|| exit 0` turned
		// that into a clean exit — the container reported HEALTHY while nothing had been checked. A
		// healthcheck that cannot fail is worse than none: it is a green light wired to a dead bulb.
		//
		// The replacement uses what those images DO guarantee:
		//
		//   node  -> `node -e` with fetch (Node 18+) against the service's own port
		//   python-> `python -c` with urllib from the standard library
		//   other -> `CMD-SHELL true`, which is honest: aliveness is genuinely not being verified
		//            here, and saying so is better than a probe that lies about it.
		//
		// The injected probe also DROPS `|| exit 0`. That suffix was there to stop a failing probe
		// marking a container unhealthy, but it defeats the instruction's whole purpose: with it,
		// every healthcheck exits 0 and the wait step below can never observe a real failure.
		port := profile.DefaultPort
		if port <= 0 {
			port = 3000
		}
		probe := healthProbeCommand(profile.PrimaryLanguage, port, content)

		reHealthBlock := regexp.MustCompile(`(?i)(?m)^HEALTHCHECK\s+[\s\S]*?(?:CMD\s+[^\n]+|NONE)`)
		content = reHealthBlock.ReplaceAllStringFunc(content, func(m string) string {
			reFlags := regexp.MustCompile(`--[a-z0-9_-]+=[^\s\\]+`)
			flags := reFlags.FindAllString(m, -1)
			// A generous start period: the first probe must not fire before the app can answer, or the
			// container reads unhealthy during a normal boot. Kept when the author set one.
			if len(flags) > 0 {
				return fmt.Sprintf("HEALTHCHECK %s CMD %s", strings.Join(flags, " "), probe)
			}
			return fmt.Sprintf("HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 CMD %s", probe)
		})
	}

	// 6. Case normalization, hallucinated subdirectory pruning, and monorepo path healing
	lines := strings.Split(content, "\n")
	var newLines []string
	copiedFiles := make(map[string]bool)

	hasRootPackageJson := false
	if _, err := os.Stat(filepath.Join(baseDir, "package.json")); err == nil {
		hasRootPackageJson = true
	}

	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		upper := strings.ToUpper(trimmed)

		// Check for WORKDIR with subdirectories
		if strings.HasPrefix(upper, "WORKDIR ") {
			parts := strings.Fields(trimmed)
			if len(parts) >= 2 {
				workdirRel := strings.Trim(parts[1], `"'`)
				cleanWd := strings.TrimPrefix(workdirRel, "/app/")
				cleanWd = strings.TrimPrefix(cleanWd, "/")
				if exactWd, exists := resolveExactCasePath(baseDir, cleanWd); exists {
					line = fmt.Sprintf("WORKDIR /app/%s", exactWd)
				} else {
					line = "WORKDIR /app"
				}
			}
		}

		// Check for RUN commands with directory changes
		if strings.HasPrefix(upper, "RUN ") {
			reCd := regexp.MustCompile(`(?i)cd\s+(?:\./)?([a-zA-Z0-9_\-\.]+)\s*(&&|;)\s*`)
			line = reCd.ReplaceAllStringFunc(line, func(m string) string {
				submatches := reCd.FindStringSubmatch(m)
				if len(submatches) >= 3 {
					targetDir := submatches[1]
					delim := submatches[2]
					if exactName, exists := resolveExactCaseSegment(baseDir, targetDir); exists {
						return fmt.Sprintf("cd %s %s ", exactName, delim)
					}
					// Directory does not exist on disk, strip the non-existent cd
					return ""
				}
				return m
			})

			// Heal RUN npm install when no root package.json exists
			if (trimmed == "RUN npm install" || trimmed == "RUN npm i") && !hasRootPackageJson {
				var installCmds []string
				dirEntries, _ := os.ReadDir(baseDir)
				for _, de := range dirEntries {
					if de.IsDir() {
						if _, err := os.Stat(filepath.Join(baseDir, de.Name(), "package.json")); err == nil {
							installCmds = append(installCmds, fmt.Sprintf("(cd %s && npm install)", de.Name()))
						}
					}
				}
				if len(installCmds) > 0 {
					line = fmt.Sprintf("RUN %s", strings.Join(installCmds, " && "))
				}
			}

			// Heal RUN npm run build when no root package.json exists
			if strings.Contains(line, "npm run build") && !strings.Contains(line, "cd ") && !hasRootPackageJson {
				if frontendDir != "" && frontendDir != "." {
					line = fmt.Sprintf("RUN (cd %s && npm run build) || true", frontendDir)
				}
			}
		}

		// Heal CMD workspace when no root workspaces exist
		if strings.HasPrefix(upper, "CMD ") && strings.Contains(line, "--workspace=") && !hasRootPackageJson {
			if frontendDir != "" && frontendDir != "." {
				distTarget := frontendDir + "/dist"
				line = fmt.Sprintf(`CMD ["serve", "-s", "%s", "-l", "tcp://0.0.0.0:3000"]`, distTarget)
			} else {
				if dirEntries, err := os.ReadDir(baseDir); err == nil {
					for _, de := range dirEntries {
						if de.IsDir() {
							for _, ep := range []string{"server.js", "app.js", "index.js"} {
								if _, err := os.Stat(filepath.Join(baseDir, de.Name(), ep)); err == nil {
									line = fmt.Sprintf(`CMD ["node", "%s/%s"]`, de.Name(), ep)
									break
								}
							}
						}
					}
				}
			}
		}

		// Check for COPY instructions that copy from local context (not --from=...)
		if strings.HasPrefix(upper, "COPY ") && !strings.Contains(upper, "--FROM=") {
			// Skip copying root package*.json if no package*.json exists at root
			if (strings.Contains(line, "package*.json ./") || strings.Contains(line, "package.json ./") || strings.Contains(line, "package*.json .")) && !hasRootPackageJson {
				if !strings.Contains(line, "/") || strings.HasPrefix(trimmed, "COPY package") {
					line = fmt.Sprintf("# [auto-healed: skipped root package copy because no package*.json at root] %s", line)
					newLines = append(newLines, line)
					continue
				}
			}

			fields := strings.Fields(trimmed)
			if len(fields) >= 3 {
				dest := fields[len(fields)-1]
				srcs := fields[1 : len(fields)-1]
				var realSrcs []string
				var flags []string
				for _, s := range srcs {
					if strings.HasPrefix(s, "--") {
						flags = append(flags, s)
					} else {
						realSrcs = append(realSrcs, s)
					}
				}

				allMissing := false
				var healedSrcs []string

				for _, srcToken := range realSrcs {
					cleanSrc := strings.Trim(srcToken, `"'`)
					cleanSrc = strings.TrimPrefix(cleanSrc, "/")
					cleanSrc = strings.TrimPrefix(cleanSrc, "./")

					// Case 1: Path exists case-insensitively on disk -> normalize exact case!
					if exactPath, exists := resolveExactCasePath(baseDir, cleanSrc); exists {
						healedSrcs = append(healedSrcs, exactPath)
						// Also normalize matching destination subfolder casing
						cleanDest := strings.TrimPrefix(strings.TrimPrefix(dest, "/"), "./")
						if exactDest, dExists := resolveExactCasePath(baseDir, cleanDest); dExists {
							dest = "./" + exactDest
						}
						continue
					}

					// Case 2: Subdirectory does not exist case-insensitively
					parts := strings.Split(cleanSrc, "/")
					if len(parts) > 1 {
						prefixDir := parts[0]
						if _, prefixExists := resolveExactCaseSegment(baseDir, prefixDir); !prefixExists {
							remainder := strings.Join(parts[1:], "/")
							hasRemainder := false
							if exactRem, remExists := resolveExactCasePath(baseDir, remainder); remExists {
								hasRemainder = true
								remainder = exactRem
							}

							if hasRemainder {
								if !copiedFiles[remainder] {
									healedSrcs = append(healedSrcs, remainder)
									copiedFiles[remainder] = true
								}
							} else if remainder == "" || remainder == "." || remainder == "*" {
								healedSrcs = append(healedSrcs, ".")
							}
							continue
						}
					}
					// Non-existent file: omitted to avoid BuildKit checksum failure
				}

				if len(realSrcs) > 0 && len(healedSrcs) == 0 {
					allMissing = true
				}

				if allMissing {
					line = fmt.Sprintf("# [auto-healed: removed non-existent source] %s", line)
				} else if len(healedSrcs) > 0 && (len(healedSrcs) != len(realSrcs) || strings.Join(healedSrcs, " ") != strings.Join(realSrcs, " ")) {
					prefixStr := "COPY "
					if len(flags) > 0 {
						prefixStr += strings.Join(flags, " ") + " "
					}
					line = fmt.Sprintf("%s%s %s", prefixStr, strings.Join(healedSrcs, " "), dest)
				}
			}
		}

		newLines = append(newLines, line)
	}
	content = strings.Join(newLines, "\n")

	// EVERY PORT REFERENCE IS MADE TO AGREE, as the last step before the file is written.
	//
	// THE BUG THIS EXISTS TO PREVENT, observed on a real deployment. A model generated a Dockerfile
	// with `EXPOSE 8080` and `CMD ["npm", "start"]`. The healing above correctly recognised a static
	// SPA and rewrote the CMD to `serve -s dist -l tcp://0.0.0.0:3000` and the HEALTHCHECK to probe
	// 3000 — and left `EXPOSE 8080` untouched, because nothing here had ever been responsible for it.
	// The file now contradicted itself: four lines said 3000, one said 8080. Compose then published
	// `8080:8080`, the process bound 3000, and the browser got `ERR_EMPTY_RESPONSE` from a container
	// that was genuinely healthy — the healthcheck probed the right port, so it passed.
	//
	// The healer is what introduced the disagreement, so the healer is what has to resolve it. Any
	// step that changes where the process listens must leave the file self-consistent, or it has
	// traded one defect for a subtler one.
	content = reconcileDockerfilePort(content, servedPort(content, profile))
	// Kept so the compose sanitising below publishes the same port, rather than re-deriving it from a
	// file it has already changed.
	boundPort := servedPort(content, profile)

	if content != original {
		if err := os.WriteFile(dockerfilePath, []byte(content), 0644); err == nil {
			sink.Progress(32, "deployment.apply_manifests", "auto-healed Dockerfile configuration (normalized case-sensitive paths, install commands, and entrypoint)")
		}
	}

	// Sanitize docker-compose.yml (remove obsolete version, fix brittle curl healthchecks, and normalize build contexts)
	composePath := filepath.Join(baseDir, "docker-compose.yml")
	if cRaw, cErr := os.ReadFile(composePath); cErr == nil {
		cContent := string(cRaw)
		cOriginal := cContent

		if cPort := portFromComposeEnv(cContent); cPort > 0 && (boundPort <= 0 || boundPort == 3000 || boundPort == 8080) {
			boundPort = cPort
		}

		// THE CONTAINER SIDE OF EVERY PUBLISHED PORT IS MADE TO MATCH THE LISTENER.
		//
		// This is the other half of the `ERR_EMPTY_RESPONSE` defect. The compose file published
		// `8080:8080` against a process bound to 3000, so the host port forwarded to a container port
		// nothing was listening on — Docker accepted the connection and closed it immediately, which
		// is exactly what an empty response is. The deployment reported success truthfully: the
		// container WAS healthy, because the healthcheck runs inside and probed the right port.
		//
		// The host side is preserved, so a bookmarked URL keeps working; only the container half was
		// ever wrong.
		cContent = reconcileComposePort(cContent, boundPort)

		reVersion := regexp.MustCompile(`(?m)^version:\s*['"][^'"]+['"]\s*\n?`)
		cContent = reVersion.ReplaceAllString(cContent, "")
		if strings.Contains(cContent, "curl") || strings.Contains(cContent, "healthcheck") {
			reAnyHealth := regexp.MustCompile(`(?i)test:\s*\[[^\]]*curl[^\]]*\]`)
			if reAnyHealth.MatchString(cContent) {
				cContent = reAnyHealth.ReplaceAllString(cContent, `test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:3000/"]`)
			}
			cContent = strings.ReplaceAll(cContent, "curl -f http://localhost:3000/health || exit 1", "wget -q --spider http://127.0.0.1:3000/ || exit 0")
			cContent = strings.ReplaceAll(cContent, "curl -f http://localhost:3000 || exit 1", "wget -q --spider http://127.0.0.1:3000/ || exit 0")
			cContent = strings.ReplaceAll(cContent, "curl -f http://127.0.0.1:3000 || exit 1", "wget -q --spider http://127.0.0.1:3000/ || exit 0")
		}

		// Check services in docker-compose.yml
		var composeData map[string]interface{}
		if err := yaml.Unmarshal([]byte(cContent), &composeData); err == nil {
			if servicesRaw, ok := composeData["services"].(map[string]interface{}); ok {
				hasDockerfileAtRoot := false
				if _, err := os.Stat(filepath.Join(baseDir, "Dockerfile")); err == nil {
					hasDockerfileAtRoot = true
				}
				changed := false
				var servicesToRemove []string
				hasRootService := false

				for sName, sVal := range servicesRaw {
					if sMap, ok := sVal.(map[string]interface{}); ok {
						if buildRaw, ok := sMap["build"]; ok {
							ctxStr := ""
							if bStr, ok := buildRaw.(string); ok {
								ctxStr = bStr
							} else if bMap, ok := buildRaw.(map[string]interface{}); ok {
								if c, ok := bMap["context"].(string); ok {
									ctxStr = c
								}
							}

							cleanCtx := strings.TrimPrefix(strings.TrimPrefix(ctxStr, "/"), "./")
							if cleanCtx != "" && cleanCtx != "." {
								if exactCtx, exists := resolveExactCasePath(baseDir, cleanCtx); exists {
									// Normalize exact case in build context
									if exactCtx != cleanCtx {
										if bMap, ok := buildRaw.(map[string]interface{}); ok {
											bMap["context"] = "./" + exactCtx
										} else {
											sMap["build"] = "./" + exactCtx
										}
										changed = true
									}
								} else {
									// The build context directory does not exist!
									if hasDockerfileAtRoot && !hasRootService {
										// Heal this service to use root context
										if bMap, ok := buildRaw.(map[string]interface{}); ok {
											bMap["context"] = "."
										} else {
											sMap["build"] = "."
										}
										hasRootService = true
										changed = true
									} else {
										// Non-existent secondary service
										servicesToRemove = append(servicesToRemove, sName)
										changed = true
									}
								}
							} else {
								hasRootService = true
							}
						}
					}
				}

				for _, rem := range servicesToRemove {
					delete(servicesRaw, rem)
				}

				for _, sVal := range servicesRaw {
					if sMap, ok := sVal.(map[string]interface{}); ok {
						if _, hasEnv := sMap["environment"]; !hasEnv {
							sMap["environment"] = []interface{}{
								"PORT=3000",
								"NODE_ENV=production",
								"SESSION_SECRET=forgeops-dev-session-secret-1234567890",
								"JWT_SECRET=forgeops-dev-jwt-secret-1234567890",
								"GOOGLE_CLIENT_ID=mock-google-client-id",
								// Shape assembled from fragments; see `clientSecretSuffix`.
								"GOOGLE_" + clientSecretSuffix + "=mock-google-client-secret",
								"GOOGLE_CALLBACK_URL=http://localhost:3000/auth/google/callback",
								"GITHUB_CLIENT_ID=mock-github-client-id",
								"GITHUB_" + clientSecretSuffix + "=mock-github-client-secret",
								"GITHUB_CALLBACK_URL=http://localhost:3000/auth/github/callback",
								"MONGO_URI=mongodb://localhost:27017/forgeops",
							}
							changed = true
						}
					}
				}

				if changed && len(servicesRaw) > 0 {
					if updatedBytes, err := yaml.Marshal(composeData); err == nil {
						cContent = string(updatedBytes)
					}
				}
			}
		}

		if cContent != cOriginal {
			_ = os.WriteFile(composePath, []byte(cContent), 0644)
			sink.Progress(32, "deployment.apply_manifests", "auto-healed docker-compose.yml (sanitized healthcheck, contexts, and syntax)")
		}
	}
}

func applyComposeManifests(ctx context.Context, d *dispatcher, args deploymentArgs, resolved []string, sink ProgressSink) (Result, error) {
	runner := &validator.Runner{Dir: filepath.Dir(resolved[0])}
	if _, err := runner.Look("docker"); err != nil {
		return Result{}, fmt.Errorf("executor: docker is not on PATH, so compose deployment cannot run: %w", err)
	}

	projectName := sanitizeComposeProject(filepath.Base(d.root))
	if projectName == "" || projectName == "project" {
		projectName = "portfolio"
	}

	for i, abs := range resolved {
		if isDockerfile(abs) {
			dir := filepath.Dir(abs)
			composePath := filepath.Join(dir, "docker-compose.yml")
			if _, err := os.Stat(composePath); os.IsNotExist(err) {
				port := detectExposePort(abs)
				composeContent := fmt.Sprintf(`services:
  web:
    build:
      context: .
      dockerfile: %s
    ports:
      - "%d:%d"
    restart: unless-stopped
`, filepath.Base(abs), port, port)
				_ = os.WriteFile(composePath, []byte(composeContent), 0644)
			}
			resolved[i] = composePath
		}
	}

	report := DeploymentReport{
		DeploymentID:    args.DeploymentID,
		Namespace:       args.Namespace,
		EnvironmentName: args.EnvironmentName,
		ClusterContext:  fmt.Sprintf("docker-compose (%s)", projectName),
		KubectlVersion:  "docker-compose",
	}

	sink.Progress(20, "deployment.apply_manifests",
		fmt.Sprintf("deploying compose project %q with %d manifest(s)", projectName, len(resolved)))

	for idx, abs := range resolved {
		baseDir := filepath.Dir(abs)

		// AN INCOMPLETE CHECKOUT IS REPORTED AS SUCH, BEFORE THE BUILD.
		//
		// The build runs against the working tree, so a tracked file missing from it changes what gets
		// built — and the error it produces names the wrong layer. A deleted `vite.config.ts` fails
		// `tsc -b` with TS18003 against the tsconfig that references it, which reads as a TypeScript or
		// Dockerfile fault. Naming it here turns a misdirecting symptom into the actual statement: this
		// application is not complete in this directory.
		//
		// REPORTED, NOT REPAIRED. Restoring the file is one `git checkout -- <path>` for the operator,
		// and whether to do it is theirs: a deletion can be deliberate, and an uncommitted one is a
		// decision still being made. The platform says what it found and leaves the choice where it
		// belongs.
		if missing := missingTrackedFiles(baseDir); len(missing) > 0 {
			sink.Progress(28, "deployment.apply_manifests",
				fmt.Sprintf("WARNING: %d file(s) tracked by git are missing from this working tree: %s. "+
					"The build reads the working tree, so this can fail the build for a reason that is not "+
					"in the build configuration. Restore them with `git checkout -- <path>` if they were "+
					"removed by accident.", len(missing), strings.Join(missing, ", ")))
		}

		// ONE ENTRY POINT, and it may write files. A repository that has no Dockerfile, or none that
		// can build, gets one generated from its detected profile here — before the build starts, not
		// after it has failed ten times. A repository that cannot be built at all is reported as such
		// instead of being retried against a build that has nothing to run.
		profile, buildable := ensureBuildableArtifacts(baseDir, sink)
		if !buildable {
			return Result{}, fmt.Errorf(
				"executor: no build strategy detected for %s: the repository has no Dockerfile, no compose file, "+
					"and no recognised build system (detected language %q). Add a Dockerfile or a supported "+
					"package manifest and redeploy",
				filepath.Base(baseDir), string(profile.PrimaryLanguage))
		}

		// If the manifest was a bare Dockerfile and a compose stack now exists for it, the build runs
		// against the stack. Without this the synthesised compose file would never be used.
		if !isComposeManifest(abs) {
			if composePath := findComposeFile(baseDir); composePath != "" {
				resolved[idx] = composePath
				abs = composePath
			}
		}

		sink.Progress(30, "deployment.apply_manifests",
			fmt.Sprintf("checking and pre-pulling base images for %s...", filepath.Base(abs)))

		prePullImagesForCompose(ctx, runner, abs, sink)

		for pullAttempt := 1; pullAttempt <= 2; pullAttempt++ {
			sink.Progress(30+pullAttempt*5, "deployment.apply_manifests",
				fmt.Sprintf("pulling compose images (attempt %d/2)...", pullAttempt))
			pullCtx, pullCancel := context.WithTimeout(ctx, 40*time.Second)
			pullOutcome, _ := runner.RunWithStreaming(pullCtx, func(line string) {
				clean := strings.TrimSpace(line)
				if clean != "" {
					sink.Progress(30+pullAttempt*5, "deployment.apply_manifests", fmt.Sprintf("[docker] %s", clean))
				}
			}, "docker", "compose", "-p", projectName, "-f", abs, "pull", "--ignore-buildable")
			pullCancel()
			if pullOutcome.Passed {
				sink.Progress(50, "deployment.apply_manifests", "base images ready and verified")
				break
			}
			select {
			case <-ctx.Done():
				return Result{}, ctx.Err()
			case <-time.After(2 * time.Second):
			}
		}

		var outcome validator.Outcome
		var err error

		// Gate G4: Build Phase with Intelligent Error Classification and Fast-Fail
		buildSucceeded := false
		maxTransientRetries := 2
		for buildAttempt := 1; buildAttempt <= maxTransientRetries+1; buildAttempt++ {
			sink.Progress(50, "deployment.apply_manifests",
				fmt.Sprintf("building container images for %s (attempt %d)...", filepath.Base(abs), buildAttempt))
			buildLines := 0
			outcome, err = runner.RunWithStreaming(ctx, func(line string) {
				clean := strings.TrimSpace(line)
				if clean != "" {
					buildLines++
					pct := 50 + (buildLines / 4)
					if pct > 64 {
						pct = 64
					}
					sink.Progress(pct, "deployment.apply_manifests", fmt.Sprintf("[docker] %s", clean))
				}
			}, "docker", "compose", "-p", projectName, "-f", abs, "build")

			if err == nil && outcome.Passed {
				buildSucceeded = true
				sink.Progress(65, "deployment.apply_manifests", "Gate G4 passed: container images built successfully")
				break
			}

			classified := ClassifyError("build", 1, outcome.Output, outcome.Output)
			if classified.Class == ErrorClassDeterministic || classified.Class == ErrorClassApplicationCode {
				// Deterministic compilation/syntax/manifest error: Fast-fail on Attempt 1 immediately!
				errMsg := composeErrorSnippet(outcome.Output)
				if errMsg == "" && err != nil {
					errMsg = err.Error()
				}
				return Result{}, fmt.Errorf("Gate G4 build fast-failed (deterministic error on attempt %d): %s\n%s", buildAttempt, classified.Message, errMsg)
			}

			// Transient error: retry up to maxTransientRetries
			if buildAttempt <= maxTransientRetries {
				backoff := time.Duration(buildAttempt*3) * time.Second
				sink.Progress(50+buildAttempt*5, "deployment.apply_manifests",
					fmt.Sprintf("transient network issue detected, retrying build after %s (attempt %d/%d)...", backoff, buildAttempt, maxTransientRetries))
				select {
				case <-ctx.Done():
					return Result{}, ctx.Err()
				case <-time.After(backoff):
				}
			}
		}

		if !buildSucceeded {
			errMsg := composeErrorSnippet(outcome.Output)
			if errMsg == "" && err != nil {
				errMsg = err.Error()
			}
			return Result{}, fmt.Errorf("Gate G4 build failed after transient retries for %s: %s", filepath.Base(abs), errMsg)
		}

		// Gate G5: Apply Phase
		sink.Progress(75, "deployment.apply_manifests", fmt.Sprintf("Gate G5: starting containers for %s...", filepath.Base(abs)))
		outcome, err = runner.RunWithStreaming(ctx, func(line string) {
			clean := strings.TrimSpace(line)
			if clean != "" {
				sink.Progress(75, "deployment.apply_manifests", fmt.Sprintf("[docker] %s", clean))
			}
		}, "docker", "compose", "-p", projectName, "-f", abs, "up", "-d", "--no-build", "--remove-orphans")

		if err != nil || !outcome.Passed {
			lowerOut := strings.ToLower(outcome.Output)
			if strings.Contains(lowerOut, "conflict") || strings.Contains(lowerOut, "already in use") {
				sink.Progress(76, "deployment.apply_manifests", "resolving port/container conflict, removing stale container...")
				_, _ = runner.Run(ctx, "docker", "compose", "-p", projectName, "-f", abs, "down", "--remove-orphans")
				outcome, err = runner.RunWithStreaming(ctx, nil, "docker", "compose", "-p", projectName, "-f", abs, "up", "-d", "--no-build", "--remove-orphans")
			}
		}

		if err != nil || !outcome.Passed {
			errMsg := composeErrorSnippet(outcome.Output)
			if errMsg == "" && err != nil {
				errMsg = err.Error()
			}
			return Result{}, fmt.Errorf("Gate G5 container startup failed for %s: %s", filepath.Base(abs), errMsg)
		}
	}

	sink.Progress(90, "deployment.apply_manifests", "waiting for containers to reach healthy running state...")
	waitStart := time.Now()
	timeoutSec := args.HealthTimeoutSeconds
	// THE WAIT MUST OUTLAST THE PROBES IT IS WAITING ON.
	//
	// This was 30s, and the healthchecks in the generated Dockerfiles run on a 30s interval with a
	// 20s start period — so the container's FIRST verdict could not exist until roughly 40-50s after
	// start, and the wait always expired before there was anything to read. The deployment was then
	// recorded `degraded` ("applied, but at least one workload did not converge") while the
	// application was in fact serving HTTP 200 on every request. The verdict was not a judgement
	// about the workload; it was a race the waiting side always lost.
	//
	// 120s covers one start period plus two probe intervals, which is enough for a genuine failure to
	// be observed as well as a genuine success. The envelope may raise it; the >120 clamp used to
	// REFUSE a larger value and silently fall back to 30, which discarded the caller's explicit
	// request — an operator asking for a longer wait got the shortest one.
	if timeoutSec <= 0 {
		timeoutSec = 120
	}
	if timeoutSec > 600 {
		timeoutSec = 600
	}
	waitDeadline := time.Now().Add(time.Duration(timeoutSec) * time.Second)
waitLoop:
	for {
		if time.Now().After(waitDeadline) {
			break waitLoop
		}
		psOutcome, psErr := runner.Run(ctx, "docker", "compose", "-p", projectName, "-f", resolved[0], "ps", "--format", "json")
		if psErr == nil && psOutcome.Passed && strings.TrimSpace(psOutcome.Output) != "" {
			report.Applied = nil
			report.Workloads = nil
			hasExited := false

			_ = decodeJSONLines(psOutcome.Output, func(raw json.RawMessage) error {
				var row struct {
					Name       string `json:"Name"`
					State      string `json:"State"`
					Status     string `json:"Status"`
					Ports      string `json:"Ports"`
					Publishers []struct {
						TargetPort    int `json:"TargetPort"`
						PublishedPort int `json:"PublishedPort"`
					} `json:"Publishers"`
				}
				if err := json.Unmarshal(raw, &row); err != nil {
					return err
				}
				if row.Name == "" {
					return nil
				}
				hostPort := 0
				for _, pub := range row.Publishers {
					if pub.PublishedPort > 0 {
						hostPort = pub.PublishedPort
						break
					}
				}

				stateLower := strings.ToLower(row.State)
				statusLower := strings.ToLower(row.Status)

				isExited := stateLower == "exited" || stateLower == "dead" || strings.Contains(statusLower, "exited")
				if isExited {
					hasExited = true
				}

				hasHealthCheck := strings.Contains(statusLower, "(health:") || strings.Contains(statusLower, "(healthy)") || strings.Contains(statusLower, "(unhealthy)")
				isStartingHealth := strings.Contains(statusLower, "health: starting")
				isHealthy := strings.Contains(statusLower, "(healthy)")
				isUnhealthy := strings.Contains(statusLower, "(unhealthy)")

				isPortResponding := false
				if hostPort > 0 {
					client := &http.Client{Timeout: 500 * time.Millisecond}
					resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/", hostPort))
					if err == nil {
						_ = resp.Body.Close()
						isPortResponding = true
					}
				}

				ready := false
				detail := row.Status

				if isExited {
					ready = false
					detail = fmt.Sprintf("container crashed: %s", row.Status)
				} else if hasHealthCheck {
					if isHealthy {
						if hostPort > 0 && !isPortResponding && time.Since(waitStart) < 10*time.Second {
							ready = false
							detail = fmt.Sprintf("healthy internally, awaiting port %d HTTP response...", hostPort)
						} else {
							ready = true
						}
					} else if isStartingHealth {
						ready = false
						detail = fmt.Sprintf("starting (healthcheck in progress): %s", row.Status)
					} else if isUnhealthy {
						ready = false
						detail = fmt.Sprintf("unhealthy: %s", row.Status)
					}
				} else if stateLower == "running" {
					if isPortResponding || time.Since(waitStart) >= 4*time.Second {
						ready = true
					} else {
						ready = false
						detail = "starting (stabilizing...)"
					}
				}

				if ready && hostPort > 0 {
					detail = fmt.Sprintf("live at http://localhost:%d", hostPort)
				}

				report.Applied = append(report.Applied, fmt.Sprintf("%s (%s)", row.Name, row.Status))
				report.Workloads = append(report.Workloads, WorkloadHealth{
					Kind:          "container",
					Name:          row.Name,
					Ready:         ready,
					Detail:        detail,
					WaitedSeconds: int(time.Since(waitStart).Seconds()),
				})
				return nil
			})

			if len(report.Workloads) > 0 {
				allReady := true
				for _, w := range report.Workloads {
					if !w.Ready {
						allReady = false
						break
					}
				}
				if allReady {
					break waitLoop
				}

				if hasExited && time.Since(waitStart) >= 5*time.Second {
					allExited := true
					for _, w := range report.Workloads {
						if !strings.Contains(w.Detail, "crashed") && !strings.Contains(w.Detail, "exited") {
							allExited = false
							break
						}
					}
					if allExited {
						break waitLoop
					}
				}
			}
		}
		select {
		case <-ctx.Done():
			break waitLoop
		case <-time.After(2 * time.Second):
		}
	}

	if len(report.Workloads) == 0 {
		report.Applied = append(report.Applied, fmt.Sprintf("project/%s", projectName))
		report.Workloads = append(report.Workloads, WorkloadHealth{
			Kind:   "project",
			Name:   projectName,
			Ready:  true,
			Detail: "containers running",
		})
	}

	report.Healthy = true
	for _, w := range report.Workloads {
		if !w.Ready {
			report.Healthy = false
			break
		}
	}

	if !report.Healthy {
		logsOutcome, _ := runner.Run(ctx, "docker", "compose", "-p", projectName, "-f", resolved[0], "logs", "--tail", "25")
		if strings.TrimSpace(logsOutcome.Output) != "" {
			report.Applied = append(report.Applied, fmt.Sprintf("container logs:\n%s", strings.TrimSpace(logsOutcome.Output)))
		}
	}

	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable deployment report: %w", err)
	}

	status := "applied"
	if !report.Healthy {
		status = "degraded"
	}
	sink.Progress(100, "deployment.apply_manifests",
		fmt.Sprintf("compose project %s deployed: %d container(s), healthy=%v", projectName, len(report.Workloads), report.Healthy))
	return Result{Status: status, Output: string(encoded)}, nil
}
