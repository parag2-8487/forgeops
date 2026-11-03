// SPDX-License-Identifier: Apache-2.0

// Decoding `kubectl get -o json` into the shapes the dashboard renders.
//
// WHY THESE ARE THEIR OWN FUNCTIONS. They were inline closures inside `k8sInventory`, which meant the only
// way to exercise them was to have a cluster — so on a machine without one they were 365 uncovered lines,
// and the parts most likely to be wrong were the parts least likely to be checked. They are pure functions
// of a tool's output: given bytes, produce typed rows. That is exactly the shape that should be tested
// against REAL captured output, and `kubernetes_decode_test.go` does, using payloads taken from an actual
// cluster rather than invented ones.
//
// Each returns an error rather than an empty slice when the document cannot be read. That distinction is
// the whole contract of this dashboard: "this namespace has no ingresses" and "the ingress list was
// unreadable" are opposite facts, and the caller turns the second into a named entry in `partial_reasons`
// instead of an empty table.
//
// THE FOUR SHAPES OF THE REAL API THAT NAIVE DECODING GETS WRONG, all of them handled here and each pinned
// by a test:
//
//  1. A DaemonSet has no `spec.replicas` at all. Its desired count lives in
//     `status.desiredNumberScheduled`, so a reader that only looked at the spec would report every
//     DaemonSet as having no desired replicas.
//  2. A node's `Ready` condition is one entry in an ARRAY whose order is not fixed, so it must be searched
//     for. An absent condition leaves the tri-state nil rather than false.
//  3. `status.readyReplicas` is OMITTED for a workload at zero replicas — not zero. Pointers throughout,
//     because a workload scaled to zero and a workload nothing has observed are different facts.
//  4. A pod's waiting reason is per CONTAINER, and the one an operator needs (`ImagePullBackOff`,
//     `CrashLoopBackOff`) belongs to whichever container is stuck.
package executor

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
)

// decodeNamespaces reads a namespace list.
func decodeNamespaces(document string) ([]string, error) {
	var payload struct {
		Items []struct {
			Metadata struct {
				Name string `json:"name"`
			} `json:"metadata"`
		} `json:"items"`
	}
	if err := decodeFirstJSON(document, &payload); err != nil {
		return nil, err
	}
	names := make([]string, 0, len(payload.Items))
	for _, item := range payload.Items {
		names = append(names, item.Metadata.Name)
	}
	return names, nil
}

// decodeNodes reads a node list, preserving the readiness tri-state.
func decodeNodes(document string) ([]K8sNode, error) {
	var payload struct {
		Items []struct {
			Metadata struct {
				Name string `json:"name"`
			} `json:"metadata"`
			Status struct {
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
	if err := decodeFirstJSON(document, &payload); err != nil {
		return nil, err
	}
	nodes := make([]K8sNode, 0, len(payload.Items))
	for _, item := range payload.Items {
		node := K8sNode{
			Name:    item.Metadata.Name,
			Version: item.Status.NodeInfo.KubeletVersion,
			OSImage: item.Status.NodeInfo.OSImage,
		}
		node.Allocatable.CPU = item.Status.Allocatable["cpu"]
		node.Allocatable.Memory = item.Status.Allocatable["memory"]
		// SEARCHED, not indexed: the conditions array's order is not fixed. And only a condition that is
		// actually present sets the pointer — an absent one leaves it nil, which the dashboard renders as
		// "has not reported" rather than as "NotReady", because paging somebody for a reporting gap and
		// hiding a real outage are both worse than saying what is known.
		for _, condition := range item.Status.Conditions {
			if condition.Type != "Ready" {
				continue
			}
			ready := condition.Status == "True"
			node.Ready = &ready
		}
		nodes = append(nodes, node)
	}
	return nodes, nil
}

// decodePods reads a pod list, expressing readiness as a ratio and surfacing the waiting reason.
func decodePods(document string) ([]K8sPod, error) {
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
	if err := decodeFirstJSON(document, &payload); err != nil {
		return nil, err
	}
	pods := make([]K8sPod, 0, len(payload.Items))
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
			// SUMMED across containers: the single most diagnostic number for a crash loop is the total,
			// and reporting only the first container's count would understate a failing sidecar.
			pod.Restarts += container.RestartCount
			if container.State.Waiting != nil && pod.Reason == "" {
				pod.Reason = container.State.Waiting.Reason
			}
		}
		pods = append(pods, pod)
	}
	return pods, nil
}

// decodeWorkloads reads one workload family. `kind` is the singular lower-case name the dashboard and the
// action operation both use, so what is displayed is what can be acted on.
func decodeWorkloads(kind, document string) ([]K8sWorkload, error) {
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
				// A DaemonSet reports differently: no `spec.replicas`, and the number that matters is how
				// many nodes it should be on. Both are read and the one present is preferred.
				DesiredNumberScheduled *int `json:"desiredNumberScheduled"`
				NumberReady            *int `json:"numberReady"`
			} `json:"status"`
		} `json:"items"`
	}
	if err := decodeFirstJSON(document, &payload); err != nil {
		return nil, err
	}
	workloads := make([]K8sWorkload, 0, len(payload.Items))
	for _, item := range payload.Items {
		workload := K8sWorkload{
			Namespace: item.Metadata.Namespace,
			Kind:      kind,
			Name:      item.Metadata.Name,
			Desired:   item.Spec.Replicas,
			Ready:     item.Status.ReadyReplicas,
			Images:    []string{},
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
		workloads = append(workloads, workload)
	}
	return workloads, nil
}

