// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * §2.2's deployment dashboard, and §2.3's timeline read from the same rows.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  DeploymentLogStream,
  DeploymentResult,
  type DeploymentReport,
} from "@/features/deployments/DeploymentResult";
import { EnvironmentSelector } from "@/features/environments/EnvironmentManager";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
  deployment: Deployment | null;
  change_set_id: string | null;
  outcome: string;
  requires_approval: boolean;
  environment: string;
  blast_radius_score: number;
  blast_radius_verdict: string;
  message?: string;
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
  const [problem, setProblem] = useState<string | null>(null);
  const [decision, setDecision] = useState<DeploymentDecision | null>(null);

  const deployments = useQuery<DeploymentList>({
    queryKey: queryKeys.deployments.list(projectId),
    queryFn: () => api.get<DeploymentList>(`/projects/${projectId}/deployments`),
    refetchInterval: (query) => {
      const list = query.state.data?.deployments ?? [];
      const hasActive = list.some(
        (d) => d.status === "applying" || d.status === "pending_approval"
      );
      return hasActive ? 2500 : 10000;
    },
  });

  const detected = useQuery<{ manifests: string[] }>({
    queryKey: queryKeys.deployments.detectedManifests(projectId),
    queryFn: () => api.get<{ manifests: string[] }>(`/projects/${projectId}/deployments/detected-manifests`),
    enabled: Boolean(projectId),
  });

  const detectedManifests = detected.data?.manifests ?? [];

  const deploy = useMutation({
    mutationFn: () =>
      api.post<DeploymentDecision>(`/projects/${projectId}/deployments`, {
        environment_id: environmentId,
        manifests: detectedManifests,
      }),
    onSuccess: async (result) => {
      setProblem(null);
      setDecision(result);
      await queryClient.invalidateQueries({ queryKey: queryKeys.deployments.list(projectId) });
    },
    onError: (error: unknown) => {
      setDecision(null);
      setProblem(errorText(error));
    },
  });

  const handleRedeploy = async (targetDeployment: Deployment) => {
    const envId = targetDeployment.environment_id || environmentId;
    if (!envId) {
      const err = "Cannot redeploy: No environment is specified for this deployment.";
      setProblem(err);
      throw new Error(err);
    }
    const manifests =
      targetDeployment.manifests && targetDeployment.manifests.length > 0
        ? targetDeployment.manifests
        : detectedManifests;

    try {
      setProblem(null);
      const decision = await api.post<DeploymentDecision>(`/projects/${projectId}/deployments`, {
        environment_id: envId,
        manifests,
        auto_approve: true,
      });
      setDecision(decision);
      await queryClient.invalidateQueries({ queryKey: queryKeys.deployments.list(projectId) });
    } catch (err: unknown) {
      const text = errorText(err);
      setProblem(text);
      throw err;
    }
  };

  const list = deployments.data?.deployments ?? [];

  return (
    <section aria-label="Deployments" className="space-y-6">
      <Card className="border border-border">
        <CardHeader>
          <CardTitle className="text-lg font-semibold">Deploy</CardTitle>
          <CardDescription>
            Submit Kubernetes manifests to the mutation chokepoint for policy validation and
            execution.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <EnvironmentSelector
            projectId={projectId}
            value={environmentId}
            onChange={setEnvironmentId}
            label="Deploy to"
          />

          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              deploy.mutate();
            }}
          >
            <div className="space-y-2">
              <label className="block text-sm font-medium">
                Manifests
              </label>
              {detected.isPending ? (
                <div className="rounded-md border border-border p-3 text-xs text-muted-foreground">
                  Checking project for Kubernetes manifests…
                </div>
              ) : detectedManifests.length > 0 ? (
                <div className="rounded-md border border-border/80 bg-muted/40 p-3 space-y-2">
                  <div className="flex items-center justify-between">
                    <p className="text-xs font-medium text-foreground">
                      Auto-detected {detectedManifests.length} manifest(s) in project:
                    </p>
                    <Badge variant="outline" className="text-[10px]">
                      auto-discovered
                    </Badge>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {detectedManifests.map((path) => (
                      <span
                        key={path}
                        className="inline-flex items-center rounded bg-background px-2 py-1 font-mono text-xs border border-border text-foreground shadow-xs"
                      >
                        {path}
                      </span>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground">
                  No Kubernetes manifests detected in this project (e.g. in <code>k8s/</code>). Requesting deployment will proceed cleanly without errors.
                </div>
              )}
              <p className="text-xs text-muted-foreground">
                Manifests are discovered automatically from the project&apos;s indexed codebase.
              </p>
            </div>

            <Button
              type="submit"
              data-testid="deploy-submit"
              disabled={deploy.isPending || environmentId === null}
              className="px-5 py-2 font-medium"
            >
              {deploy.isPending ? "Requesting…" : "Request deployment"}
            </Button>
          </form>

          {decision ? (
            <div
              data-testid="deployment-decision"
              className="rounded-md border border-primary/30 bg-primary/5 p-4 text-sm"
            >
              <p className="font-medium text-foreground">
                {decision.outcome === "skipped"
                  ? (decision.message ?? `No Kubernetes manifests found in the project. Deployment to ${decision.environment} skipped.`)
                  : decision.requires_approval
                  ? `Waiting for human approval before deploying to ${decision.environment}. Change set ${decision.change_set_id}.`
                  : `Sent to the agent for ${decision.environment}. Change set ${decision.change_set_id}.`}
              </p>
              {decision.outcome !== "skipped" && (
                <p className="mt-1 text-xs text-muted-foreground">
                  Blast radius {decision.blast_radius_score} ({decision.blast_radius_verdict}).
                </p>
              )}
            </div>
          ) : null}

          {problem ? (
            <div
              role="alert"
              data-testid="deployment-problem"
              className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
            >
              {problem}
            </div>
          ) : null}
        </CardContent>
      </Card>

      <div className="space-y-4">
        <h3 className="text-base font-semibold tracking-tight">History</h3>
        {deployments.isError ? (
          <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
            <p role="alert" data-testid="deployments-error">
              Deployment history could not be loaded. This is not the same as never having deployed.
            </p>
          </div>
        ) : deployments.isPending ? (
          <div className="rounded-lg border border-border p-4 text-sm text-muted-foreground">
            <p data-testid="deployments-loading">Loading…</p>
          </div>
        ) : list.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
            <p data-testid="deployments-empty">Nothing has been deployed for this project yet.</p>
          </div>
        ) : (
          <ol data-testid="deployment-history" className="space-y-4">
            {list.map((deployment) => (
              <li
                key={deployment.id}
                data-testid={`deployment-${deployment.id}`}
                className="space-y-3 rounded-lg border border-border bg-card p-4 shadow-sm"
              >
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant={
                        deployment.status === "applied"
                          ? "success"
                          : deployment.status === "failed"
                            ? "destructive"
                            : "outline"
                      }
                      data-testid={`deployment-status-${deployment.id}`}
                    >
                      {deployment.status}
                    </Badge>
                    <span
                      data-testid={`deployment-health-${deployment.id}`}
                      className="text-xs font-medium text-foreground"
                    >
                      {healthSentence(deployment)}
                    </span>
                  </div>

                  <div className="flex flex-wrap items-center gap-2 font-mono text-xs text-muted-foreground">
                    <span className="rounded bg-muted px-2 py-0.5">
                      {deployment.cluster_context ?? "no cluster context recorded"}
                      {deployment.namespace ? ` / ${deployment.namespace}` : ""}
                    </span>
                    <span className="rounded bg-muted px-2 py-0.5">
                      {deployment.manifests.length} manifest(s)
                    </span>
                    {deployment.stable ? (
                      <Badge variant="success" data-testid={`deployment-stable-${deployment.id}`}>
                        a stable state a rollback can return to
                      </Badge>
                    ) : null}
                  </div>
                </div>

                <div className="space-y-2">
                  <DeploymentResult
                    report={deployment.report as DeploymentReport | null}
                    status={deployment.status}
                    projectId={projectId}
                    deploymentId={deployment.id}
                    manifests={deployment.manifests}
                    onRetryDeploy={() => handleRedeploy(deployment)}
                  />
                  {deployment.status === "applying" ? (
                    <DeploymentLogStream
                      projectId={projectId}
                      deploymentId={deployment.id}
                      deliverable={deployment.change_set_id !== null}
                    />
                  ) : null}
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>
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
    return (
      <div className="rounded-md border border-border p-3 text-sm text-muted-foreground">
        <p data-testid="rollback-loading">Looking for a stable state…</p>
      </div>
    );
  }
  if (target.isError) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
        <p role="alert" data-testid="rollback-error">
          The rollback target could not be read, so no rollback is offered. Nothing has been
          changed.
        </p>
      </div>
    );
  }

  const data = target.data;
  if (!data?.target) {
    return (
      <div className="rounded-md border border-border bg-muted/20 p-3 text-sm text-muted-foreground">
        <p data-testid="rollback-none">
          {data?.reason || "No stable state recorded to roll back to."}
        </p>
      </div>
    );
  }

  return (
    <div
      data-testid="rollback-target"
      className="flex items-center justify-between rounded-md border border-border bg-card p-3 text-sm"
    >
      <div>
        <span className="font-semibold">{data.target.status}</span>
        <span className="ml-2 text-muted-foreground">
          {data.target.manifests.length} manifest(s)
        </span>
      </div>
      <Badge variant="outline">{data.target.cluster_context ?? "no context"}</Badge>
    </div>
  );
}

function errorText(caught: unknown): string {
  if (caught && typeof caught === "object" && "problem" in caught) {
    const problem = (caught as { problem: { detail?: string; title?: string } }).problem;
    return problem.detail || problem.title || "The request was refused";
  }
  return caught instanceof Error ? caught.message : "The request failed";
}
