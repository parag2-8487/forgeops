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
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
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
      <section
        aria-label="Kubernetes"
        data-testid="k8s-dashboard"
        className="rounded-lg border border-border bg-card p-5 space-y-3"
      >
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-base">Cluster status</h3>
          <span className="rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 text-xs px-2 py-0.5 font-medium">
            Agent Disconnected
          </span>
        </div>
        <p data-testid="k8s-error" role="alert" className="text-sm text-muted-foreground">
          The agent did not report the cluster: {inventory.error.message}. This is not the same as
          an empty cluster.
        </p>
        <button
          type="button"
          onClick={() => void inventory.refetch()}
          className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Ask again
        </button>
      </section>
    );
  }

  const raw = inventory.data;
  if (!raw) return null;
  const data: K8sInventory = {
    ...raw,
    namespaces: raw.namespaces ?? [],
    nodes: raw.nodes ?? [],
    workloads: raw.workloads ?? [],
    pods: raw.pods ?? [],
    services: raw.services ?? [],
    ingresses: raw.ingresses ?? [],
    config_maps: raw.config_maps ?? [],
    horizontal_pod_autoscalers: raw.horizontal_pod_autoscalers ?? [],
    partial_reasons: raw.partial_reasons ?? [],
  };
  const freshness = freshnessOf(data.observed_at);

  return (
    <section aria-label="Kubernetes" data-testid="k8s-dashboard" className="space-y-6">
      <Card className="p-5">
        <header className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
            <div>
              <h2 className="text-xl font-semibold tracking-tight">Cluster</h2>
              <p className="flex items-center gap-2 pt-1 font-mono text-xs text-muted-foreground">
                <span data-testid="k8s-context" className="font-semibold text-foreground">
                  {data.cluster_context}
                </span>
                <span>•</span>
                <span data-testid="k8s-server-version">Kubernetes {data.server_version}</span>
                <span>•</span>
                <span data-testid="k8s-freshness">
                  {freshness.kind === "never-reported"
                    ? "never reported"
                    : freshness.kind === "stale"
                      ? `stale — last reported ${freshness.ageSeconds}s ago`
                      : `reported ${freshness.ageSeconds}s ago`}
                </span>
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex items-center gap-2">
                <label
                  htmlFor="k8s-namespace"
                  className="text-xs font-medium text-muted-foreground"
                >
                  Namespace
                </label>
                <select
                  id="k8s-namespace"
                  data-testid="k8s-namespace-select"
                  value={namespace ?? ""}
                  onChange={(event) => setNamespace(event.target.value || null)}
                  className="h-8 rounded-md border border-border bg-background px-2.5 text-xs focus:outline-none focus:ring-1 focus:ring-primary"
                >
                  <option value="">Every namespace</option>
                  {data.namespaces.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </div>
              <EnvironmentSelector
                projectId={projectId}
                value={environmentId}
                onChange={setEnvironmentId}
              />
            </div>
          </div>

          {/* UNREADABLE IS NOT EMPTY, and this is where that promise is kept. */}
          {data.partial_reasons.length > 0 && (
            <div
              data-testid="k8s-partial"
              role="alert"
              className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300"
            >
              <p className="font-semibold">
                Some of this cluster could not be read, so the panels below are incomplete rather
                than empty:
              </p>
              <ul className="list-disc list-inside mt-1 space-y-0.5">
                {data.partial_reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </div>
          )}
        </header>

        {lastOutcome && (
          <p
            data-testid="k8s-last-outcome"
            role="status"
            className="mt-3 rounded-md border border-primary/20 bg-primary/10 p-2.5 text-xs text-foreground font-medium"
          >
            {lastOutcome === "applying"
              ? "Sent to the agent."
              : lastOutcome === "approval-required"
                ? "Waiting for a human to approve it. Nothing has changed in the cluster yet."
                : `The governance gate answered: ${lastOutcome}.`}
          </p>
        )}
      </Card>

      <Card className="p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-semibold tracking-tight">Nodes</h3>
          <Badge variant="outline">{data.nodes.length} nodes</Badge>
        </div>
        {data.nodes.length === 0 ? (
          <p
            data-testid="k8s-nodes-empty"
            className="text-sm text-muted-foreground py-4 text-center"
          >
            No nodes were reported. A cluster that answered has at least one, so check the warning
            above.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table data-testid="k8s-nodes" className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/40 font-mono text-[11px] text-muted-foreground uppercase tracking-wider">
                  <th scope="col" className="p-2.5">
                    Node
                  </th>
                  <th scope="col" className="p-2.5">
                    Ready
                  </th>
                  <th scope="col" className="p-2.5">
                    Kubelet
                  </th>
                  <th scope="col" className="p-2.5">
                    Allocatable CPU / memory
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {data.nodes.map((node) => (
                  <tr
                    key={node.name}
                    data-testid={`k8s-node-${node.name}`}
                    className="hover:bg-muted/20 transition-colors"
                  >
                    <td className="p-2.5 font-medium font-mono text-foreground">{node.name}</td>
                    <td data-testid={`k8s-node-ready-${node.name}`} className="p-2.5">
                      <Badge
                        variant={
                          node.ready === null ? "outline" : node.ready ? "success" : "destructive"
                        }
                        className="text-[10px] uppercase font-mono tracking-wider"
                      >
                        {node.ready === null
                          ? "has not reported"
                          : node.ready
                            ? "Ready"
                            : "NotReady"}
                      </Badge>
                    </td>
                    <td className="p-2.5 font-mono text-muted-foreground">
                      {node.kubelet_version}
                    </td>
                    <td className="p-2.5 font-mono text-muted-foreground">
                      {node.allocatable.cpu || "not reported"} /{" "}
                      {node.allocatable.memory || "not reported"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card className="p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-semibold tracking-tight">Workloads</h3>
          <Badge variant="outline">{data.workloads.length} workloads</Badge>
        </div>
        {data.workloads.length === 0 ? (
          <p
            data-testid="k8s-workloads-empty"
            className="text-sm text-muted-foreground py-4 text-center"
          >
            {namespace
              ? `No deployments, stateful sets or daemon sets in ${namespace}.`
              : "No deployments, stateful sets or daemon sets in any namespace."}
          </p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table data-testid="k8s-workloads" className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/40 font-mono text-[11px] text-muted-foreground uppercase tracking-wider">
                  <th scope="col" className="p-2.5">
                    Workload
                  </th>
                  <th scope="col" className="p-2.5">
                    Namespace
                  </th>
                  <th scope="col" className="p-2.5">
                    Ready / desired
                  </th>
                  <th scope="col" className="p-2.5">
                    Image
                  </th>
                  <th scope="col" className="p-2.5 text-right">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {data.workloads.map((workload) => {
                  const key = `${workload.namespace}/${workload.kind}/${workload.name}`;
                  return (
                    <tr
                      key={key}
                      data-testid={`k8s-workload-${workload.name}`}
                      className="hover:bg-muted/20 transition-colors"
                    >
                      <td className="p-2.5 font-medium font-mono text-foreground">
                        {workload.kind}/{workload.name}
                      </td>
                      <td className="p-2.5 font-mono text-muted-foreground">
                        {workload.namespace}
                      </td>
                      <td
                        data-testid={`k8s-replicas-${workload.name}`}
                        className="p-2.5 font-mono font-medium"
                      >
                        {count(workload.ready_replicas)} / {count(workload.desired_replicas)}
                      </td>
                      <td
                        className="p-2.5 font-mono text-muted-foreground truncate max-w-[200px]"
                        title={workload.images.join(", ")}
                      >
                        {workload.images.join(", ") || "not reported"}
                      </td>
                      <td className="p-2.5 text-right">
                        <div className="flex flex-wrap items-center justify-end gap-1.5">
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
                            className="h-7 w-14 px-1.5 text-xs rounded border border-border bg-background font-mono text-center"
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
                            className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded border border-border bg-background hover:bg-muted text-foreground transition-colors disabled:opacity-40"
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
                            className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded border border-border bg-background hover:bg-muted text-foreground transition-colors disabled:opacity-40"
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
                            className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded border border-border bg-background hover:bg-muted text-foreground transition-colors disabled:opacity-40"
                          >
                            roll back
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card className="p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-semibold tracking-tight">Pods</h3>
          <Badge variant="outline">{data.pods.length} pods</Badge>
        </div>
        {data.pods.length === 0 ? (
          <p
            data-testid="k8s-pods-empty"
            className="text-sm text-muted-foreground py-4 text-center"
          >
            No pods in this scope.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table data-testid="k8s-pods" className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/40 font-mono text-[11px] text-muted-foreground uppercase tracking-wider">
                  <th scope="col" className="p-2.5">
                    Pod
                  </th>
                  <th scope="col" className="p-2.5">
                    Phase
                  </th>
                  <th scope="col" className="p-2.5">
                    Containers ready
                  </th>
                  <th scope="col" className="p-2.5">
                    Restarts
                  </th>
                  <th scope="col" className="p-2.5">
                    Node
                  </th>
                  <th scope="col" className="p-2.5">
                    Waiting because
                  </th>
                  <th scope="col" className="p-2.5 text-right">
                    Output
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {data.pods.map((pod) => (
                  <tr
                    key={`${pod.namespace}/${pod.name}`}
                    data-testid={`k8s-pod-${pod.name}`}
                    className="hover:bg-muted/20 transition-colors"
                  >
                    <td className="p-2.5 font-medium font-mono text-foreground">{pod.name}</td>
                    <td className="p-2.5">
                      <Badge
                        variant={pod.phase === "Running" ? "success" : "outline"}
                        className="text-[10px] uppercase font-mono tracking-wider"
                      >
                        {pod.phase}
                      </Badge>
                    </td>
                    <td data-testid={`k8s-pod-ready-${pod.name}`} className="p-2.5 font-mono">
                      {pod.ready_containers}/{pod.total_containers}
                    </td>
                    <td
                      data-testid={`k8s-pod-restarts-${pod.name}`}
                      className="p-2.5 font-mono text-muted-foreground"
                    >
                      {pod.restarts}
                    </td>
                    <td className="p-2.5 font-mono text-muted-foreground">
                      {pod.node || "not scheduled"}
                    </td>
                    <td
                      data-testid={`k8s-pod-reason-${pod.name}`}
                      className="p-2.5 font-mono text-muted-foreground"
                    >
                      {pod.reason || "—"}
                    </td>
                    <td className="p-2.5 text-right">
                      <button
                        type="button"
                        data-testid={`k8s-pod-logs-${pod.name}`}
                        onClick={() => setOpenPod(openPod === pod.name ? null : pod.name)}
                        className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded border border-border bg-background hover:bg-muted text-foreground transition-colors"
                      >
                        {openPod === pod.name ? "hide logs and events" : "logs and events"}
                      </button>
                      {openPod === pod.name && (
                        <div className="w-full mt-2 text-left">
                          <PodDetail
                            projectId={projectId}
                            namespace={pod.namespace}
                            pod={pod.name}
                          />
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-4 space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-sm">Services and ingresses</h3>
            <Badge variant="outline">{data.services.length + data.ingresses.length}</Badge>
          </div>
          <div className="space-y-3">
            <div>
              <p className="text-xs font-medium text-muted-foreground mb-1 uppercase tracking-wider font-mono">
                Services
              </p>
              <ul
                data-testid="k8s-services"
                className="space-y-1 font-mono text-xs max-h-40 overflow-y-auto rounded border border-border bg-muted/10 p-2"
              >
                {data.services.length === 0 ? (
                  <li className="text-muted-foreground">No services in this scope.</li>
                ) : (
                  data.services.map((service) => (
                    <li
                      key={`${service.namespace}/${service.name}`}
                      className="p-1 rounded bg-card border border-border/40 text-[11px] flex justify-between"
                    >
                      <span className="font-medium text-foreground">{service.name}</span>
                      <span className="text-muted-foreground">
                        {service.type}, {service.cluster_ip}, {service.ports || "no ports"}
                      </span>
                    </li>
                  ))
                )}
              </ul>
            </div>
            <div>
              <p className="text-xs font-medium text-muted-foreground mb-1 uppercase tracking-wider font-mono">
                Ingresses
              </p>
              <ul
                data-testid="k8s-ingresses"
                className="space-y-1 font-mono text-xs max-h-40 overflow-y-auto rounded border border-border bg-muted/10 p-2"
              >
                {data.ingresses.length === 0 ? (
                  <li className="text-muted-foreground">No ingresses in this scope.</li>
                ) : (
                  data.ingresses.map((ingress) => (
                    <li
                      key={`${ingress.namespace}/${ingress.name}`}
                      className="p-1 rounded bg-card border border-border/40 text-[11px] flex justify-between"
                    >
                      <span className="font-medium text-foreground">{ingress.name}</span>
                      <span className="text-muted-foreground">
                        {ingress.hosts.join(", ") || "no hosts"}
                      </span>
                    </li>
                  ))
                )}
              </ul>
            </div>
          </div>
        </Card>

        <Card className="p-4 space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-sm">Config maps</h3>
            <Badge variant="outline">{data.config_maps.length}</Badge>
          </div>
          <div className="max-h-56 overflow-y-auto rounded-md border border-border bg-muted/10 p-2">
            <ul data-testid="k8s-configmaps" className="space-y-1 font-mono text-xs">
              {data.config_maps.length === 0 ? (
                <li className="text-muted-foreground p-1">No config maps in this scope.</li>
              ) : (
                data.config_maps.map((configMap) => (
                  <li
                    key={`${configMap.namespace}/${configMap.name}`}
                    className="p-1.5 rounded bg-card border border-border/40 text-[11px]"
                  >
                    <span className="font-medium text-foreground">{configMap.name}</span>
                    <span className="text-muted-foreground ml-2">
                      — keys: {configMap.keys.join(", ") || "none"} (values are not read)
                    </span>
                  </li>
                ))
              )}
            </ul>
          </div>
        </Card>
      </div>

      <Card className="p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-semibold tracking-tight">Horizontal pod autoscalers</h3>
          <Badge variant="outline">{data.horizontal_pod_autoscalers.length}</Badge>
        </div>
        {data.horizontal_pod_autoscalers.length === 0 ? (
          <p data-testid="k8s-hpa-empty" className="text-sm text-muted-foreground py-2 text-center">
            No autoscalers in this scope.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table data-testid="k8s-hpa" className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/40 font-mono text-[11px] text-muted-foreground uppercase tracking-wider">
                  <th scope="col" className="p-2.5">
                    Autoscaler
                  </th>
                  <th scope="col" className="p-2.5">
                    Target
                  </th>
                  <th scope="col" className="p-2.5">
                    Min / max
                  </th>
                  <th scope="col" className="p-2.5 text-right">
                    Current
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {data.horizontal_pod_autoscalers.map((hpa) => (
                  <tr
                    key={`${hpa.namespace}/${hpa.name}`}
                    data-testid={`k8s-hpa-${hpa.name}`}
                    className="hover:bg-muted/20 transition-colors"
                  >
                    <td className="p-2.5 font-medium font-mono text-foreground">{hpa.name}</td>
                    <td className="p-2.5 font-mono text-muted-foreground">{hpa.target}</td>
                    <td className="p-2.5 font-mono text-muted-foreground">
                      {count(hpa.min_replicas)} / {count(hpa.max_replicas)}
                    </td>
                    <td
                      data-testid={`k8s-hpa-current-${hpa.name}`}
                      className="p-2.5 font-mono font-medium text-right"
                    >
                      {count(hpa.current_replicas)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </section>
  );
}
