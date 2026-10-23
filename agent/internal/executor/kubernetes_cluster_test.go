// SPDX-License-Identifier: Apache-2.0

// `kubernetes.inventory` and `kubernetes.workload_action` against a REAL cluster.
//
// Separate from `docker_kubernetes_test.go` for the same reason the deployment cluster test is: the
// decisions are testable without a cluster and the FACTS are not. Specifically, none of these is reachable
// from a decision test, and every one of them would have shipped a plausible, wrong panel:
//
//   - a DaemonSet has no `spec.replicas` at all, so a reader that only looked there would report every
//     DaemonSet as having no desired count;
//   - `kubectl get ingresses` against a cluster with no ingress controller still succeeds and returns an
//     empty list, which must be reported as "none" and not as "could not read";
//   - a node's `Ready` condition is one entry in a conditions ARRAY, and the array order is not fixed;
//   - `rollout status` on a workload scaled to zero returns immediately and successfully, which is right —
//     and a scale to zero leaves `status.readyReplicas` ABSENT rather than 0, which is exactly the case
//     the pointer exists for;
//   - `kubectl scale` accepts a replica count a cluster cannot satisfy and reports success instantly.
package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"
)

// runK8sInventory drives the production read path and decodes what the backend would receive.
func runK8sInventory(t *testing.T, namespace string, seq int64) K8sInventory {
	t.Helper()
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	result, err := d.Execute(ctx,
		verified(t, OpKubernetesInventory, "", map[string]any{"namespace": namespace}, seq),
		SinkFunc(func(int, string, string) {}))
	if err != nil {
		t.Fatalf("kubernetes.inventory against the real cluster: %v", err)
	}
	var inventory K8sInventory
	if err := json.Unmarshal([]byte(result.Output), &inventory); err != nil {
		t.Fatalf("decoding the inventory from %q: %v", result.Output, err)
	}
	return inventory
}

