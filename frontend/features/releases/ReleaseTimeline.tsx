"use client";

/**
 * §2.3's release timeline and side-by-side comparison.
 *
 * `healthy` is three states on this screen, as everywhere: `null` is "nothing verified it" (in flight, or
 * the apply failed before anything could be checked), `false` is "checked and not ready", `true` is
 * converged. A marker that collapsed null and false would show a rollout still in progress as a failure.
 *
 * Rollback is offered only for a STABLE deployment, and the absence of one is rendered as an absence with
 * no button: the server decides what is stable (set on health, not on apply), so this screen never computes
 * "the previous deployment" itself.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, queryKeys } from "@/lib/api";

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
      <section aria-label="Releases" data-testid="release-timeline">
        <p data-testid="timeline-loading">Reading this project&apos;s deployment history…</p>
      </section>
    );
  }

  if (timeline.isError) {
    return (
      <section aria-label="Releases" data-testid="release-timeline">
        <p data-testid="timeline-error" role="alert">
          The deployment history could not be read: {timeline.error.message}. This is not the same
          as never having deployed.
        </p>
      </section>
    );
  }

  const deployments = timeline.data?.deployments ?? [];

  return (
    <section aria-label="Releases" data-testid="release-timeline">
      <h2>Release timeline</h2>

      {outcome && (
        <p data-testid="timeline-outcome" role="status">
          {outcome === "applying"
            ? "Sent to the agent."
            : outcome === "approval-required"
              ? "Waiting for a human to approve it. Nothing has changed yet."
              : `The governance gate answered: ${outcome}.`}
        </p>
      )}

      {deployments.length === 0 ? (
        <p data-testid="timeline-empty">This project has not deployed yet.</p>
      ) : (
        <ol data-testid="timeline-markers">
          {deployments.map((deployment) => (
            <li key={deployment.id} data-testid={`timeline-marker-${deployment.id}`}>
              <span data-testid={`timeline-env-${deployment.id}`}>
                {deployment.environment ?? "unknown environment"}
              </span>{" "}
              <span data-testid={`timeline-status-${deployment.id}`}>{deployment.status}</span>{" "}
              {/* THREE WORDS FOR THREE STATES. */}
              <span data-testid={`timeline-health-${deployment.id}`}>
                {healthWord(deployment.healthy)}
              </span>{" "}
              <span>{deployment.created_at ?? "no timestamp"}</span>{" "}
              <span data-testid={`timeline-manifests-${deployment.id}`}>
                {deployment.manifests.length} manifest(s)
              </span>
              {/* Only a stable deployment can be rolled back to, and the absence is an absence. */}
              {deployment.stable ? (
                <button
                  type="button"
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
                </button>
              ) : (
                <span data-testid={`timeline-unstable-${deployment.id}`}>
                  not a rollback target — its workloads never converged
                </span>
              )}
              <button
                type="button"
                data-testid={`timeline-promote-${deployment.environment_id}`}
                disabled={promote.isPending || !deployment.stable}
                onClick={() => promote.mutate({ source_environment_id: deployment.environment_id })}
              >
                promote this environment
              </button>
              <label>
                <input
                  type="radio"
                  name="diff-left"
                  data-testid={`timeline-left-${deployment.id}`}
                  checked={left === deployment.id}
                  onChange={() => setLeft(deployment.id)}
                />{" "}
                compare from
              </label>
              <label>
                <input
                  type="radio"
                  name="diff-right"
                  data-testid={`timeline-right-${deployment.id}`}
                  checked={right === deployment.id}
                  onChange={() => setRight(deployment.id)}
                />{" "}
                compare to
              </label>
            </li>
          ))}
        </ol>
      )}

      <h3>Side by side</h3>
      {!left || !right ? (
        <p data-testid="diff-unselected">Choose two deployments to compare.</p>
      ) : left === right ? (
        <p data-testid="diff-same">Those are the same deployment.</p>
      ) : diff.isLoading ? (
        <p data-testid="diff-loading">Comparing…</p>
      ) : diff.isError ? (
        <p data-testid="diff-error" role="alert">
          The comparison failed: {diff.error.message}
        </p>
      ) : diff.data ? (
        <div data-testid="diff-result">
          <p data-testid="diff-identical">
            {diff.data.identical_manifests
              ? "Both deployments carried the same manifest set."
              : "The manifest sets differ."}
          </p>
          <ul data-testid="diff-added">
            {diff.data.manifests_added.length === 0 ? (
              <li>Nothing added.</li>
            ) : (
              diff.data.manifests_added.map((path) => <li key={path}>added {path}</li>)
            )}
          </ul>
          <ul data-testid="diff-removed">
            {diff.data.manifests_removed.length === 0 ? (
              <li>Nothing removed.</li>
            ) : (
              diff.data.manifests_removed.map((path) => <li key={path}>removed {path}</li>)
            )}
          </ul>
          <p data-testid="diff-health">
            {healthWord(diff.data.left.healthy)} → {healthWord(diff.data.right.healthy)}
          </p>
          {diff.data.cluster_context_changed && (
            <p data-testid="diff-context">
              These went to different clusters: {diff.data.left.cluster_context ?? "unrecorded"} →{" "}
              {diff.data.right.cluster_context ?? "unrecorded"}
            </p>
          )}
        </div>
      ) : null}
    </section>
  );
}
