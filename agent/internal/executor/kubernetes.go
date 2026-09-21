// SPDX-License-Identifier: Apache-2.0

// Kubernetes: one read-only inventory operation and one mutating workload-action operation.
//
// The same two reasons as `docker.go`, and one more that is specific to clusters.
//
// WHY `kubectl` AND NOT client-go. client-go would pull a very large dependency tree into a binary that
// ships to operators, and it would need its own kubeconfig, context, exec-credential-plugin and OIDC
// handling — the four things that make a cluster reachable in practice. `kubectl` already has all of them
// and is already required by `deployment.apply_manifests`. The property this preserves is the one that
// matters: the agent reaches exactly the clusters the operator can reach, with the operator's own
// credentials, and no signed envelope can widen that.
//
// WHY SCALE, RESTART AND ROLLBACK ARE ONE OPERATION. They are the same authority — "may change how a
// named workload is running" — over the same named target, and each is bounded and reversible in the same
// way. What is deliberately NOT in this file is the authority a fourth verb would add: there is no
// delete, no namespace-wide selector, no `--all`, and no way to name a resource kind outside the workload
// set. A dashboard that could scale can therefore not be used to remove anything.
//
// WHY THE INVENTORY REPORTS COUNTS THE WAY IT DOES. Every count is a POINTER or carries an explicit
// observed-at, because the panel above it has to distinguish three states that a bare integer collapses
// into one: never reported, reported a while ago, reported now. A pod count of 0 from a cluster that
// answered and a pod count of 0 because the API server refused are the same number and opposite facts.
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

var (
	// ErrClusterUnreachable separates "kubectl is absent" (ErrKubectlMissing) from "no cluster
	// answered". The remedies differ: install a tool versus fix a context or a VPN.
	ErrClusterUnreachable = errors.New("executor: no Kubernetes cluster answered")
	// ErrUnknownWorkloadAction refuses an action outside the closed set by name.
	ErrUnknownWorkloadAction = errors.New("executor: that workload action is not in the closed set")
	// ErrReplicasOutOfRange refuses a scale that is negative or larger than the bound.
	ErrReplicasOutOfRange = errors.New("executor: the requested replica count is outside the allowed range")
)

// MaxScaleReplicas bounds a scale.
//
// DERIVED, not picked: this platform's own fleet target is 10,000 agents across all tenants, and a single
// workload asking for more replicas than a small cluster has capacity for is not a scaling decision, it is
// a mis-assembled envelope or a fat finger. 100 is above anything the dashboard offers and far below the
// point where a cluster falls over answering it. The refusal names the bound so it is not a mystery.
const MaxScaleReplicas = 100

// workloadActions is the closed set of things that may be done to a running workload.
var workloadActions = map[string]struct{}{
	"scale":    {},
	"restart":  {},
	"rollback": {},
}

// k8sInventoryArgs selects the namespace. Empty means every namespace the operator can see, which is what
// a cluster overview needs; a named one is what a project view needs.
type k8sInventoryArgs struct {
	Namespace string `json:"namespace,omitempty"`
	Context   string `json:"context,omitempty"`
}

// K8sPod is one pod as the dashboard shows it.
type K8sPod struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	Phase     string `json:"phase"`
	// Ready and Total are container counts, so "1/2 ready" is expressible. A boolean would lose the
	// difference between a pod starting and a pod with one broken sidecar.
	Ready int `json:"ready_containers"`
	Total int `json:"total_containers"`
	// Restarts is the sum across containers: the single most diagnostic number for a crash loop.
	Restarts int    `json:"restarts"`
	Node     string `json:"node"`
	// Reason carries the container's waiting reason — `ImagePullBackOff`, `CrashLoopBackOff` — which is
	// what an operator actually acts on. Empty when the pod is not waiting on anything.
	Reason    string `json:"reason"`
	StartedAt string `json:"started_at"`
}

// K8sWorkload is a Deployment, StatefulSet or DaemonSet.
type K8sWorkload struct {
	Namespace string `json:"namespace"`
	Kind      string `json:"kind"`
	Name      string `json:"name"`
	// Desired and Ready are pointers: a workload whose status the API server has not populated yet is
	// not a workload with zero ready replicas.
	Desired *int     `json:"desired_replicas"`
	Ready   *int     `json:"ready_replicas"`
	Images  []string `json:"images"`
}

