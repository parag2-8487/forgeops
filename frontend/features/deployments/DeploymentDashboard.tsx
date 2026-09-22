"use client";

/**
 * §2.2's deployment dashboard, and §2.3's timeline read from the same rows.
 *
 * THE DISTINCTION THIS SCREEN EXISTS TO PRESERVE. A deployment has three outcomes that look like two:
 *
 *   applied   the manifests reached the cluster AND every workload converged
 *   degraded  the manifests reached the cluster and at least one workload did NOT converge
 *   failed    the apply was refused; nothing is running that was not running before
 *
 * `degraded` is the one a dashboard usually loses. It is not a failure — nothing needs retrying, the
 * objects are there — and it is not a success, because the thing the operator asked for is not running. A
 * green tick on it would be the worst version of this codebase's recurring defect: a plausible-looking
 * status that a human acts on.
 *
 * SO `healthy` IS RENDERED AS THREE STATES, NOT TWO. `null` means nothing verified it (in flight, or the
 * apply failed before anything could be checked), `false` means the workloads were checked and were not
 * ready, `true` means they converged. An interface that showed `null` and `false` the same way would tell
 * an operator that a deployment currently mid-rollout has already failed.
 *
 * THE ROLLBACK TARGET IS READ, NOT INFERRED. The server answers with the newest deployment whose
 * workloads actually converged, and with `null` plus a reason when there is not one. Computing "the
 * previous deployment" here would offer a rollback to a degraded state — which is exactly what the
 * server's `stable` flag exists to prevent.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  DeploymentLogStream,
  DeploymentResult,
  type DeploymentReport,
} from "@/features/deployments/DeploymentResult";
import { EnvironmentSelector } from "@/features/environments/EnvironmentManager";
import { api, queryKeys } from "@/lib/api";

export type Deployment = {
  id: string;
  environment_id: string;
  change_set_id: string | null;
  status: string;
  healthy: boolean | null;
  stable: boolean;
  manifests: string[];
  manifest_digest: string;
  cluster_context: string | null;
  namespace: string | null;
  report: Record<string, unknown> | null;
  created_at: string | null;
  completed_at: string | null;
};

type DeploymentList = { deployments: Deployment[] };

type DeploymentDecision = {
  deployment: Deployment;
  change_set_id: string;
  outcome: string;
  requires_approval: boolean;
  environment: string;
  blast_radius_score: number;
  blast_radius_verdict: string;
};

type RollbackTarget = { target: Deployment | null; reason: string };

/** The three-state health sentence. Never a tick, never a cross alone. */
export function healthSentence(deployment: Deployment): string {
  if (deployment.status === "failed") {
    return "The apply was refused — nothing was deployed, so no workload was checked";
  }
  if (deployment.healthy === null) {
    return "No workload has been verified yet";
  }
  if (deployment.healthy) {
    return "Every workload converged";
  }
  return "Applied, but at least one workload did not converge";
}

