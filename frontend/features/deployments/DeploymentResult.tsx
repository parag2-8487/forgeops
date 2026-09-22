"use client";

/**
 * §2.2's deployment results: the agent's structured report, and the live `log` stream while it runs.
 *
 * The report is rendered as its parts — what was applied, each workload's readiness, the kubectl version —
 * rather than as raw JSON, because the point of a structured report is that the distinctions in it survive
 * to the screen. A workload with `ready: false` and one with no entry at all are different facts.
 *
 * The live stream is opened only for a deployment that has actually been delivered. An EventSource on a
 * `pending_approval` deployment would show an empty log, and an operator would read that as "nothing is
 * happening" rather than "a human has not approved it".
 */

import { useEffect, useRef, useState } from "react";

export type WorkloadHealth = {
  kind?: string;
  name?: string;
  ready?: boolean;
  detail?: string;
  waited_seconds?: number;
};

export type DeploymentReport = {
  applied?: string[];
  workloads?: WorkloadHealth[];
  healthy?: boolean;
  kubectl_version?: string;
  cluster_context?: string;
  namespace?: string;
};

/** Words for a workload's readiness, keeping "no entry" apart from "not ready". */
export function readinessWord(workload: WorkloadHealth): string {
  if (workload.ready === undefined) return "not reported";
  return workload.ready ? "ready" : "not ready";
}

export function DeploymentResult({
  report,
  status,
}: {
  report: DeploymentReport | null;
  status: string;
}) {
  if (report === null) {
    return (
      <div data-testid="deployment-result">
        <p data-testid="deployment-result-absent">
          {status === "pending_approval"
            ? "No result yet: this is waiting for a human to approve it."
            : "No result has been reported for this deployment yet."}
        </p>
      </div>
    );
  }

  const applied = report.applied ?? [];
  const workloads = report.workloads ?? [];

  return (
    <div data-testid="deployment-result">
      <p data-testid="deployment-result-applied">
        {applied.length === 0
          ? "The report names no applied objects."
          : `${applied.length} object(s) applied`}
      </p>
      {applied.length > 0 && (
        <ul data-testid="deployment-result-objects">
          {applied.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {workloads.length === 0 ? (
        <p data-testid="deployment-result-unverified">
          {/* Applied and never verified is the case `degraded` exists for; it must not read as success. */}
          No workload was waited on, so nothing here verifies that anything is running.
        </p>
      ) : (
        <ul data-testid="deployment-result-workloads">
          {workloads.map((workload, index) => (
            <li
              key={`${workload.kind}/${workload.name}-${index}`}
              data-testid={`workload-${workload.name}`}
            >
              {workload.kind}/{workload.name}: {readinessWord(workload)}
              {workload.waited_seconds !== undefined ? ` after ${workload.waited_seconds}s` : ""}
              {workload.detail ? ` — ${workload.detail}` : ""}
            </li>
          ))}
        </ul>
      )}
      {report.kubectl_version && (
        <p data-testid="deployment-result-client">applied with {report.kubectl_version}</p>
      )}
    </div>
  );
}

/**
 * The live `log` stream for one deployment.
 *
 * `EventSource` rather than a polled fetch: the server already publishes frames as they arrive, and polling
 * would turn a stream into a series of snapshots with gaps between them.
 */
export function DeploymentLogStream({
  projectId,
  deploymentId,
  deliverable,
}: {
  projectId: string;
  deploymentId: string;
  /** False for a deployment with no agent command yet. Opening a stream then shows an empty log. */
  deliverable: boolean;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [state, setState] = useState<"idle" | "open" | "closed" | "error">(
    // "open" from the start when a stream will be opened, so the effect does not set state
    // synchronously ? the lint rule is right that doing so cascades a render for no reason.
    deliverable ? "open" : "idle",
  );
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!deliverable) return;
    const base = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";
    const source = new EventSource(
      `${base}/projects/${projectId}/deployments/${deploymentId}/logs`,
      {
        withCredentials: true,
      },
    );
    sourceRef.current = source;
    source.addEventListener("log", (event) => {
      const payload = (event as MessageEvent).data;
      setLines((previous) => [...previous, String(payload)]);
    });
    source.addEventListener("complete", () => {
      setState("closed");
      source.close();
    });
    source.addEventListener("error", () => {
      // An error is a state of its own, not silence: a stream that failed and one that ended look
      // identical without this.
      setState("error");
    });
    return () => {
      source.close();
      sourceRef.current = null;
    };
  }, [projectId, deploymentId, deliverable]);

  if (!deliverable) {
    return (
      <p data-testid="deployment-log-undeliverable">
        This deployment has not been sent to an agent, so it has no output yet.
      </p>
    );
  }

  return (
    <div data-testid="deployment-log">
      <p data-testid="deployment-log-state">
        {state === "open"
          ? "streaming"
          : state === "closed"
            ? "the deployment settled and the stream closed"
            : state === "error"
              ? "the stream failed; this is not the same as the deployment producing no output"
              : "not started"}
      </p>
      {lines.length === 0 ? (
        <p data-testid="deployment-log-empty">No output yet.</p>
      ) : (
        <pre data-testid="deployment-log-lines">{lines.join("\n")}</pre>
      )}
    </div>
  );
}