// K8sService is one service.
type K8sService struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	Type      string `json:"type"`
	ClusterIP string `json:"cluster_ip"`
	Ports     string `json:"ports"`
}

// K8sIngress is one ingress, reported by the hosts it answers on.
type K8sIngress struct {
	Namespace string   `json:"namespace"`
	Name      string   `json:"name"`
	Hosts     []string `json:"hosts"`
	Class     string   `json:"class"`
}

// K8sConfigMap is a ConfigMap's identity and key names — never its values. A ConfigMap frequently holds
// something that should have been a Secret, and a dashboard that printed values would publish it.
type K8sConfigMap struct {
	Namespace string   `json:"namespace"`
	Name      string   `json:"name"`
	Keys      []string `json:"keys"`
}

// K8sHPA is one HorizontalPodAutoscaler.
type K8sHPA struct {
	Namespace   string `json:"namespace"`
	Name        string `json:"name"`
	Target      string `json:"target"`
	MinReplicas *int   `json:"min_replicas"`
	MaxReplicas *int   `json:"max_replicas"`
	Current     *int   `json:"current_replicas"`
}

// K8sNode is one node and whether it is Ready.
type K8sNode struct {
	Name string `json:"name"`
	// Ready is a POINTER because a node whose Ready condition is absent is not a node that is not
	// ready. The tri-state is the whole contract of this dashboard.
	Ready       *bool  `json:"ready"`
	Version     string `json:"kubelet_version"`
	OSImage     string `json:"os_image"`
	Allocatable struct {
		CPU    string `json:"cpu"`
		Memory string `json:"memory"`
	} `json:"allocatable"`
}

// K8sInventory is the whole read.
type K8sInventory struct {
	ClusterContext string         `json:"cluster_context"`
	ServerVersion  string         `json:"server_version"`
	Namespaces     []string       `json:"namespaces"`
	Nodes          []K8sNode      `json:"nodes"`
	Pods           []K8sPod       `json:"pods"`
	Workloads      []K8sWorkload  `json:"workloads"`
	Services       []K8sService   `json:"services"`
	Ingresses      []K8sIngress   `json:"ingresses"`
	ConfigMaps     []K8sConfigMap `json:"config_maps"`
	HPAs           []K8sHPA       `json:"horizontal_pod_autoscalers"`
	// ObservedAt is taken on the host that read the cluster, in RFC 3339.
	ObservedAt string `json:"observed_at"`
	// PartialReasons names every family that could not be read, by name and cause. A dashboard needs
	// this to grey out one panel instead of implying the cluster has no ingresses when it has no
	// PERMISSION to list them — the difference between "none" and "not allowed to look".
	PartialReasons []string `json:"partial_reasons"`
}

// k8sWorkloadActionArgs is one action over one named workload.
type k8sWorkloadActionArgs struct {
	Action    string `json:"action"`
	Namespace string `json:"namespace"`
	// Kind is one of the three pod-bearing kinds. Anything else is refused, which is what keeps this
	// from becoming a general-purpose kubectl.
	Kind string `json:"kind"`
	Name string `json:"name"`
	// Replicas applies to `scale` only.
	Replicas int `json:"replicas,omitempty"`
	// ToRevision applies to `rollback` only. Zero means the previous revision, which is what
	// `kubectl rollout undo` does with no flag.
	ToRevision int    `json:"to_revision,omitempty"`
	Context    string `json:"context,omitempty"`
	// HealthTimeoutSeconds bounds the wait for the workload to converge after the action. The action is
	// not finished when the API server accepts it, for the same reason a deployment is not.
	HealthTimeoutSeconds int `json:"health_timeout_seconds,omitempty"`
}

