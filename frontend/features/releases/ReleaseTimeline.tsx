// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * §2.3's release timeline and side-by-side comparison.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, queryKeys } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export type TimelineDeployment = {
  id: string;
  environment_id: string;
  environment: string | null;
  change_set_id: string | null;
  status: string;
  healthy: boolean | null;
  stable: boolean;
  manifests: string[];
  manifest_digest: string;
  cluster_context: string | null;
  namespace: string | null;
  created_at: string | null;
  completed_at: string | null;
};

type Timeline = { deployments: TimelineDeployment[]; count: number; limit: number };

type Diff = {
  left: TimelineDeployment;
  right: TimelineDeployment;
  manifests_added: string[];
  manifests_removed: string[];
  manifests_unchanged: string[];
  identical_manifests: boolean;
  cluster_context_changed: boolean;
  namespace_changed: boolean;
  health_changed: boolean;
};

/** The words for each health state. Exported so the diff and the timeline cannot disagree. */
export function healthWord(healthy: boolean | null): string {
  if (healthy === null) return "not verified";
  return healthy ? "converged" : "did not converge";
}

export function ReleaseTimeline({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [left, setLeft] = useState<string | null>(null);
  const [right, setRight] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);

  const timeline = useQuery<Timeline>({
    queryKey: queryKeys.releases.timeline(projectId),
    queryFn: () => api.get<Timeline>(`/projects/${projectId}/releases/timeline`),
  });

  const diff = useQuery<Diff>({
    queryKey: queryKeys.releases.diff(projectId, left ?? "", right ?? ""),
    queryFn: () =>
      api.get<Diff>(`/projects/${projectId}/releases/diff?left_id=${left}&right_id=${right}`),
    enabled: Boolean(left && right && left !== right),
  });

  const rollback = useMutation<
    { outcome: string },
    Error,
    { environment_id: string; deployment_id: string }
  >({
    mutationFn: (body) =>
      api.post<{ outcome: string }>(`/projects/${projectId}/releases/rollback`, body),
    onSuccess: (accepted) => {
      setOutcome(accepted.outcome);
      void queryClient.invalidateQueries({ queryKey: queryKeys.releases.all });
    },
  });

  const promote = useMutation<{ outcome: string }, Error, { source_environment_id: string }>({
    mutationFn: (body) =>
      api.post<{ outcome: string }>(`/projects/${projectId}/releases/promote`, body),
    onSuccess: (accepted) => {
      setOutcome(accepted.outcome);
      void queryClient.invalidateQueries({ queryKey: queryKeys.releases.all });
    },
  });

  if (timeline.isLoading) {
    return (
      <section
        aria-label="Releases"
        data-testid="release-timeline"
        className="rounded-lg border border-border p-4"
      >
        <p data-testid="timeline-loading" className="text-sm text-muted-foreground">
          Reading this project&apos;s deployment history…
        </p>
      </section>
    );
  }

  if (timeline.isError) {
    return (
      <section
        aria-label="Releases"
        data-testid="release-timeline"
        className="rounded-lg border border-destructive/40 bg-destructive/10 p-4"
      >
        <p data-testid="timeline-error" role="alert" className="text-sm text-destructive">
          The deployment history could not be read: {timeline.error.message}. This is not the same
          as never having deployed.
        </p>
      </section>
    );
  }

  const deployments = timeline.data?.deployments ?? [];

  return (
    <section aria-label="Releases" data-testid="release-timeline" className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold tracking-tight">Release timeline</h2>
        <Badge variant="outline">{deployments.length} releases</Badge>
      </div>

      {outcome && (
        <div
          data-testid="timeline-outcome"
          role="status"
          className="rounded-md border border-primary/30 bg-primary/5 p-3 text-sm font-medium text-foreground"
        >
          {outcome === "applying"
            ? "Sent to the agent."
            : outcome === "approval-required"
              ? "Waiting for a human to approve it. Nothing has changed yet."
              : `The governance gate answered: ${outcome}.`}
        </div>
      )}

      {deployments.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
          <p data-testid="timeline-empty">This project has not deployed yet.</p>
        </div>
      ) : (
        <ol data-testid="timeline-markers" className="space-y-4">
          {deployments.map((deployment) => (
            <li
              key={deployment.id}
              data-testid={`timeline-marker-${deployment.id}`}
              className="space-y-3 rounded-lg border border-border bg-card p-4 shadow-sm"
            >
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
                <div className="flex flex-wrap items-center gap-2">
                  <strong
                    className="font-mono text-base"
                    data-testid={`timeline-env-${deployment.id}`}
                  >
                    {deployment.environment ?? "unknown environment"}
                  </strong>
                  <Badge variant="outline" data-testid={`timeline-status-${deployment.id}`}>
                    {deployment.status}
                  </Badge>
                  <Badge
                    variant={
                      deployment.healthy === true
                        ? "success"
                        : deployment.healthy === false
                          ? "destructive"
                          : "outline"
                    }
                    data-testid={`timeline-health-${deployment.id}`}
                  >
                    {healthWord(deployment.healthy)}
                  </Badge>
                </div>

                <div className="flex flex-wrap items-center gap-2 font-mono text-xs text-muted-foreground">
                  <span>{deployment.created_at ?? "no timestamp"}</span>
                  <span
                    className="rounded bg-muted px-2 py-0.5"
                    data-testid={`timeline-manifests-${deployment.id}`}
                  >
                    {deployment.manifests.length} manifest(s)
                  </span>
                </div>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
                <div className="flex flex-wrap items-center gap-3">
                  {deployment.stable ? (
                    <Button
                      type="button"
                      variant="destructive"
                      size="sm"
                      data-testid={`timeline-rollback-${deployment.id}`}
                      disabled={rollback.isPending}
                      onClick={() =>
                        rollback.mutate({
                          environment_id: deployment.environment_id,
                          deployment_id: deployment.id,
                        })
                      }
                    >
                      roll back to this
                    </Button>
                  ) : (
                    <span
                      data-testid={`timeline-unstable-${deployment.id}`}
                      className="rounded bg-muted px-2 py-1 text-xs text-muted-foreground"
                    >
                      not a rollback target — its workloads never converged
                    </span>
                  )}
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    data-testid={`timeline-promote-${deployment.environment_id}`}
                    disabled={promote.isPending || !deployment.stable}
                    onClick={() =>
                      promote.mutate({ source_environment_id: deployment.environment_id })
                    }
                  >
                    promote this environment
                  </Button>
                </div>

                <div className="flex items-center gap-4 text-xs font-medium">
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="radio"
                      name="diff-left"
                      data-testid={`timeline-left-${deployment.id}`}
                      checked={left === deployment.id}
                      onChange={() => setLeft(deployment.id)}
                      className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    />{" "}
                    compare from
                  </label>
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="radio"
                      name="diff-right"
                      data-testid={`timeline-right-${deployment.id}`}
                      checked={right === deployment.id}
                      onChange={() => setRight(deployment.id)}
                      className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    />{" "}
                    compare to
                  </label>
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}

      <Card className="border border-border">
        <CardHeader>
          <CardTitle className="text-base font-semibold">Side by side</CardTitle>
          <CardDescription>
            Compare two deployments to inspect manifest diffs and status shifts.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!left || !right ? (
            <p data-testid="diff-unselected" className="text-sm text-muted-foreground">
              Choose two deployments to compare.
            </p>
          ) : left === right ? (
            <p data-testid="diff-same" className="text-sm text-amber-600 dark:text-amber-400">
              Those are the same deployment.
            </p>
          ) : diff.isLoading ? (
            <p data-testid="diff-loading" className="text-sm text-muted-foreground">
              Comparing…
            </p>
          ) : diff.isError ? (
            <p data-testid="diff-error" role="alert" className="text-sm text-destructive">
              The comparison failed: {diff.error.message}
            </p>
          ) : diff.data ? (
            <div
              data-testid="diff-result"
              className="space-y-3 rounded-lg border border-border bg-muted/20 p-4 text-xs"
            >
              <p data-testid="diff-identical" className="font-semibold text-foreground text-sm">
                {diff.data.identical_manifests
                  ? "Both deployments carried the same manifest set."
                  : "The manifest sets differ."}
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2">
                <div className="rounded-md border border-border bg-card p-3">
                  <span className="font-semibold block mb-1">Additions</span>
                  <ul
                    data-testid="diff-added"
                    className="space-y-1 text-emerald-600 dark:text-emerald-400 font-mono"
                  >
                    {diff.data.manifests_added.length === 0 ? (
                      <li className="text-muted-foreground font-sans">Nothing added.</li>
                    ) : (
                      diff.data.manifests_added.map((path) => <li key={path}>added {path}</li>)
                    )}
                  </ul>
                </div>
                <div className="rounded-md border border-border bg-card p-3">
                  <span className="font-semibold block mb-1">Removals</span>
                  <ul data-testid="diff-removed" className="space-y-1 text-destructive font-mono">
                    {diff.data.manifests_removed.length === 0 ? (
                      <li className="text-muted-foreground font-sans">Nothing removed.</li>
                    ) : (
                      diff.data.manifests_removed.map((path) => <li key={path}>removed {path}</li>)
                    )}
                  </ul>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-3 pt-2">
                <Badge variant="outline" data-testid="diff-health">
                  {healthWord(diff.data.left.healthy)} → {healthWord(diff.data.right.healthy)}
                </Badge>
                {diff.data.cluster_context_changed && (
                  <p data-testid="diff-context" className="font-mono text-muted-foreground">
                    These went to different clusters:{" "}
                    {diff.data.left.cluster_context ?? "unrecorded"} →{" "}
                    {diff.data.right.cluster_context ?? "unrecorded"}
                  </p>
                )}
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </section>
  );
}
