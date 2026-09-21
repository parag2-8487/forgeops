// SPDX-License-Identifier: Apache-2.0

// `deployment.apply_manifests` against a REAL Kubernetes cluster.
//
// WHY THIS FILE EXISTS, stated plainly because the gap it closes is the one this repository has been
// burned by most. `deployment_test.go` establishes the operation's DECISIONS — confinement, which kinds
// are waited on, the timeout clamp, how a verdict line is parsed — and says in its own header that it
// does not claim the operation has been run against Kubernetes. It had not been. Six validators once sat
// as `unimplemented(...)` while a criterion claimed they passed, and "the decision logic is tested" is
// the same shape of claim: it is true, and it is not the claim anybody cares about.
//
// WHAT ONLY A REAL CLUSTER CAN ESTABLISH:
//
//   - `kubectl apply` accepts what this code hands it. The argument vector is built by hand — no shell —
//     and an argument in the wrong position is invisible to a test that never runs the tool.
//   - The health half actually verifies. `rollout status` is the difference between "the API server
//     accepted the objects" and "anything is running", and a wrong invocation would report the first as
//     the second — a green deployment over a cluster with nothing running.
//   - `degraded` is REACHABLE. A workload that cannot converge must produce `Healthy: false` with the
//     workload named. Asserted here by deploying an image tag that cannot be pulled, which is the
//     commonest real failure and the one an operator most needs named.
//   - A ROLLBACK RESTORES. Re-applying the previous manifest set brings the workload back to Ready, and
//     the image the cluster reports afterwards is the previous one. Read back from the CLUSTER with
//     `kubectl get -o jsonpath`, not from this operation's own report: a report is this code's opinion,
//     and the whole point is to check the opinion against the object.
//
// Opt-in through `FORGEOPS_REAL_CLUSTER`, exactly as `internal/session/real_keychain_test.go` opts into
// the machine's real credential store, and for the same reason: a developer running `go test ./...` on a
// laptop has no cluster, and a suite that fails for the absence of infrastructure trains people to
// ignore it. `TestRealCluster_TheOptInIsNamedInCI` fails if the workflow stops setting it, so this file
// cannot quietly become dead code the way Q-19's did.
package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const realClusterEnv = "FORGEOPS_REAL_CLUSTER"

// realClusterImage is a real, published, tiny image with no registry credentials and no application
// behaviour to configure. `pause` is what the kubelet itself runs for every pod's network namespace, so
// it is present on a kind node's image set already and a rollout does not wait on a pull.
const realClusterImage = "registry.k8s.io/pause:3.9"

// realClusterBrokenImage is the same image at a tag that does not exist. The pull fails, the pod sits in
// ImagePullBackOff, and `rollout status` never reports success — the commonest real deployment failure.
const realClusterBrokenImage = "registry.k8s.io/pause:0.0.0-does-not-exist"

func requireRealCluster(t *testing.T) string {
	t.Helper()
	if os.Getenv(realClusterEnv) != "1" {
		t.Skipf("set %s=1 to apply manifests to a real cluster; the Kubernetes & SPIRE workflow does "+
			"this on every push, against the kind cluster it already stands up", realClusterEnv)
	}
	// A cluster that cannot be reached is a FAILURE here, not a skip. The opt-in is the statement that a
	// cluster is supposed to be present; discovering it is absent and passing anyway is how a green job
	// comes to mean nothing.
	if _, err := exec.LookPath("kubectl"); err != nil {
		t.Fatalf("%s=1 but kubectl is not on PATH: %v", realClusterEnv, err)
	}
	out, err := exec.Command("kubectl", "config", "current-context").CombinedOutput()
	if err != nil {
		t.Fatalf("%s=1 but kubectl reports no current context: %v — %s", realClusterEnv, err, out)
	}
	return strings.TrimSpace(string(out))
}