// K8sActionReport is what a mutating workload operation reports.
type K8sActionReport struct {
	Action    string `json:"action"`
	Namespace string `json:"namespace"`
	Kind      string `json:"kind"`
	Name      string `json:"name"`
	// ReplicasBefore and ReplicasAfter are read from the cluster around the action, so the audit row
	// carries what changed rather than what was asked for.
	ReplicasBefore *int `json:"replicas_before"`
	ReplicasAfter  *int `json:"replicas_after"`
	// Health is the same structure `deployment.apply_manifests` reports, because it is the same
	// question: did the thing converge. A scale that the API server accepted and the cluster could not
	// satisfy is the case this exists to name.
	Health         WorkloadHealth `json:"health"`
	Healthy        bool           `json:"healthy"`
	ClusterContext string         `json:"cluster_context"`
	ObservedAt     string         `json:"observed_at"`
}

// kubectlRunner resolves the tool and proves a cluster answers before anything else runs.
func kubectlRunner(ctx context.Context, dir, kubeContext string) (*validator.Runner, []string, string, error) {
	runner := &validator.Runner{Dir: dir}
	if _, err := runner.Look("kubectl"); err != nil {
		return nil, nil, "", fmt.Errorf("%w: %w", ErrKubectlMissing, err)
	}
	base := []string{}
	if kubeContext != "" {
		base = append(base, "--context", kubeContext)
	}
	// `version` against the SERVER, because that is the call that fails when there is no cluster.
	// `--client=true` would succeed with no cluster at all and prove nothing.
	outcome, err := runner.Run(ctx, "kubectl", append(append([]string{}, base...),
		"version", "-o", "json")...)
	if err != nil || !outcome.Passed {
		return nil, nil, "", fmt.Errorf("%w: %s", ErrClusterUnreachable, firstLine(outcome.Output))
	}
	var payload struct {
		ServerVersion struct {
			GitVersion string `json:"gitVersion"`
		} `json:"serverVersion"`
	}
	// THE OUTPUT IS NOT NECESSARILY JSON, and a real cluster is how that was discovered. kubectl writes
	// diagnostics to stderr — "Warning: version difference between client (1.36) and server (1.32)
	// exceeds the supported minor version skew" is the common one — and the runner merges the two
	// streams, so the buffer is a warning line followed by the document. Unmarshalling the whole buffer
	// fails, and the first version of this code ignored that failure, left the version empty, and
	// refused every inventory on a cluster whose version differed from the operator's kubectl. A skew
	// warning is not an unreachable cluster.
	//
	// Decoding from the first brace rather than stripping known prefixes: the set of things kubectl
	// might print is not a list this code can keep current.
	if decodeErr := decodeFirstJSON(outcome.Output, &payload); decodeErr != nil {
		return nil, nil, "", fmt.Errorf("%w: kubectl's version output was unreadable (%v): %s",
			ErrClusterUnreachable, decodeErr, firstLine(outcome.Output))
	}
	if payload.ServerVersion.GitVersion == "" {
		return nil, nil, "", fmt.Errorf("%w: kubectl reported no server version", ErrClusterUnreachable)
	}
	return runner, base, payload.ServerVersion.GitVersion, nil
}

// decodeFirstJSON decodes the FIRST complete JSON value in a buffer that may carry tool diagnostics
// before it, after it, or both.
//
// WHY A DECODER AND NOT A TRIM, recorded because the first two attempts were both wrong and a real cluster
// is what exposed each. kubectl writes warnings to stderr — "version difference between client (1.36) and
// server (1.32) exceeds the supported minor version skew" is the everyday one — and the runner merges the
// streams. The first version unmarshalled the whole buffer, ignored the failure, and refused every read on
// a cluster whose version differed from the operator's client: a warning became an unreachable cluster.
// The second trimmed to the first brace, which handles a warning BEFORE the document and fails with
// "invalid character 'W' after top-level value" when the warning lands after it, which is what actually
// happens when stdout is flushed first.
//
// A streaming decoder reads one value and stops, so neither position matters and no list of known prefixes
// has to be kept current. The decode error is RETURNED rather than swallowed, so a buffer that genuinely
// holds no document is reported with what the tool said.
func decodeFirstJSON(output string, target any) error {
	start := strings.IndexAny(output, "{[")
	if start < 0 {
		return fmt.Errorf("no JSON document in %q", firstLine(output))
	}
	return json.NewDecoder(strings.NewReader(output[start:])).Decode(target)
}

