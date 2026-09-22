"use client";

/**
 * Progressive rollout visualisation. Phase 2 §2.7a.
 *
 * WHAT THIS PANEL MUST NOT DO, and it is the whole reason it is written the way it is: show a plausible
 * canary weight when it cannot read one. A weight is a number an operator ACTS on — they promote, they
 * abort, they wait — and a wrong one sends traffic somewhere they did not intend. So:
 *
 *   - `canary_weight`, `current_step` and `total_steps` arrive as -1 for NOT REPORTED, and this component
 *     renders those in words. Zero and "unknown" are different facts: zero means no traffic has been
 *     shifted, and showing 0% for an unreadable field would say the canary had not started when it might
 *     be at 80%.
 *   - AN EMPTY ANALYSIS LIST IS STATED AS "nothing is gating this rollout" rather than rendered as an empty
 *     table, which reads as "all checks passed".
 *   - `Inconclusive` is carried through as its own verdict, never folded into pass or fail. An idle service
 *     makes an error-rate query 0/0 = NaN, which Argo Rollouts reports as Inconclusive; calling that a pass
 *     would promote a release nothing measured.
 *
 * The freshness rule the Docker and Kubernetes dashboards use applies here too: `observed_at` is rendered,
 * and a stale read says so.
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import { queryKeys } from "@/lib/api/query-keys";

/** -1 from the agent means the cluster did not report this field. */
const NOT_REPORTED = -1;

export interface AnalysisMetricSummary {
  name: string;
  phase: string;
  successful: number;
  failed: number;
  inconclusive: number;
  error: number;
  latest_value: string;
}

export interface AnalysisRunSummary {
  name: string;
  phase: string;
  metrics: AnalysisMetricSummary[];
  started_at: string;
}

export interface RolloutDetail {
  rollout: string;
  namespace: string;
  strategy: string;
  phase: string;
  message: string;
  canary_weight: number;
  current_step: number;
  total_steps: number;
  replicas: number;
  updated_replicas: number;
  ready_replicas: number;
  available_replicas: number;
  stable_revision: string;
  canary_revision: string;
  analysis_runs: AnalysisRunSummary[];
  observed_at: string;
}

interface Props {
  projectId: string;
  namespace: string;
  rollout: string;
}

/** How old a read may be before the panel says so. */
const STALE_AFTER_MS = 60_000;

function describeWeight(detail: RolloutDetail): string {
  if (detail.canary_weight === NOT_REPORTED) {
    // THE LOAD-BEARING SENTENCE. An operator reading this knows not to act on a number.
    return "not reported by the cluster";
  }
  return `${detail.canary_weight}% of traffic on the new version`;
}

function describeStep(detail: RolloutDetail): string {
  if (detail.current_step === NOT_REPORTED || detail.total_steps === NOT_REPORTED) {
    return "the rollout's position in its plan was not reported";
  }
  if (detail.current_step >= detail.total_steps) {
    return `all ${detail.total_steps} step(s) complete`;
  }
  // 1-based for a human. Rollouts' own index is 0-based, and a panel showing "step 0 of 3" reads as
  // "nothing has happened" when the first step is in progress.
  return `step ${detail.current_step + 1} of ${detail.total_steps}`;
}

function describePhase(detail: RolloutDetail): string {
  if (!detail.phase) {
    return "the controller reported no phase for this rollout";
  }
  if (detail.message) {
    // The controller's message is the most useful field on the report and the one most likely to be
    // dropped as noise: "Degraded" alone sends an operator to read logs.
    return `${detail.phase} — ${detail.message}`;
  }
  return detail.phase;
}

function verdictOf(run: AnalysisRunSummary): string {
  switch (run.phase) {
    case "Successful":
      return "passed";
    case "Failed":
      return "FAILED — the rollout is aborted and traffic returns to the stable version";
    case "Error":
      return "ERRORED — the metric provider could not be queried, so nothing was measured";
    case "Inconclusive":
      // NOT a pass and NOT a failure. An idle service produces this, and promoting on it would promote a
      // release nothing measured.
      return "inconclusive — nothing was measured, so this gate has not been satisfied";
    case "Running":
      return "running";
    default:
      return run.phase || "no phase reported";
  }
}