// kubectlOrFail runs kubectl for the test's own scaffolding — creating and deleting the namespace, and
// reading objects back. Deliberately separate from the production path: nothing here is what is under
// test, and routing setup through the operation would make the test its own witness.
func kubectlOrFail(t *testing.T, args ...string) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	out, err := exec.CommandContext(ctx, "kubectl", args...).CombinedOutput()
	if err != nil {
		t.Fatalf("kubectl %s: %v — %s", strings.Join(args, " "), err, out)
	}
	return strings.TrimSpace(string(out))
}

func writeManifest(t *testing.T, root, name, body string) string {
	t.Helper()
	abs := filepath.Join(root, name)
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		t.Fatalf("creating the manifest directory: %v", err)
	}
	if err := os.WriteFile(abs, []byte(body), 0o644); err != nil {
		t.Fatalf("writing %s: %v", name, err)
	}
	return name
}

func deploymentManifest(name, image string) string {
	return fmt.Sprintf(`apiVersion: apps/v1
kind: Deployment
metadata:
  name: %[1]s
  labels:
    app: %[1]s
spec:
  replicas: 1
  selector:
    matchLabels:
      app: %[1]s
  template:
    metadata:
      labels:
        app: %[1]s
    spec:
      containers:
        - name: app
          image: %[2]s
          resources:
            limits:
              cpu: 50m
              memory: 32Mi
`, name, image)
}

func serviceManifest(name string) string {
	return fmt.Sprintf(`apiVersion: v1
kind: Service
metadata:
  name: %[1]s
spec:
  selector:
    app: %[1]s
  ports:
    - port: 80
      targetPort: 80
`, name)
}

// runRealDeployment drives the PRODUCTION path: a signed envelope through `Execute`, so the dispatch
// row's approval requirement, the agent's own policy evaluation and the confinement all apply exactly as
// they do for a command from the backend.
func runRealDeployment(
	t *testing.T, root, namespace string, manifests []string, seq int64, timeoutSeconds int,
) (DeploymentReport, error) {
	t.Helper()
	d, err := New(Deps{Root: root})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	args := map[string]any{
		"deployment_id":          fmt.Sprintf("dep-real-%d", seq),
		"manifests":              manifests,
		"namespace":              namespace,
		"health_timeout_seconds": timeoutSeconds,
		"environment_name":       "ci",
	}
	// Generous: a real apply plus a real rollout wait, twice over in the degraded case.
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Minute)
	defer cancel()

	var progress []string
	result, execErr := d.Execute(ctx, verified(t, OpDeploymentApplyManifests, "approval-real", args, seq),
		SinkFunc(func(percent int, op, message string) {
			progress = append(progress, fmt.Sprintf("%d%% %s", percent, message))
		}))
	if execErr != nil {
		return DeploymentReport{}, execErr
	}
	// The progress sink must have spoken. A silent operation that takes minutes is indistinguishable
	// from a hung one on the screen that shows it.
	if len(progress) == 0 {
		t.Error("the operation reported no progress, so a deploying screen would have nothing to show")
	}

	// The report travels as the result's Output, which is what the backend parses — so this decodes the
	// same bytes the backend would, rather than reaching into a field only a test can see.
	var report DeploymentReport
	if err := json.Unmarshal([]byte(result.Output), &report); err != nil {
		t.Fatalf("decoding the deployment report from %q: %v", result.Output, err)
	}
	if result.Status == "" {
		t.Error("the result carries no status, so the backend cannot tell applied from degraded")
	}
	return report, nil
}