// decodeServices reads a service list, flattening the port list into one readable column.
func decodeServices(document string) ([]K8sService, error) {
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
	if err := decodeFirstJSON(document, &payload); err != nil {
		return nil, err
	}
	services := make([]K8sService, 0, len(payload.Items))
	for _, item := range payload.Items {
		ports := make([]string, 0, len(item.Spec.Ports))
		for _, port := range item.Spec.Ports {
			ports = append(ports, fmt.Sprintf("%d/%s", port.Port, port.Protocol))
		}
		services = append(services, K8sService{
			Namespace: item.Metadata.Namespace,
			Name:      item.Metadata.Name,
			Type:      item.Spec.Type,
			ClusterIP: item.Spec.ClusterIP,
			Ports:     strings.Join(ports, ","),
		})
	}
	return services, nil
}

// decodeIngresses reads an ingress list by the hosts it answers on.
func decodeIngresses(document string) ([]K8sIngress, error) {
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
	if err := decodeFirstJSON(document, &payload); err != nil {
		return nil, err
	}
	ingresses := make([]K8sIngress, 0, len(payload.Items))
	for _, item := range payload.Items {
		hosts := make([]string, 0, len(item.Spec.Rules))
		for _, rule := range item.Spec.Rules {
			// A rule with no host is a catch-all and has no name to show. Skipped rather than rendered as
			// an empty string, which would look like a bug in this panel.
			if rule.Host != "" {
				hosts = append(hosts, rule.Host)
			}
		}
		ingresses = append(ingresses, K8sIngress{
			Namespace: item.Metadata.Namespace,
			Name:      item.Metadata.Name,
			Hosts:     hosts,
			Class:     item.Spec.IngressClassName,
		})
	}
	return ingresses, nil
}

// decodeConfigMaps reads a ConfigMap list as KEY NAMES ONLY.
//
// The values are never decoded, let alone reported. A ConfigMap regularly holds something that should have
// been a Secret, and a dashboard that printed values would publish it to every viewer of the project. The
// keys are the useful half: an operator needs to know a key exists without being shown what it holds.
func decodeConfigMaps(document string) ([]K8sConfigMap, error) {
	var payload struct {
		Items []struct {
			Metadata struct {
				Name      string `json:"name"`
				Namespace string `json:"namespace"`
			} `json:"metadata"`
			Data map[string]string `json:"data"`
		} `json:"items"`
	}
	if err := decodeFirstJSON(document, &payload); err != nil {
		return nil, err
	}
	configMaps := make([]K8sConfigMap, 0, len(payload.Items))
	for _, item := range payload.Items {
		keys := make([]string, 0, len(item.Data))
		for key := range item.Data {
			keys = append(keys, key)
		}
		// SORTED, because Go's map iteration order is randomised and a panel whose rows reordered on every
		// refresh is unusable.
		slices.Sort(keys)
		configMaps = append(configMaps, K8sConfigMap{
			Namespace: item.Metadata.Namespace,
			Name:      item.Metadata.Name,
			Keys:      keys,
		})
	}
	return configMaps, nil
}

