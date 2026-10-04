// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * §2.2's deployment results: the agent's structured report, and the live `log` stream while it runs.
 */

import { useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { getAccessToken } from "@/lib/session";

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
      <div
        data-testid="deployment-result"
        className="rounded-md border border-border/60 bg-muted/20 p-3 text-xs text-muted-foreground"
      >
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
    <div
      data-testid="deployment-result"
      className="space-y-2 rounded-md border border-border bg-card p-3 text-xs"
    >
      <div className="flex items-center justify-between">
        <p data-testid="deployment-result-applied" className="font-semibold text-foreground">
          {applied.length === 0
            ? "The report names no applied objects."
            : `${applied.length} object(s) applied`}
        </p>
        {report.kubectl_version && (
          <p data-testid="deployment-result-client" className="font-mono text-muted-foreground">
            applied with {report.kubectl_version}
          </p>
        )}
      </div>

      {applied.length > 0 && (
        <ul
          data-testid="deployment-result-objects"
          className="space-y-1 font-mono text-muted-foreground pl-2 border-l border-border"
        >
          {applied.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}

      {workloads.length === 0 ? (
        <p data-testid="deployment-result-unverified" className="text-muted-foreground italic">
          No workload was waited on, so nothing here verifies that anything is running.
        </p>
      ) : (
        <ul data-testid="deployment-result-workloads" className="space-y-1 pt-1">
          {workloads.map((workload, index) => (
            <li
              key={`${workload.kind}/${workload.name}-${index}`}
              data-testid={`workload-${workload.name}`}
              className="flex items-center gap-2 font-mono"
            >
              <Badge variant={workload.ready ? "success" : "destructive"}>
                {readinessWord(workload)}
              </Badge>
              <span>
                {workload.kind}/{workload.name}
                {workload.waited_seconds !== undefined ? ` after ${workload.waited_seconds}s` : ""}
                {workload.detail ? ` — ${workload.detail}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * The live `log` stream for one deployment.
 */
export function DeploymentLogStream({
  projectId,
  deploymentId,
  deliverable,
}: {
  projectId: string;
  deploymentId: string;
  deliverable: boolean;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [state, setState] = useState<"idle" | "open" | "closed" | "error">(
    deliverable ? "open" : "idle",
  );
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!deliverable) return;
    const base = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";
    const token = getAccessToken();
    const query = token ? `?token=${encodeURIComponent(token)}` : "";
    const source = new EventSource(
      `${base}/projects/${projectId}/deployments/${deploymentId}/logs${query}`,
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
      setState("error");
    });
    return () => {
      source.close();
      sourceRef.current = null;
    };
  }, [projectId, deploymentId, deliverable]);

  if (!deliverable) {
    return (
      <div className="rounded-md border border-border/40 bg-muted/10 p-2 text-xs text-muted-foreground">
        <p data-testid="deployment-log-undeliverable">
          This deployment has not been sent to an agent, so it has no output yet.
        </p>
      </div>
    );
  }

  return (
    <div
      data-testid="deployment-log"
      className="space-y-1.5 rounded-md border border-zinc-800 bg-zinc-950 p-3 text-xs text-zinc-100"
    >
      <div className="flex items-center justify-between border-b border-zinc-800/80 pb-1.5 text-zinc-400">
        <span className="font-mono text-[10px] uppercase tracking-wider">Deployment Logs</span>
        <p data-testid="deployment-log-state" className="text-[11px]">
          {state === "open"
            ? "streaming"
            : state === "closed"
              ? "the deployment settled and the stream closed"
              : state === "error"
                ? "the stream failed; this is not the same as the deployment producing no output"
                : "not started"}
        </p>
      </div>
      {lines.length === 0 ? (
        <p data-testid="deployment-log-empty" className="py-2 text-zinc-500 font-mono">
          No output yet.
        </p>
      ) : (
        <pre
          data-testid="deployment-log-lines"
          className="max-h-64 overflow-y-auto whitespace-pre-wrap font-mono text-zinc-300"
        >
          {lines.join("\n")}
        </pre>
      )}
    </div>
  );
}