// TestRealCluster_AppliesVerifiesDegradesAndRollsBack is one test on purpose: the four facts are stages
// of a single history, and splitting them would either re-create the cluster state four times or make
// them order-dependent while pretending not to be.
func TestRealCluster_AppliesVerifiesDegradesAndRollsBack(t *testing.T) {
	clusterContext := requireRealCluster(t)
	t.Logf("cluster context: %s", clusterContext)

	namespace := fmt.Sprintf("forgeops-deploy-%d", time.Now().UnixNano()%1_000_000)
	kubectlOrFail(t, "create", "namespace", namespace)
	t.Cleanup(func() {
		// Best effort by nature — the cluster may already be gone — but the failure is REPORTED rather
		// than swallowed, because a leaked namespace changes the next run's result.
		out, err := exec.Command("kubectl", "delete", "namespace", namespace, "--wait=false").CombinedOutput()
		if err != nil {
			t.Logf("could not delete namespace %s: %v — %s", namespace, err, out)
		}
	})

	root := t.TempDir()
	const workload = "checkout-api"
	good := writeManifest(t, root, "k8s/deployment.yaml", deploymentManifest(workload, realClusterImage))
	svc := writeManifest(t, root, "k8s/service.yaml", serviceManifest(workload))
	broken := writeManifest(t, root, "k8s/deployment-broken.yaml",
		deploymentManifest(workload, realClusterBrokenImage))

	// ── 1. apply, and verify health for real ──
	report, err := runRealDeployment(t, root, namespace, []string{good, svc}, 1, 120)
	if err != nil {
		t.Fatalf("the first real deployment failed: %v", err)
	}
	if !report.Healthy {
		t.Fatalf("a deployment of a pullable image reported unhealthy: %+v", report.Workloads)
	}
	if len(report.Applied) != 2 {
		t.Errorf("applied %d object(s), want 2 (the Deployment and the Service): %v",
			len(report.Applied), report.Applied)
	}
	// THE DISTINCTION THE UNIT TEST COULD ONLY ASSERT ABOUT A MAP: exactly one workload was waited on,
	// and it is not the Service. `rollout status` on a Service exits non-zero, so if a Service were
	// waitable this deployment would have been reported degraded.
	if len(report.Workloads) != 1 {
		t.Fatalf("waited on %d workload(s), want 1 (the Deployment, not the Service): %+v",
			len(report.Workloads), report.Workloads)
	}
	if report.Workloads[0].Kind != "deployment" || report.Workloads[0].Name != workload {
		t.Errorf("waited on %s/%s, want deployment/%s",
			report.Workloads[0].Kind, report.Workloads[0].Name, workload)
	}
	if !report.Workloads[0].Ready {
		t.Errorf("the workload was not ready but the deployment reported healthy: %+v", report.Workloads[0])
	}
	if report.KubectlVersion == "" {
		t.Error("the report names no kubectl version, so 'which client applied this' is unanswerable")
	}

	// READ BACK FROM THE CLUSTER, not from the report. Everything above is this code's account of what
	// happened; this is the object.
	replicas := kubectlOrFail(t, "-n", namespace, "get", "deployment", workload,
		"-o", "jsonpath={.status.readyReplicas}")
	if replicas != "1" {
		t.Errorf("the cluster reports %q ready replicas after a healthy deployment, want 1", replicas)
	}
	if got := kubectlOrFail(t, "-n", namespace, "get", "deployment", workload,
		"-o", "jsonpath={.spec.template.spec.containers[0].image}"); got != realClusterImage {
		t.Errorf("the cluster runs image %q, want %q", got, realClusterImage)
	}
	// The Service was applied, not merely reported as applied.
	kubectlOrFail(t, "-n", namespace, "get", "service", workload)

	// ── 2. degraded is reachable, and names the workload ──
	// 45 seconds: long enough for the pull to be attempted and fail, short enough to keep this test in
	// the same minute. The operation clamps but does not extend.
	degraded, err := runRealDeployment(t, root, namespace, []string{broken}, 2, 45)
	if err != nil {
		t.Fatalf("the degraded deployment returned an error instead of a degraded report: %v", err)
	}
	if degraded.Healthy {
		t.Fatalf("a workload that cannot pull its image was reported healthy: %+v", degraded.Workloads)
	}
	if len(degraded.Workloads) != 1 || degraded.Workloads[0].Ready {
		t.Fatalf("the degraded report does not name one unready workload: %+v", degraded.Workloads)
	}
	if !strings.Contains(degraded.Workloads[0].Detail, workload) &&
		!strings.Contains(strings.ToLower(degraded.Workloads[0].Detail), "timed out") {
		t.Errorf("the degraded detail explains nothing an operator can act on: %q",
			degraded.Workloads[0].Detail)
	}
	// The APPLY still succeeded — the objects are in the cluster, and that is a different fact from
	// health. An operation that reported the apply as failed would send an operator looking for a
	// rejected manifest rather than a failing pull.
	if len(degraded.Applied) != 1 {
		t.Errorf("the degraded deployment applied %v, want the one Deployment", degraded.Applied)
	}

	// ── 3. the rollback restores the previous stable state ──
	// Through the SAME operation, because that is what the backend's rollback does: it re-deploys the
	// stable target's manifests. There is no separate rollback authority to audit, and there must not be.
	restored, err := runRealDeployment(t, root, namespace, []string{good, svc}, 3, 120)
	if err != nil {
		t.Fatalf("the rollback deployment failed: %v", err)
	}
	if !restored.Healthy {
		t.Fatalf("the rollback did not restore a healthy state: %+v", restored.Workloads)
	}
	if got := kubectlOrFail(t, "-n", namespace, "get", "deployment", workload,
		"-o", "jsonpath={.spec.template.spec.containers[0].image}"); got != realClusterImage {
		t.Errorf("after the rollback the cluster runs %q, want the previous image %q", got, realClusterImage)
	}
	if got := kubectlOrFail(t, "-n", namespace, "get", "deployment", workload,
		"-o", "jsonpath={.status.readyReplicas}"); got != "1" {
		t.Errorf("after the rollback the cluster reports %q ready replicas, want 1", got)
	}
}