// decodeHPAs reads a HorizontalPodAutoscaler list.
func decodeHPAs(document string) ([]K8sHPA, error) {
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
	if err := decodeFirstJSON(document, &payload); err != nil {
		return nil, err
	}
	autoscalers := make([]K8sHPA, 0, len(payload.Items))
	for _, item := range payload.Items {
		autoscalers = append(autoscalers, K8sHPA{
			Namespace: item.Metadata.Namespace,
			Name:      item.Metadata.Name,
			Target: fmt.Sprintf("%s/%s", strings.ToLower(item.Spec.ScaleTargetRef.Kind),
				item.Spec.ScaleTargetRef.Name),
			MinReplicas: item.Spec.MinReplicas,
			MaxReplicas: intPtr(item.Spec.MaxReplicas),
			Current:     item.Status.CurrentReplicas,
		})
	}
	return autoscalers, nil
}

// decodeDockerRows reads docker's `--format json` output into container rows.
//
// One object per LINE, not an array — see `decodeJSONLines`. Every measured field is left nil here: the
// stats sample is a separate call, and a row that arrived without one must report "not measured" rather
// than zero.
func decodeDockerRows(output string) ([]DockerContainer, error) {
	containers := []DockerContainer{}
	err := decodeJSONLines(output, func(raw json.RawMessage) error {
		var row struct {
			ID     string `json:"ID"`
			Names  string `json:"Names"`
			Image  string `json:"Image"`
			State  string `json:"State"`
			Status string `json:"Status"`
			Ports  string `json:"Ports"`
		}
		if err := decodeFirstJSON(string(raw), &row); err != nil {
			return err
		}
		containers = append(containers, DockerContainer{
			ID: row.ID, Name: row.Names, Image: row.Image,
			State: row.State, Status: row.Status, Ports: row.Ports,
		})
		return nil
	})
	if err != nil {
		return nil, err
	}
	return containers, nil
}

// applyDockerStats folds a `docker stats --no-stream --format json` sample into rows already read.
//
// Returns whether ANY row was populated, which becomes `stats_sampled`. Without that flag a dashboard
// cannot distinguish "stats were not requested" from "every container is idle" — and the second is a
// statement about the host while the first is a statement about the request.
func applyDockerStats(containers []DockerContainer, output string) (bool, error) {
	byName := map[string]*DockerContainer{}
	for index := range containers {
		byName[containers[index].Name] = &containers[index]
		byName[containers[index].ID] = &containers[index]
	}
	sampled := false
	err := decodeJSONLines(output, func(raw json.RawMessage) error {
		var row struct {
			Name     string `json:"Name"`
			ID       string `json:"ID"`
			CPUPerc  string `json:"CPUPerc"`
			MemUsage string `json:"MemUsage"`
			NetIO    string `json:"NetIO"`
		}
		if err := decodeFirstJSON(string(raw), &row); err != nil {
			return err
		}
		target := byName[row.Name]
		if target == nil {
			target = byName[row.ID]
		}
		if target == nil {
			// A container that appeared between the two calls. Ignored rather than treated as an error: a
			// list one second out of date is not a failure, and refusing the whole sample for it would be.
			return nil
		}
		target.CPUPercent = parseStatPercent(row.CPUPerc)
		target.MemoryBytes, target.MemoryLimit = parseStatBytes(row.MemUsage)
		target.NetworkRx, target.NetworkTx = parseStatBytes(row.NetIO)
		sampled = true
		return nil
	})
	if err != nil {
		return false, err
	}
	return sampled, nil
}