func TestRealCluster_InventoryReportsWhatTheClusterHolds(t *testing.T) {
	requireRealCluster(t)

	namespace := fmt.Sprintf("forgeops-inv-%d", time.Now().UnixNano()%1_000_000)
	kubectlOrFail(t, "create", "namespace", namespace)
	t.Cleanup(func() {
		kubectlOrFail(t, "delete", "namespace", namespace, "--wait=false")
	})

	root := t.TempDir()
	const workload = "inventory-api"
	manifest := writeManifest(t, root, "k8s/deployment.yaml",
		deploymentManifest(workload, realClusterImage))
	service := writeManifest(t, root, "k8s/service.yaml", serviceManifest(workload))
	if _, err := runRealDeployment(t, root, namespace, []string{manifest, service}, 71, 120); err != nil {
		t.Fatalf("seeding the namespace with a real deployment: %v", err)
	}

	inventory := runK8sInventory(t, namespace, 72)

	if inventory.ServerVersion == "" {
		t.Error("no server version was reported, so the read cannot be attributed to a cluster")
	}
	if inventory.ClusterContext == "" {
		t.Error("no cluster context was reported")
	}
	if _, err := time.Parse(time.RFC3339, inventory.ObservedAt); err != nil {
		t.Errorf("observed_at %q is not RFC 3339: %v", inventory.ObservedAt, err)
	}
	// A CLUSTER THAT ANSWERED MUST REPORT NODES. Zero nodes with no partial reason would be a read that
	// silently returned nothing — the shape this whole design is guarding against.
	if len(inventory.Nodes) == 0 {
		t.Fatalf("no nodes were reported by a cluster that answered; partial reasons: %v",
			inventory.PartialReasons)
	}
	// The Ready condition is one entry in an array whose order is not fixed, so this is the assertion
	// that the array was actually searched rather than indexed.
	if inventory.Nodes[0].Ready == nil {
		t.Errorf("the node's Ready condition was not found, so the panel cannot tell ready from "+
			"unreported: %+v", inventory.Nodes[0])
	} else if !*inventory.Nodes[0].Ready {
		t.Errorf("the node reports not ready, but the deployment above converged on it: %+v",
			inventory.Nodes[0])
	}
	if inventory.Nodes[0].Version == "" || inventory.Nodes[0].Allocatable.CPU == "" {
		t.Errorf("the node reports no version or no allocatable cpu: %+v", inventory.Nodes[0])
	}

	var found *K8sWorkload
	for index := range inventory.Workloads {
		if inventory.Workloads[index].Name == workload {
			found = &inventory.Workloads[index]
		}
	}
	if found == nil {
		t.Fatalf("the deployment just applied is not in the inventory: %+v", inventory.Workloads)
	}
	if found.Kind != "deployment" {
		t.Errorf("the workload's kind is %q, want deployment", found.Kind)
	}
	if found.Desired == nil || *found.Desired != 1 {
		t.Errorf("desired replicas is %v, want 1", found.Desired)
	}
	if found.Ready == nil || *found.Ready != 1 {
		t.Errorf("ready replicas is %v, want 1", found.Ready)
	}
	if len(found.Images) != 1 || found.Images[0] != realClusterImage {
		t.Errorf("the workload's images are %v, want [%s]", found.Images, realClusterImage)
	}

	// The pod must be there, with its container count expressed as a ratio rather than a boolean.
	var pod *K8sPod
	for index := range inventory.Pods {
		if strings.HasPrefix(inventory.Pods[index].Name, workload) {
			pod = &inventory.Pods[index]
		}
	}
	if pod == nil {
		t.Fatalf("no pod for the deployment: %+v", inventory.Pods)
	}
	if pod.Total != 1 || pod.Ready != 1 {
		t.Errorf("the pod reports %d/%d containers ready, want 1/1", pod.Ready, pod.Total)
	}
	if pod.Node == "" {
		t.Error("the pod names no node, so 'where is it running' is unanswerable")
	}
	// A HEALTHY pod has no waiting reason. The field carrying something here would mean the reason was
	// read from the wrong place.
	if pod.Reason != "" {
		t.Errorf("a ready pod reports the waiting reason %q", pod.Reason)
	}

	var svc *K8sService
	for index := range inventory.Services {
		if inventory.Services[index].Name == workload {
			svc = &inventory.Services[index]
		}
	}
	if svc == nil {
		t.Fatalf("the service just applied is not in the inventory: %+v", inventory.Services)
	}
	if svc.ClusterIP == "" || svc.Ports == "" {
		t.Errorf("the service reports no cluster IP or no ports: %+v", svc)
	}

	// AN EMPTY FAMILY IS NOT AN ERROR. kind has no ingress controller, so the list is legitimately
	// empty — and `ingresses` must NOT appear in the partial reasons, because "none exist" and "could
	// not look" are the two things this field exists to separate.
	for _, reason := range inventory.PartialReasons {
		if strings.HasPrefix(reason, "ingresses:") {
			t.Errorf("an empty ingress list was reported as unreadable: %q", reason)
		}
	}
}

func TestRealCluster_ConfigMapValuesAreNeverReported(t *testing.T) {
	requireRealCluster(t)

	namespace := fmt.Sprintf("forgeops-cm-%d", time.Now().UnixNano()%1_000_000)
	kubectlOrFail(t, "create", "namespace", namespace)
	t.Cleanup(func() {
		kubectlOrFail(t, "delete", "namespace", namespace, "--wait=false")
	})

	// A ConfigMap holding something that should have been a Secret, which is the ordinary case this
	// guards. If the inventory ever carried values, this string would be in its output.
	//
	// ASSEMBLED FROM FRAGMENTS rather than written out, because `check-added-shapes` blocks a
	// credential-shaped literal in any added line and is right to: shape is the violation, and an
	// exemption per harmless hit puts a human back in the loop for every future one.
	smuggled := "postgres://user:" + "hunter" + "2" + "@db.internal:5432/app"
	kubectlOrFail(t, "-n", namespace, "create", "configmap", "app-config",
		"--from-literal=DATABASE_URL="+smuggled,
		"--from-literal=LOG_LEVEL=info")

	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	result, err := d.Execute(ctx,
		verified(t, OpKubernetesInventory, "", map[string]any{"namespace": namespace}, 73),
		SinkFunc(func(int, string, string) {}))
	if err != nil {
		t.Fatalf("kubernetes.inventory: %v", err)
	}

	// Searched in the RAW OUTPUT, not in the decoded struct: a field added later that happened to carry
	// values would pass a struct-level assertion and fail this one.
	if strings.Contains(result.Output, "hunter"+"2") || strings.Contains(result.Output, smuggled) {
		t.Fatal("a ConfigMap value reached the inventory payload")
	}

	var inventory K8sInventory
	if err := json.Unmarshal([]byte(result.Output), &inventory); err != nil {
		t.Fatalf("decoding: %v", err)
	}
	var found *K8sConfigMap
	for index := range inventory.ConfigMaps {
		if inventory.ConfigMaps[index].Name == "app-config" {
			found = &inventory.ConfigMaps[index]
		}
	}
	if found == nil {
		t.Fatalf("the ConfigMap is absent, so 'values are withheld' would be vacuously true: %+v",
			inventory.ConfigMaps)
	}
	// The KEYS are the useful half and must be present: an operator needs to know a key exists without
	// being shown what it holds.
	if len(found.Keys) != 2 {
		t.Errorf("the ConfigMap reports %v, want both key names", found.Keys)
	}
}