// getJSON runs a `kubectl get -o json` and returns the raw document for the caller to decode.
//
// A failure is returned rather than swallowed so the caller can record WHICH family was unreadable and
// why: "this cluster has no ingresses" and "this principal may not list ingresses" are opposite facts.
func getJSON(ctx context.Context, runner *validator.Runner, base []string, args ...string) (string, error) {
	full := append(append([]string{"get"}, base...), args...)
	full = append(full, "-o", "json")
	outcome, err := runner.Run(ctx, "kubectl", full...)
	if err != nil || !outcome.Passed {
		return "", fmt.Errorf("%w — %s", errOrRefused(err), firstLine(outcome.Output))
	}
	return outcome.Output, nil
}

// scopeArgs turns a namespace into kubectl's scope flags. Empty means every namespace.
func scopeArgs(namespace string) []string {
	if namespace == "" {
		return []string{"--all-namespaces"}
	}
	return []string{"--namespace", namespace}
}

func intPtr(v int) *int { return &v }

// k8sInventory is the read-only handler.
//
// Each family is read independently and a failure in one is RECORDED rather than fatal. A cluster where
// the operator may list pods but not ingresses is ordinary, and refusing the whole read would make the
// dashboard useless for them; reporting "ingresses: forbidden" tells them exactly what they are missing.
func k8sInventory(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args k8sInventoryArgs
	if len(v.Args()) > 0 {
		if err := json.Unmarshal(v.Args(), &args); err != nil {
			return Result{}, fmt.Errorf("%w: kubernetes inventory arguments: %v", ErrBadArgs, err)
		}
	}

	runner, base, serverVersion, err := kubectlRunner(ctx, d.root, args.Context)
	if err != nil {
		return Result{}, err
	}

	clusterContext := args.Context
	if clusterContext == "" {
		if outcome, ctxErr := runner.Run(ctx, "kubectl", "config", "current-context"); ctxErr == nil {
			clusterContext = strings.TrimSpace(outcome.Output)
		}
		if clusterContext == "" {
			clusterContext = "unknown (kubectl reported no current context)"
		}
	}

	inventory := K8sInventory{
		ClusterContext: clusterContext,
		ServerVersion:  serverVersion,
		ObservedAt:     d.now().UTC().Format(time.RFC3339),
		Namespaces:     []string{},
		Nodes:          []K8sNode{},
		Pods:           []K8sPod{},
		Workloads:      []K8sWorkload{},
		Services:       []K8sService{},
		Ingresses:      []K8sIngress{},
		ConfigMaps:     []K8sConfigMap{},
		HPAs:           []K8sHPA{},
		PartialReasons: []string{},
	}
	unreadable := func(family string, err error) {
		inventory.PartialReasons = append(inventory.PartialReasons,
			fmt.Sprintf("%s: %s", family, firstLine(err.Error())))
	}

	sink.Progress(15, string(OpKubernetesInventory), "reading namespaces and nodes")

	if raw, err := getJSON(ctx, runner, base, "namespaces"); err != nil {
		unreadable("namespaces", err)
	} else {
		var payload struct {
			Items []struct {
				Metadata struct{ Name string } `json:"metadata"`
			} `json:"items"`
		}
		if err := decodeFirstJSON(raw, &payload); err != nil {
			unreadable("namespaces", err)
		}
		for _, item := range payload.Items {
			inventory.Namespaces = append(inventory.Namespaces, item.Metadata.Name)
		}
	}

	if raw, err := getJSON(ctx, runner, base, "nodes"); err != nil {
		unreadable("nodes", err)
	} else {
		var payload struct {
			Items []struct {
				Metadata struct{ Name string } `json:"metadata"`
				Status   struct {
					Conditions []struct {
						Type   string `json:"type"`
						Status string `json:"status"`
					} `json:"conditions"`
					NodeInfo struct {
						KubeletVersion string `json:"kubeletVersion"`
						OSImage        string `json:"osImage"`
					} `json:"nodeInfo"`
					Allocatable map[string]string `json:"allocatable"`
				} `json:"status"`
			} `json:"items"`
		}
		if err := decodeFirstJSON(raw, &payload); err != nil {
			unreadable("nodes", err)
		}
		for _, item := range payload.Items {
			node := K8sNode{
				Name:    item.Metadata.Name,
				Version: item.Status.NodeInfo.KubeletVersion,
				OSImage: item.Status.NodeInfo.OSImage,
			}
			node.Allocatable.CPU = item.Status.Allocatable["cpu"]
			node.Allocatable.Memory = item.Status.Allocatable["memory"]
			// Only a Ready condition that is actually present sets the pointer. An absent condition
			// leaves it nil, which the dashboard renders as "the node has not reported" — not as "the
			// node is down", which would page somebody for a reporting gap.
			for _, condition := range item.Status.Conditions {
				if condition.Type != "Ready" {
					continue
				}
				ready := condition.Status == "True"
				node.Ready = &ready
			}
			inventory.Nodes = append(inventory.Nodes, node)
		}
	}

	sink.Progress(40, string(OpKubernetesInventory), "reading pods and workloads")

	scope := scopeArgs(args.Namespace)
	if raw, err := getJSON(ctx, runner, base, append([]string{"pods"}, scope...)...); err != nil {
		unreadable("pods", err)
	} else {
		var payload struct {
			Items []struct {
				Metadata struct {
					Name      string `json:"name"`
					Namespace string `json:"namespace"`
				} `json:"metadata"`
				Spec struct {
					NodeName string `json:"nodeName"`
				} `json:"spec"`
				Status struct {
					Phase             string `json:"phase"`
					StartTime         string `json:"startTime"`
					ContainerStatuses []struct {
						Ready        bool `json:"ready"`
						RestartCount int  `json:"restartCount"`
						State        struct {
							Waiting *struct {
								Reason string `json:"reason"`
							} `json:"waiting"`
						} `json:"state"`
					} `json:"containerStatuses"`
				} `json:"status"`
			} `json:"items"`
		}
		if err := decodeFirstJSON(raw, &payload); err != nil {
			unreadable("pods", err)
		}
		for _, item := range payload.Items {
			pod := K8sPod{
				Namespace: item.Metadata.Namespace,
				Name:      item.Metadata.Name,
				Phase:     item.Status.Phase,
				Node:      item.Spec.NodeName,
				StartedAt: item.Status.StartTime,
				Total:     len(item.Status.ContainerStatuses),
			}
			for _, container := range item.Status.ContainerStatuses {
				if container.Ready {
					pod.Ready++
				}
				pod.Restarts += container.RestartCount
				if container.State.Waiting != nil && pod.Reason == "" {
					pod.Reason = container.State.Waiting.Reason
				}
			}
			inventory.Pods = append(inventory.Pods, pod)
		}
	}

	for kind, plural := range map[string]string{
		"deployment": "deployments", "statefulset": "statefulsets", "daemonset": "daemonsets",
	} {
		raw, err := getJSON(ctx, runner, base, append([]string{plural}, scope...)...)
		if err != nil {
			unreadable(plural, err)
			continue
		}
		var payload struct {
			Items []struct {
				Metadata struct {
					Name      string `json:"name"`
					Namespace string `json:"namespace"`
				} `json:"metadata"`
				Spec struct {
					Replicas *int `json:"replicas"`
					Template struct {
						Spec struct {
							Containers []struct {
								Image string `json:"image"`
							} `json:"containers"`
						} `json:"spec"`
					} `json:"template"`
				} `json:"spec"`
				Status struct {
					ReadyReplicas *int `json:"readyReplicas"`
					// A DaemonSet reports differently: it has no `spec.replicas`, and the number that
					// matters is how many nodes it should be on. Read both and prefer the one present.
					DesiredNumberScheduled *int `json:"desiredNumberScheduled"`
					NumberReady            *int `json:"numberReady"`
				} `json:"status"`
			} `json:"items"`
		}
		if err := decodeFirstJSON(raw, &payload); err != nil {
			unreadable(plural, err)
			continue
		}
		for _, item := range payload.Items {
			workload := K8sWorkload{
				Namespace: item.Metadata.Namespace, Kind: kind, Name: item.Metadata.Name,
				Desired: item.Spec.Replicas, Ready: item.Status.ReadyReplicas, Images: []string{},
			}
			if workload.Desired == nil && item.Status.DesiredNumberScheduled != nil {
				workload.Desired = item.Status.DesiredNumberScheduled
			}
			if workload.Ready == nil && item.Status.NumberReady != nil {
				workload.Ready = item.Status.NumberReady
			}
			for _, container := range item.Spec.Template.Spec.Containers {
				workload.Images = append(workload.Images, container.Image)
			}
			inventory.Workloads = append(inventory.Workloads, workload)
		}
	}

	sink.Progress(70, string(OpKubernetesInventory), "reading services, ingresses, config and autoscalers")

	if raw, err := getJSON(ctx, runner, base, append([]string{"services"}, scope...)...); err != nil {
		unreadable("services", err)
	} else {
		var payload struct {
			Items []struct {
				Metadata struct {
					Name      string `json:"name"`
					Namespace string `json:"namespace"`
				} `json:"metadata"`
				Spec struct {
					Type      string `json:"type"`
					ClusterIP string `json:"clusterIP"`
					Ports     []struct {
						Port     int    `json:"port"`
						Protocol string `json:"protocol"`
					} `json:"ports"`
				} `json:"spec"`
			} `json:"items"`
		}
		if err := decodeFirstJSON(raw, &payload); err != nil {
			unreadable("services", err)
		}
		for _, item := range payload.Items {
			ports := make([]string, 0, len(item.Spec.Ports))
			for _, port := range item.Spec.Ports {
				ports = append(ports, fmt.Sprintf("%d/%s", port.Port, port.Protocol))
			}
			inventory.Services = append(inventory.Services, K8sService{
				Namespace: item.Metadata.Namespace, Name: item.Metadata.Name,
				Type: item.Spec.Type, ClusterIP: item.Spec.ClusterIP, Ports: strings.Join(ports, ","),
			})
		}
	}

	if raw, err := getJSON(ctx, runner, base, append([]string{"ingresses"}, scope...)...); err != nil {
		unreadable("ingresses", err)
	} else {
		var payload struct {
			Items []struct {
				Metadata struct {
					Name      string `json:"name"`
					Namespace string `json:"namespace"`
				} `json:"metadata"`
				Spec struct {
					IngressClassName string `json:"ingressClassName"`
					Rules            []struct {
						Host string `json:"host"`
					} `json:"rules"`
				} `json:"spec"`
			} `json:"items"`
		}
		if err := decodeFirstJSON(raw, &payload); err != nil {
			unreadable("ingresses", err)
		}
		for _, item := range payload.Items {
			hosts := make([]string, 0, len(item.Spec.Rules))
			for _, rule := range item.Spec.Rules {
				if rule.Host != "" {
					hosts = append(hosts, rule.Host)
				}
			}
			inventory.Ingresses = append(inventory.Ingresses, K8sIngress{
				Namespace: item.Metadata.Namespace, Name: item.Metadata.Name,
				Hosts: hosts, Class: item.Spec.IngressClassName,
			})
		}
	}

	if raw, err := getJSON(ctx, runner, base, append([]string{"configmaps"}, scope...)...); err != nil {
		unreadable("configmaps", err)
	} else {
		var payload struct {
			Items []struct {
				Metadata struct {
					Name      string `json:"name"`
					Namespace string `json:"namespace"`
				} `json:"metadata"`
				Data map[string]string `json:"data"`
			} `json:"items"`
		}
		if err := decodeFirstJSON(raw, &payload); err != nil {
			unreadable("configmaps", err)
		}
		for _, item := range payload.Items {
			// KEYS ONLY. A ConfigMap regularly holds something that should have been a Secret, and a
			// dashboard that rendered values would publish it to every viewer of the project.
			keys := make([]string, 0, len(item.Data))
			for key := range item.Data {
				keys = append(keys, key)
			}
			inventory.ConfigMaps = append(inventory.ConfigMaps, K8sConfigMap{
				Namespace: item.Metadata.Namespace, Name: item.Metadata.Name, Keys: keys,
			})
		}
	}

	if raw, err := getJSON(ctx, runner, base, append([]string{"horizontalpodautoscalers"}, scope...)...); err != nil {
		unreadable("horizontalpodautoscalers", err)
	} else {
		var payload struct {
			Items []struct {
				Metadata struct {
					Name      string `json:"name"`
					Namespace string `json:"namespace"`
				} `json:"metadata"`
				Spec struct {
					MinReplicas    *int `json:"minReplicas"`
					MaxReplicas    int  `json:"maxReplicas"`
					ScaleTargetRef struct {
						Kind string `json:"kind"`
						Name string `json:"name"`
					} `json:"scaleTargetRef"`
				} `json:"spec"`
				Status struct {
					CurrentReplicas *int `json:"currentReplicas"`
				} `json:"status"`
			} `json:"items"`
		}
		if err := decodeFirstJSON(raw, &payload); err != nil {
			unreadable("horizontalpodautoscalers", err)
		}
		for _, item := range payload.Items {
			inventory.HPAs = append(inventory.HPAs, K8sHPA{
				Namespace: item.Metadata.Namespace, Name: item.Metadata.Name,
				Target: fmt.Sprintf("%s/%s", strings.ToLower(item.Spec.ScaleTargetRef.Kind),
					item.Spec.ScaleTargetRef.Name),
				MinReplicas: item.Spec.MinReplicas, MaxReplicas: intPtr(item.Spec.MaxReplicas),
				Current: item.Status.CurrentReplicas,
			})
		}
	}

	encoded, err := json.Marshal(inventory)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable kubernetes inventory: %w", err)
	}
	// `partial` is a DIFFERENT status from `reported`, so the backend never has to infer completeness
	// by inspecting the payload.
	status := "reported"
	if len(inventory.PartialReasons) > 0 {
		status = "partial"
	}
	sink.Progress(100, string(OpKubernetesInventory),
		fmt.Sprintf("%d pod(s), %d workload(s), %d node(s)",
			len(inventory.Pods), len(inventory.Workloads), len(inventory.Nodes)))
	return Result{Status: status, Output: string(encoded)}, nil
}

