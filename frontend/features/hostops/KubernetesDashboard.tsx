"use client";

/**
 * §2.9's Kubernetes management dashboard.
 *
 * IT SHARES THE FRESHNESS RULE WITH THE DOCKER DASHBOARD ON PURPOSE. `freshnessOf` is imported rather than
 * re-implemented: two copies of a staleness threshold is how one panel comes to call four minutes fresh
 * while the panel beside it calls the same reading stale, and an operator comparing them has no way to know
 * which to believe.
 *
 * THE THREE STATES THIS SCREEN MUST KEEP APART, in every panel:
 *
 *   absent        the cluster answered and there is none of this thing. "No ingresses in this namespace."
 *   unreadable    the cluster refused to tell us. `partial_reasons` names the family and the cause, and
 *                 this is rendered as a warning, never as an empty list. "This cluster has no ingresses"
 *                 and "you may not list ingresses" are opposite facts and an empty table states the wrong
 *                 one.
 *   unreported    the object exists and its status field is not populated. Every count from the wire is
 *                 `number | null` for this reason: a Deployment scaled to zero has NO `readyReplicas`, and
 *                 so does one the API server has not observed yet. Rendering both as "0 ready" would make a
 *                 healthy scaled-down workload look broken and a broken one look scaled down.
 *
 * A NODE'S READINESS IS A TRI-STATE FOR THE SAME REASON. `ready: null` means the node has not reported its
 * Ready condition. Showing that as "NotReady" would page somebody for a reporting gap; showing it as
 * "Ready" would hide a real outage. It is shown as "has not reported".
 *
 * SCALE, RESTART AND ROLLBACK REPORT A GOVERNANCE OUTCOME, not a result — the same reasoning as the Docker
 * dashboard. Against a recorded environment they also inherit that environment's approval requirement, so
 * the commonest outcome on production is "waiting for a human", and the button says so.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { EnvironmentSelector } from "@/features/environments/EnvironmentManager";
import { freshnessOf } from "@/features/hostops/DockerDashboard";
import { PodDetail } from "@/features/hostops/LogPanel";
import { api, queryKeys } from "@/lib/api";

export type K8sPod = {
  namespace: string;
  name: string;
  phase: string;
  ready_containers: number;
  total_containers: number;
  restarts: number;
  node: string;
  /** `ImagePullBackOff`, `CrashLoopBackOff`… empty when the pod is not waiting on anything. */
  reason: string;
  started_at: string;
};

export type K8sWorkload = {
  namespace: string;
  kind: string;
  name: string;
  desired_replicas: number | null;
  ready_replicas: number | null;
  images: string[];
};

export type K8sNode = {
  name: string;
  /** `null` means the node has not reported a Ready condition. Not the same as not ready. */
  ready: boolean | null;
  kubelet_version: string;
  os_image: string;
  allocatable: { cpu: string; memory: string };
};

export type K8sInventory = {
  cluster_context: string;
  server_version: string;
  namespaces: string[];
  nodes: K8sNode[];
  pods: K8sPod[];
  workloads: K8sWorkload[];
  services: { namespace: string; name: string; type: string; cluster_ip: string; ports: string }[];
  ingresses: { namespace: string; name: string; hosts: string[]; class: string }[];
  config_maps: { namespace: string; name: string; keys: string[] }[];
  horizontal_pod_autoscalers: {
    namespace: string;
    name: string;
    target: string;
    min_replicas: number | null;
    max_replicas: number | null;
    current_replicas: number | null;
  }[];
  observed_at: string;
  /** Each family that could not be read, with its cause. Never flattened into an empty list. */
  partial_reasons: string[];
};

type ActionAccepted = { change_set_id: string; status: string; outcome: string };

/** Render a count that may not have been reported. */
export function count(value: number | null): string {
  return value === null ? "not reported" : String(value);
}