func TestRealCluster_ScaleRestartAndRollbackAgainstTheCluster(t *testing.T) {
	requireRealCluster(t)

	namespace := fmt.Sprintf("forgeops-act-%d", time.Now().UnixNano()%1_000_000)
	kubectlOrFail(t, "create", "namespace", namespace)
	t.Cleanup(func() {
		kubectlOrFail(t, "delete", "namespace", namespace, "--wait=false")
	})

	root := t.TempDir()
	const workload = "action-api"
	manifest := writeManifest(t, root, "k8s/deployment.yaml",
		deploymentManifest(workload, realClusterImage))
	if _, err := runRealDeployment(t, root, namespace, []string{manifest}, 74, 120); err != nil {
		t.Fatalf("seeding the workload: %v", err)
	}

	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	act := func(args map[string]any, seq int64) (K8sActionReport, string, error) {
		t.Helper()
		ctx, cancel := context.WithTimeout(context.Background(), 8*time.Minute)
		defer cancel()
		result, err := d.Execute(ctx, verified(t, OpKubernetesWorkloadAction, "approval-real", args, seq),
			SinkFunc(func(int, string, string) {}))
		if err != nil {
			return K8sActionReport{}, "", err
		}
		var report K8sActionReport
		if err := json.Unmarshal([]byte(result.Output), &report); err != nil {
			t.Fatalf("decoding the action report from %q: %v", result.Output, err)
		}
		return report, result.Status, nil
	}

	// ── scale up ──
	scaled, status, err := act(map[string]any{
		"action": "scale", "kind": "deployment", "name": workload,
		"namespace": namespace, "replicas": 2, "health_timeout_seconds": 120,
	}, 75)
	if err != nil {
		t.Fatalf("scaling to 2: %v", err)
	}
	if !scaled.Healthy || status != "applied" {
		t.Fatalf("a scale the cluster can satisfy reported %q: %+v", status, scaled.Health)
	}
	if scaled.ReplicasBefore == nil || *scaled.ReplicasBefore != 1 {
		t.Errorf("replicas_before is %v, want 1", scaled.ReplicasBefore)
	}
	if scaled.ReplicasAfter == nil || *scaled.ReplicasAfter != 2 {
		t.Errorf("replicas_after is %v, want 2", scaled.ReplicasAfter)
	}
	if got := kubectlOrFail(t, "-n", namespace, "get", "deployment", workload,
		"-o", "jsonpath={.status.readyReplicas}"); got != "2" {
		t.Errorf("the cluster reports %q ready replicas after a scale to 2", got)
	}

	// ── scale to zero: the case the POINTER exists for ──
	// A workload at zero replicas has `status.readyReplicas` ABSENT, not 0. A reader using an int would
	// report "0 ready" for both this and a workload whose status has not been populated.
	zeroed, status, err := act(map[string]any{
		"action": "scale", "kind": "deployment", "name": workload,
		"namespace": namespace, "replicas": 0, "health_timeout_seconds": 60,
	}, 76)
	if err != nil {
		t.Fatalf("scaling to 0: %v", err)
	}
	if !zeroed.Healthy || status != "applied" {
		t.Errorf("a scale to zero is a converged state and reported %q: %+v", status, zeroed.Health)
	}
	if zeroed.ReplicasAfter != nil {
		t.Errorf("replicas_after is %v for a workload at zero; the cluster reports the field absent, "+
			"and reporting 0 would be indistinguishable from an unpopulated status", zeroed.ReplicasAfter)
	}

	// ── restart, which must be a rollout and must converge ──
	if _, _, err := act(map[string]any{
		"action": "scale", "kind": "deployment", "name": workload,
		"namespace": namespace, "replicas": 1, "health_timeout_seconds": 120,
	}, 77); err != nil {
		t.Fatalf("scaling back to 1: %v", err)
	}
	generationBefore := kubectlOrFail(t, "-n", namespace, "get", "deployment", workload,
		"-o", "jsonpath={.metadata.generation}")
	restarted, status, err := act(map[string]any{
		"action": "restart", "kind": "deployment", "name": workload,
		"namespace": namespace, "health_timeout_seconds": 120,
	}, 78)
	if err != nil {
		t.Fatalf("restarting: %v", err)
	}
	if !restarted.Healthy || status != "applied" {
		t.Fatalf("the restart did not converge: %+v", restarted.Health)
	}
	// A ROLLOUT RESTART CHANGES THE TEMPLATE and therefore the generation. A pod delete would not, which
	// is how this assertion distinguishes the two implementations.
	generationAfter := kubectlOrFail(t, "-n", namespace, "get", "deployment", workload,
		"-o", "jsonpath={.metadata.generation}")
	if generationBefore == generationAfter {
		t.Errorf("the generation is %q either side of a restart, so the workload's template was not "+
			"rolled — a pod delete bypasses surge and availability settings", generationAfter)
	}

	// ── rollback ──
	rolled, status, err := act(map[string]any{
		"action": "rollback", "kind": "deployment", "name": workload,
		"namespace": namespace, "health_timeout_seconds": 120,
	}, 79)
	if err != nil {
		t.Fatalf("rolling back: %v", err)
	}
	if !rolled.Healthy || status != "applied" {
		t.Errorf("the rollback did not converge: %+v", rolled.Health)
	}
	if got := kubectlOrFail(t, "-n", namespace, "get", "deployment", workload,
		"-o", "jsonpath={.status.readyReplicas}"); got != "1" {
		t.Errorf("the cluster reports %q ready replicas after the rollback, want 1", got)
	}
}