// readWorkloadReplicas reads a workload's ready replica count, or nil when it cannot be read.
func readWorkloadReplicas(ctx context.Context, runner *validator.Runner, base []string,
	namespace, kind, name string,
) *int {
	args := append(append([]string{"get"}, base...), kind, name, "--namespace", namespace,
		"-o", "jsonpath={.status.readyReplicas}")
	outcome, err := runner.Run(ctx, "kubectl", args...)
	if err != nil || !outcome.Passed {
		return nil
	}
	trimmed := strings.TrimSpace(outcome.Output)
	if trimmed == "" {
		// An empty field means the API server has not populated it — a workload scaled to zero reports
		// nothing here, and so does one that has never been observed. Nil is the honest answer.
		return nil
	}
	value, convErr := strconv.Atoi(trimmed)
	if convErr != nil {
		return nil
	}
	return &value
}

// k8sWorkloadAction is the mutating handler.
func k8sWorkloadAction(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args k8sWorkloadActionArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: kubernetes workload action arguments: %v", ErrBadArgs, err)
	}
	if _, known := workloadActions[args.Action]; !known {
		return Result{}, fmt.Errorf("%w: %q", ErrUnknownWorkloadAction, args.Action)
	}
	kind := strings.ToLower(strings.TrimSpace(args.Kind))
	// THE SAME CLOSED SET the deployment operation waits on, and deliberately the same variable: a
	// workload this platform can act on is one it can also verify. An action on something it cannot
	// verify would report success on the API server's acceptance alone.
	if _, waitable := workloadKinds[kind]; !waitable {
		return Result{}, fmt.Errorf("%w: %q is not a workload kind this operation acts on",
			ErrUnknownWorkloadAction, args.Kind)
	}
	if strings.TrimSpace(args.Name) == "" || strings.TrimSpace(args.Namespace) == "" {
		return Result{}, fmt.Errorf("%w: a workload action needs both a namespace and a name", ErrNoTarget)
	}
	if args.Action == "scale" && (args.Replicas < 0 || args.Replicas > MaxScaleReplicas) {
		return Result{}, fmt.Errorf("%w: %d requested, 0 to %d allowed",
			ErrReplicasOutOfRange, args.Replicas, MaxScaleReplicas)
	}

	runner, base, _, err := kubectlRunner(ctx, d.root, args.Context)
	if err != nil {
		return Result{}, err
	}

	clusterContext := args.Context
	if clusterContext == "" {
		if outcome, ctxErr := runner.Run(ctx, "kubectl", "config", "current-context"); ctxErr == nil {
			clusterContext = strings.TrimSpace(outcome.Output)
		}
	}

	target := kind + "/" + args.Name
	before := readWorkloadReplicas(ctx, runner, base, args.Namespace, kind, args.Name)

	var actionArgs []string
	switch args.Action {
	case "scale":
		actionArgs = []string{"scale", kind + "/" + args.Name,
			fmt.Sprintf("--replicas=%d", args.Replicas)}
	case "restart":
		// A rollout restart, not a pod delete. Deleting pods to restart a workload bypasses the
		// workload's own surge and availability settings, which is how a "restart" becomes an outage.
		actionArgs = []string{"rollout", "restart", kind + "/" + args.Name}
	case "rollback":
		actionArgs = []string{"rollout", "undo", kind + "/" + args.Name}
		if args.ToRevision > 0 {
			actionArgs = append(actionArgs, fmt.Sprintf("--to-revision=%d", args.ToRevision))
		}
	}
	actionArgs = append(actionArgs, "--namespace", args.Namespace)

	sink.Progress(30, string(OpKubernetesWorkloadAction), fmt.Sprintf("%s %s", args.Action, target))
	outcome, runErr := runner.Run(ctx, "kubectl", append(append([]string{}, base...), actionArgs...)...)
	if runErr != nil || !outcome.Passed {
		return Result{}, fmt.Errorf("executor: kubectl %s refused %s: %w — %s",
			args.Action, target, errOrRefused(runErr), firstLine(outcome.Output))
	}

	// THE HALF THAT MAKES THIS WORTH HAVING, and the same half `deployment.apply_manifests` has: the
	// API server accepting a scale is not the cluster satisfying it. A scale to a replica count the
	// cluster has no capacity for is accepted instantly and never converges.
	wait := healthTimeout(args.HealthTimeoutSeconds)
	sink.Progress(60, string(OpKubernetesWorkloadAction),
		fmt.Sprintf("verifying convergence, up to %s", wait))

	report := K8sActionReport{
		Action: args.Action, Namespace: args.Namespace, Kind: kind, Name: args.Name,
		ReplicasBefore: before, ClusterContext: clusterContext,
		ObservedAt: d.now().UTC().Format(time.RFC3339),
	}

	waitCtx, cancel := context.WithTimeout(ctx, wait)
	defer cancel()
	started := time.Now()
	rollout, rolloutErr := runner.Run(waitCtx, "kubectl", append(append([]string{}, base...),
		"rollout", "status", target, "--namespace", args.Namespace,
		fmt.Sprintf("--timeout=%ds", int(wait.Seconds())))...)
	report.Health = WorkloadHealth{
		Kind: kind, Name: args.Name,
		Ready:         rolloutErr == nil && rollout.Passed,
		Detail:        firstLine(rollout.Output),
		WaitedSeconds: int(time.Since(started).Seconds()),
	}
	if report.Health.Detail == "" && rolloutErr != nil {
		report.Health.Detail = rolloutErr.Error()
	}
	report.Healthy = report.Health.Ready
	report.ReplicasAfter = readWorkloadReplicas(ctx, runner, base, args.Namespace, kind, args.Name)

	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable workload action report: %w", err)
	}
	// `applied` and `degraded` are separate statuses for the same reason the deployment operation has
	// them: the action happened either way, and only one of the two is finished.
	status := "applied"
	if !report.Healthy {
		status = "degraded"
	}
	sink.Progress(100, string(OpKubernetesWorkloadAction),
		fmt.Sprintf("%s %s: %s", args.Action, target, status))
	return Result{Status: status, Output: string(encoded)}, nil
}