// TestRealCluster_ConfinementHoldsAgainstARealCluster is the one refusal worth re-asserting here. The
// unit test proves the path is refused; this proves the refusal happens BEFORE kubectl is invoked, with a
// reachable cluster sitting there ready to accept whatever it is handed.
func TestRealCluster_ConfinementHoldsAgainstARealCluster(t *testing.T) {
	requireRealCluster(t)

	root := t.TempDir()
	outside := filepath.Join(t.TempDir(), "escaped.yaml")
	if err := os.WriteFile(outside, []byte(deploymentManifest("escaped", realClusterImage)), 0o644); err != nil {
		t.Fatalf("writing the outside manifest: %v", err)
	}

	_, err := runRealDeployment(t, root, "default",
		[]string{filepath.Join("..", filepath.Base(filepath.Dir(outside)), "escaped.yaml")}, 4, 30)
	if err == nil {
		t.Fatal("a manifest outside the workspace was applied to a real cluster")
	}
	if Code(err) == "" {
		t.Errorf("the refusal carries no code, so the backend cannot classify it: %v", err)
	}
}

// TestRealCluster_TheOptInIsNamedInCI keeps this file from becoming dead code.
//
// The failure it guards against is specific and has happened here before: an opt-in suite whose opt-in
// quietly left CI passes locally as a skip, forever, and its absence is invisible. Reading the workflow
// is the only way to assert the other half.
func TestRealCluster_TheOptInIsNamedInCI(t *testing.T) {
	path := filepath.Join("..", "..", "..", ".github", "workflows", "k8s-ci.yml")
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v", path, err)
	}
	workflow := string(body)
	if !strings.Contains(workflow, realClusterEnv+": \"1\"") {
		t.Errorf("%s does not set %s=\"1\", so the real-cluster deployment is never exercised anywhere",
			path, realClusterEnv)
	}
	if !strings.Contains(workflow, "TestRealCluster") {
		t.Errorf("%s does not run TestRealCluster, so the opt-in is set and nothing uses it", path)
	}
}