export function RolloutPanel({ projectId, namespace, rollout }: Props) {
  const detail = useQuery<RolloutDetail>({
    queryKey: [...queryKeys.hostops.all, "rollout", projectId, namespace, rollout],
    queryFn: () =>
      api.get<RolloutDetail>(
        `/projects/${projectId}/kubernetes/namespaces/${namespace}/rollouts/${rollout}`,
      ),
    refetchInterval: 10_000,
  });

  if (detail.isLoading) {
    // LOADING IS NOT ABSENT. An empty panel here would read as "this rollout has no progress".
    return (
      <section aria-label="Progressive rollout" data-testid="rollout-panel">
        <p data-testid="rollout-loading">Asking the agent about {rollout}…</p>
      </section>
    );
  }

  if (detail.isError || !detail.data) {
    return (
      <section aria-label="Progressive rollout" data-testid="rollout-panel">
        <p data-testid="rollout-error">
          The agent did not report on {rollout}. This is not the same as the rollout being healthy
          or absent — nothing was read, so nothing here should be acted on.
        </p>
      </section>
    );
  }

  const data = detail.data;
  const observedAt = Date.parse(data.observed_at);
  const isStale = Number.isFinite(observedAt) && Date.now() - observedAt > STALE_AFTER_MS;

  return (
    <section aria-label="Progressive rollout" data-testid="rollout-panel">
      <h3>
        {data.rollout}{" "}
        <span data-testid="rollout-strategy">{data.strategy || "no strategy reported"}</span>
      </h3>

      <p data-testid="rollout-phase">{describePhase(data)}</p>

      {isStale ? (
        <p data-testid="rollout-stale">
          This reading is more than a minute old ({data.observed_at}). The rollout may have moved
          since.
        </p>
      ) : (
        <p data-testid="rollout-observed">Read at {data.observed_at}.</p>
      )}

      <dl>
        <dt>Canary weight</dt>
        <dd data-testid="rollout-weight">{describeWeight(data)}</dd>

        <dt>Progress</dt>
        <dd data-testid="rollout-step">{describeStep(data)}</dd>

        <dt>Replicas</dt>
        {/* The tallies are what is RUNNING; the weight is what the mesh was told. They answer different
            questions and a panel showing only one hides a rollout whose pods never became ready. */}
        <dd data-testid="rollout-replicas">
          {data.ready_replicas} of {data.replicas} ready, {data.updated_replicas} updated,{" "}
          {data.available_replicas} available
        </dd>

        <dt>Revisions</dt>
        <dd data-testid="rollout-revisions">
          stable {data.stable_revision || "not reported"}, canary{" "}
          {data.canary_revision || "not reported"}
        </dd>
      </dl>

      <h4>Analysis</h4>
      {data.analysis_runs.length === 0 ? (
        <p data-testid="rollout-no-analysis">
          Nothing is gating this rollout. No analysis run exists for it, so each step advances on a
          timer rather than on a measurement — that is not the same as the checks having passed.
        </p>
      ) : (
        <ul data-testid="rollout-analysis">
          {data.analysis_runs.map((run) => (
            <li key={run.name} data-testid={`rollout-run-${run.name}`}>
              <strong>{run.name}</strong>: {verdictOf(run)}
              <ul>
                {run.metrics.map((metric) => (
                  <li key={metric.name} data-testid={`rollout-metric-${metric.name}`}>
                    {metric.name}: {metric.phase}
                    {metric.latest_value
                      ? ` (latest ${metric.latest_value})`
                      : " (no measurement recorded)"}
                    {metric.inconclusive > 0
                      ? ` — ${metric.inconclusive} inconclusive measurement(s), which are neither passes nor failures`
                      : ""}
                    {metric.failed > 0 ? ` — ${metric.failed} failed` : ""}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