export function KubernetesDashboard({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [namespace, setNamespace] = useState<string | null>(null);
  const [environmentId, setEnvironmentId] = useState<string | null>(null);
  const [lastOutcome, setLastOutcome] = useState<string | null>(null);
  const [scaleTo, setScaleTo] = useState<Record<string, number>>({});
  const [openPod, setOpenPod] = useState<string | null>(null);

  const inventory = useQuery<K8sInventory>({
    queryKey: queryKeys.hostops.kubernetesInventory(projectId, namespace),
    queryFn: () =>
      api.get<K8sInventory>(
        `/projects/${projectId}/kubernetes/inventory${namespace ? `?namespace=${encodeURIComponent(namespace)}` : ""}`,
      ),
  });

  const action = useMutation<
    ActionAccepted,
    Error,
    {
      action: "scale" | "restart" | "rollback";
      kind: string;
      name: string;
      namespace: string;
      replicas?: number;
    }
  >({
    mutationFn: (body) =>
      api.post<ActionAccepted>(`/projects/${projectId}/kubernetes/workloads/actions`, {
        ...body,
        environment_id: environmentId,
      }),
    onSuccess: (accepted) => {
      setLastOutcome(accepted.outcome);
      void queryClient.invalidateQueries({ queryKey: queryKeys.hostops.all });
    },
  });

  if (inventory.isLoading) {
    return (
      <section aria-label="Kubernetes" data-testid="k8s-dashboard">
        <p data-testid="k8s-loading">Asking the agent what the cluster holds…</p>
      </section>
    );
  }

  if (inventory.isError) {
    return (
      <section aria-label="Kubernetes" data-testid="k8s-dashboard">
        <p data-testid="k8s-error" role="alert">
          The agent did not report the cluster: {inventory.error.message}. This is not the same as
          an empty cluster.
        </p>
        <button type="button" onClick={() => void inventory.refetch()}>
          Ask again
        </button>
      </section>
    );
  }

  const data = inventory.data;
  if (!data) return null;
  const freshness = freshnessOf(data.observed_at);

  return (
    <section aria-label="Kubernetes" data-testid="k8s-dashboard">
      <header>
        <h2>Cluster</h2>
        <p>
          <span data-testid="k8s-context">{data.cluster_context}</span>{" "}
          <span data-testid="k8s-server-version">Kubernetes {data.server_version}</span>{" "}
          <span data-testid="k8s-freshness">
            {freshness.kind === "never-reported"
              ? "never reported"
              : freshness.kind === "stale"
                ? `stale — last reported ${freshness.ageSeconds}s ago`
                : `reported ${freshness.ageSeconds}s ago`}
          </span>
        </p>

        {/* UNREADABLE IS NOT EMPTY, and this is where that promise is kept. */}
        {data.partial_reasons.length > 0 && (
          <div data-testid="k8s-partial" role="alert">
            <p>
              Some of this cluster could not be read, so the panels below are incomplete rather than
              empty:
            </p>
            <ul>
              {data.partial_reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </div>
        )}

        <label htmlFor="k8s-namespace">Namespace</label>
        <select
          id="k8s-namespace"
          data-testid="k8s-namespace-select"
          value={namespace ?? ""}
          onChange={(event) => setNamespace(event.target.value || null)}
        >
          <option value="">Every namespace</option>
          {data.namespaces.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>

        {/* §2.1's selector, on its third screen. An action against a recorded environment inherits that
            environment's approval requirement, which is why this is here rather than implicit. */}
        <EnvironmentSelector
          projectId={projectId}
          value={environmentId}
          onChange={setEnvironmentId}
        />
      </header>

      {lastOutcome && (
        <p data-testid="k8s-last-outcome" role="status">
          {lastOutcome === "applying"
            ? "Sent to the agent."
            : lastOutcome === "approval-required"
              ? "Waiting for a human to approve it. Nothing has changed in the cluster yet."
              : `The governance gate answered: ${lastOutcome}.`}
        </p>
      )}

      <h3>Nodes</h3>
      {data.nodes.length === 0 ? (
        <p data-testid="k8s-nodes-empty">
          No nodes were reported. A cluster that answered has at least one, so check the warning
          above.
        </p>
      ) : (
        <table data-testid="k8s-nodes">
          <thead>
            <tr>
              <th scope="col">Node</th>
              <th scope="col">Ready</th>
              <th scope="col">Kubelet</th>
              <th scope="col">Allocatable CPU / memory</th>
            </tr>
          </thead>
          <tbody>
            {data.nodes.map((node) => (
              <tr key={node.name} data-testid={`k8s-node-${node.name}`}>
                <td>{node.name}</td>
                <td data-testid={`k8s-node-ready-${node.name}`}>
                  {/* THREE WORDS FOR THREE STATES. "has not reported" is not "NotReady". */}
                  {node.ready === null ? "has not reported" : node.ready ? "Ready" : "NotReady"}
                </td>
                <td>{node.kubelet_version}</td>
                <td>
                  {node.allocatable.cpu || "not reported"} /{" "}
                  {node.allocatable.memory || "not reported"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Workloads</h3>
      {data.workloads.length === 0 ? (
        <p data-testid="k8s-workloads-empty">
          {namespace
            ? `No deployments, stateful sets or daemon sets in ${namespace}.`
            : "No deployments, stateful sets or daemon sets in any namespace."}
        </p>
      ) : (
        <table data-testid="k8s-workloads">
          <thead>
            <tr>
              <th scope="col">Workload</th>
              <th scope="col">Namespace</th>
              <th scope="col">Ready / desired</th>
              <th scope="col">Image</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.workloads.map((workload) => {
              const key = `${workload.namespace}/${workload.kind}/${workload.name}`;
              return (
                <tr key={key} data-testid={`k8s-workload-${workload.name}`}>
                  <td>
                    {workload.kind}/{workload.name}
                  </td>
                  <td>{workload.namespace}</td>
                  <td data-testid={`k8s-replicas-${workload.name}`}>
                    {/* "not reported" on either side, never 0. A workload at zero replicas reports no
                        readyReplicas at all, and so does one nothing has observed yet. */}
                    {count(workload.ready_replicas)} / {count(workload.desired_replicas)}
                  </td>
                  <td>{workload.images.join(", ") || "not reported"}</td>
                  <td>
                    <label htmlFor={`k8s-scale-${workload.name}`} className="sr-only">
                      Replicas for {workload.name}
                    </label>
                    <input
                      id={`k8s-scale-${workload.name}`}
                      data-testid={`k8s-scale-input-${workload.name}`}
                      type="number"
                      min={0}
                      max={100}
                      value={scaleTo[key] ?? workload.desired_replicas ?? 0}
                      onChange={(event) =>
                        setScaleTo({ ...scaleTo, [key]: Number(event.target.value) })
                      }
                    />
                    <button
                      type="button"
                      data-testid={`k8s-scale-${workload.name}`}
                      disabled={action.isPending}
                      onClick={() =>
                        action.mutate({
                          action: "scale",
                          kind: workload.kind,
                          name: workload.name,
                          namespace: workload.namespace,
                          replicas: scaleTo[key] ?? workload.desired_replicas ?? 0,
                        })
                      }
                    >
                      scale
                    </button>
                    <button
                      type="button"
                      data-testid={`k8s-restart-${workload.name}`}
                      disabled={action.isPending}
                      onClick={() =>
                        action.mutate({
                          action: "restart",
                          kind: workload.kind,
                          name: workload.name,
                          namespace: workload.namespace,
                        })
                      }
                    >
                      restart
                    </button>
                    <button
                      type="button"
                      data-testid={`k8s-rollback-${workload.name}`}
                      disabled={action.isPending}
                      onClick={() =>
                        action.mutate({
                          action: "rollback",
                          kind: workload.kind,
                          name: workload.name,
                          namespace: workload.namespace,
                        })
                      }
                    >
                      roll back
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <h3>Pods</h3>
      {data.pods.length === 0 ? (
        <p data-testid="k8s-pods-empty">No pods in this scope.</p>
      ) : (
        <table data-testid="k8s-pods">
          <thead>
            <tr>
              <th scope="col">Pod</th>
              <th scope="col">Phase</th>
              <th scope="col">Containers ready</th>
              <th scope="col">Restarts</th>
              <th scope="col">Node</th>
              <th scope="col">Waiting because</th>
              <th scope="col">Output</th>
            </tr>
          </thead>
          <tbody>
            {data.pods.map((pod) => (
              <tr key={`${pod.namespace}/${pod.name}`} data-testid={`k8s-pod-${pod.name}`}>
                <td>{pod.name}</td>
                <td>{pod.phase}</td>
                {/* A RATIO, not a boolean: "1/2 ready" is a sidecar problem and "0/2" is not. */}
                <td data-testid={`k8s-pod-ready-${pod.name}`}>
                  {pod.ready_containers}/{pod.total_containers}
                </td>
                <td data-testid={`k8s-pod-restarts-${pod.name}`}>{pod.restarts}</td>
                <td>{pod.node || "not scheduled"}</td>
                <td data-testid={`k8s-pod-reason-${pod.name}`}>{pod.reason || "—"}</td>
                <td>
                  <button
                    type="button"
                    data-testid={`k8s-pod-logs-${pod.name}`}
                    onClick={() => setOpenPod(openPod === pod.name ? null : pod.name)}
                  >
                    {openPod === pod.name ? "hide logs and events" : "logs and events"}
                  </button>
                  {openPod === pod.name && (
                    <PodDetail projectId={projectId} namespace={pod.namespace} pod={pod.name} />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Services and ingresses</h3>
      <ul data-testid="k8s-services">
        {data.services.length === 0 ? (
          <li>No services in this scope.</li>
        ) : (
          data.services.map((service) => (
            <li key={`${service.namespace}/${service.name}`}>
              {service.name} — {service.type}, {service.cluster_ip}, {service.ports || "no ports"}
            </li>
          ))
        )}
      </ul>
      <ul data-testid="k8s-ingresses">
        {data.ingresses.length === 0 ? (
          <li>No ingresses in this scope.</li>
        ) : (
          data.ingresses.map((ingress) => (
            <li key={`${ingress.namespace}/${ingress.name}`}>
              {ingress.name} — {ingress.hosts.join(", ") || "no hosts"}
            </li>
          ))
        )}
      </ul>

      <h3>Config maps</h3>
      <ul data-testid="k8s-configmaps">
        {data.config_maps.length === 0 ? (
          <li>No config maps in this scope.</li>
        ) : (
          data.config_maps.map((configMap) => (
            <li key={`${configMap.namespace}/${configMap.name}`}>
              {/* KEY NAMES ONLY, and said out loud. A ConfigMap regularly holds what should have been a
                  Secret, and the agent deliberately never sends the values. */}
              {configMap.name} — keys: {configMap.keys.join(", ") || "none"} (values are not read)
            </li>
          ))
        )}
      </ul>

      <h3>Horizontal pod autoscalers</h3>
      {data.horizontal_pod_autoscalers.length === 0 ? (
        <p data-testid="k8s-hpa-empty">No autoscalers in this scope.</p>
      ) : (
        <table data-testid="k8s-hpa">
          <thead>
            <tr>
              <th scope="col">Autoscaler</th>
              <th scope="col">Target</th>
              <th scope="col">Min / max</th>
              <th scope="col">Current</th>
            </tr>
          </thead>
          <tbody>
            {data.horizontal_pod_autoscalers.map((hpa) => (
              <tr key={`${hpa.namespace}/${hpa.name}`} data-testid={`k8s-hpa-${hpa.name}`}>
                <td>{hpa.name}</td>
                <td>{hpa.target}</td>
                <td>
                  {count(hpa.min_replicas)} / {count(hpa.max_replicas)}
                </td>
                <td data-testid={`k8s-hpa-current-${hpa.name}`}>{count(hpa.current_replicas)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