export function DeploymentDashboard({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [environmentId, setEnvironmentId] = useState<string | null>(null);
  const [manifests, setManifests] = useState("k8s/deployment.yaml\nk8s/service.yaml");
  const [problem, setProblem] = useState<string | null>(null);
  const [decision, setDecision] = useState<DeploymentDecision | null>(null);

  const deployments = useQuery<DeploymentList>({
    queryKey: queryKeys.deployments.list(projectId),
    queryFn: () => api.get<DeploymentList>(`/projects/${projectId}/deployments`),
  });

  const deploy = useMutation({
    mutationFn: () =>
      api.post<DeploymentDecision>(`/projects/${projectId}/deployments`, {
        environment_id: environmentId,
        manifests: manifests
          .split("\n")
          .map((line) => line.trim())
          .filter((line) => line.length > 0),
      }),
    onSuccess: async (result) => {
      setProblem(null);
      setDecision(result);
      await queryClient.invalidateQueries({ queryKey: queryKeys.deployments.list(projectId) });
    },
    // The server's own refusal, verbatim. The useful ones name a bound and a count, or say which
    // mechanism demanded a human.
    onError: (error: unknown) => {
      setDecision(null);
      setProblem(errorText(error));
    },
  });

  const list = deployments.data?.deployments ?? [];

  return (
    <section aria-label="Deployments">
      <h2>Deploy</h2>

      <EnvironmentSelector
        projectId={projectId}
        value={environmentId}
        onChange={setEnvironmentId}
        label="Deploy to"
      />

      <form
        onSubmit={(event) => {
          event.preventDefault();
          deploy.mutate();
        }}
      >
        <label htmlFor="deployment-manifests">Manifests, one path per line</label>
        <textarea
          id="deployment-manifests"
          value={manifests}
          onChange={(event) => setManifests(event.target.value)}
          rows={4}
        />
        <p>
          Paths are relative to the project the agent holds. At most 32 per deployment — the agent
          enforces the same bound, so a larger set could never be delivered.
        </p>
        <button
          type="submit"
          data-testid="deploy-submit"
          disabled={deploy.isPending || environmentId === null}
        >
          {deploy.isPending ? "Requesting…" : "Request deployment"}
        </button>
      </form>

      {decision ? (
        <p data-testid="deployment-decision">
          {decision.requires_approval
            ? `Waiting for human approval before deploying to ${decision.environment}. Change set ${decision.change_set_id}.`
            : `Sent to the agent for ${decision.environment}. Change set ${decision.change_set_id}.`}{" "}
          Blast radius {decision.blast_radius_score} ({decision.blast_radius_verdict}).
        </p>
      ) : null}

      {problem ? (
        <p role="alert" data-testid="deployment-problem">
          {problem}
        </p>
      ) : null}

      <h3>History</h3>
      {deployments.isError ? (
        <p role="alert" data-testid="deployments-error">
          Deployment history could not be loaded. This is not the same as never having deployed.
        </p>
      ) : deployments.isPending ? (
        <p data-testid="deployments-loading">Loading…</p>
      ) : list.length === 0 ? (
        <p data-testid="deployments-empty">Nothing has been deployed for this project yet.</p>
      ) : (
        <ol data-testid="deployment-history">
          {list.map((deployment) => (
            <li key={deployment.id} data-testid={`deployment-${deployment.id}`}>
              <span data-testid={`deployment-status-${deployment.id}`}>{deployment.status}</span>{" "}
              <span data-testid={`deployment-health-${deployment.id}`}>
                {healthSentence(deployment)}
              </span>{" "}
              <span>
                {deployment.cluster_context ?? "no cluster context recorded"}
                {deployment.namespace ? ` / ${deployment.namespace}` : ""}
              </span>{" "}
              <span>{deployment.manifests.length} manifest(s)</span>{" "}
              {deployment.stable ? (
                <span data-testid={`deployment-stable-${deployment.id}`}>
                  a stable state a rollback can return to
                </span>
              ) : null}
              <DeploymentResult
                report={deployment.report as DeploymentReport | null}
                status={deployment.status}
              />
              <DeploymentLogStream
                projectId={projectId}
                deploymentId={deployment.id}
                deliverable={deployment.change_set_id !== null && deployment.status === "applying"}
              />
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/** What a rollback would restore, read from the server rather than inferred from the list. */
export function RollbackTargetPanel({
  projectId,
  environmentId,
}: {
  projectId: string;
  environmentId: string;
}) {
  const target = useQuery<RollbackTarget>({
    queryKey: queryKeys.deployments.rollbackTarget(environmentId),
    queryFn: () =>
      api.get<RollbackTarget>(
        `/projects/${projectId}/deployments/rollback-target?environment_id=${environmentId}`,
      ),
  });

  if (target.isPending) {
    return <p data-testid="rollback-loading">Looking for a stable state…</p>;
  }
  if (target.isError) {
    return (
      <p role="alert" data-testid="rollback-error">
        The rollback target could not be read, so no rollback is offered. Nothing has been changed.
      </p>
    );
  }
  const answer = target.data;
  if (!answer?.target) {
    // NO BUTTON AT ALL. Offering a rollback with nothing to roll back to would restore a broken
    // deployment while reporting success.
    return (
      <p data-testid="rollback-none">
        {answer?.reason ?? "There is no stable deployment to roll back to."}
      </p>
    );
  }
  return (
    <p data-testid="rollback-target">
      Rolling back would restore the deployment of {answer.target.manifests.length} manifest(s) from{" "}
      {answer.target.created_at ?? "an unrecorded time"} — {answer.reason}.
    </p>
  );
}

function errorText(error: unknown): string {
  const detail = (error as { problem?: { detail?: string } })?.problem?.detail;
  if (typeof detail === "string" && detail.length > 0) {
    return detail;
  }
  return error instanceof Error ? error.message : "The request failed.";
}