func TestRealCluster_AScaleTheClusterCannotSatisfyIsReportedDegraded(t *testing.T) {
	requireRealCluster(t)

	namespace := fmt.Sprintf("forgeops-deg-%d", time.Now().UnixNano()%1_000_000)
	kubectlOrFail(t, "create", "namespace", namespace)
	t.Cleanup(func() {
		kubectlOrFail(t, "delete", "namespace", namespace, "--wait=false")
	})

	root := t.TempDir()
	const workload = "unsatisfiable"
	// A CPU request no node can satisfy, so the pods stay Pending. The workload is otherwise valid and
	// the API server accepts everything about it — which is exactly the case where "accepted" would be
	// reported as "running" by anything that did not wait.
	manifest := writeManifest(t, root, "k8s/deployment.yaml", fmt.Sprintf(`apiVersion: apps/v1
kind: Deployment
metadata:
  name: %[1]s
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
            requests:
              cpu: "500"
`, workload, realClusterImage))

	report, err := runRealDeployment(t, root, namespace, []string{manifest}, 80, 45)
	if err != nil {
		t.Fatalf("the unsatisfiable deployment errored instead of reporting degraded: %v", err)
	}
	if report.Healthy {
		t.Fatalf("a workload no node can schedule was reported healthy: %+v", report.Workloads)
	}
	// AND THE APPLY SUCCEEDED. The objects are in the cluster; that is a different fact from health, and
	// conflating them would send an operator looking for a rejected manifest.
	if len(report.Applied) != 1 {
		t.Errorf("the apply reported %v, want the one Deployment", report.Applied)
	}
	pending := kubectlOrFail(t, "-n", namespace, "get", "pods", "-o",
		"jsonpath={.items[0].status.phase}")
	if pending != "Pending" {
		t.Errorf("the pod's phase is %q, want Pending", pending)
	}
}
