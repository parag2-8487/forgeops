"use client";

/**
 * The shared vocabulary every §2.10 monitoring panel renders. Phase 2 §2.10.
 *
 * WHY THIS IS ONE COMPONENT RATHER THAN A CONVENTION FOUR PANELS FOLLOW.
 *
 * The recurring defect in this codebase is a panel showing a plausible number when it cannot reach its
 * source, and the reason it recurs is that avoiding it takes discipline at every render site. Four panels
 * each deciding how to phrase "unreachable" gives four chances for one of them to draw an empty chart
 * instead -- and an empty chart reads as "nothing is happening", which is the exact wrong conclusion when
 * the truth is "we cannot see".
 *
 * So the verdict arrives from the backend as a required field, and `MetricVerdictNotice` is the only thing
 * that turns it into words. A panel that wants to show figures asks `hasNumbers` first. There is deliberately
 * no way to render a value while ignoring the verdict: `useMetricQuery` returns them together.
 *
 * The five states, and why each is a separate sentence rather than a separate number:
 *
 *   unconfigured    no metrics store in this deployment. NORMAL -- monitoring is optional here -- but the
 *                   figures are unavailable, not zero.
 *   unreachable     configured and silent. Nothing on screen describes the present.
 *   never_reported  the store answered and holds nothing. Different from zero: zero is a measurement.
 *   stale           real numbers describing the past. Whatever reports them has most likely stopped.
 *   fresh           measured within the last minute.
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";

export type MetricVerdict = "unconfigured" | "unreachable" | "never_reported" | "stale" | "fresh";

export interface MetricPoint {
  at: number;
  /** null where Prometheus returned NaN -- a ratio with no denominator. Never coerced to 0. */
  value: number | null;
}

export interface MetricSeries {
  labels: Record<string, string>;
  points: MetricPoint[];
}

export interface MetricResult {
  name: string;
  kind: "instant" | "range";
  verdict: MetricVerdict;
  explanation: string;
  has_numbers: boolean;
  newest_sample_age_seconds: number | null;
  describes: string;
  promql: string;
  series: MetricSeries[];
}

/** One short label per verdict, for a heading. The full sentence comes from the backend. */
const VERDICT_HEADING: Record<MetricVerdict, string> = {
  unconfigured: "No metrics store configured",
  unreachable: "Metrics store unreachable",
  never_reported: "Never reported",
  stale: "Stale reading",
  fresh: "Reported just now",
};

const VERDICT_TONE: Record<MetricVerdict, string> = {
  unconfigured: "monitoring-notice monitoring-notice--absent",
  unreachable: "monitoring-notice monitoring-notice--broken",
  never_reported: "monitoring-notice monitoring-notice--absent",
  stale: "monitoring-notice monitoring-notice--stale",
  fresh: "monitoring-notice monitoring-notice--fresh",
};

/**
 * Runs one catalogue query. The only way a panel reaches the metrics store.
 *
 * Note there is no parameter through which PromQL could pass. The name is a catalogue key; the backend
 * refuses anything else and composes the tenant matcher itself.
 */
export function useMetricQuery(
  name: string,
  options: { arguments?: Record<string, string>; window?: string; enabled?: boolean } = {},
) {
  const args = options.arguments ?? {};
  const window = options.window ?? "1h";
  return useQuery<MetricResult>({
    queryKey: ["monitoring", "query", name, args, window],
    enabled: options.enabled ?? true,
    // Long enough not to hammer the store, short enough that the staleness verdict is meaningful: a panel
    // refreshing every five minutes would call a fresh reading stale for most of the gap.
    refetchInterval: 30_000,
    queryFn: async () =>
      api.post<MetricResult>("/monitoring/query", {
        name,
        arguments: args,
        window,
      }),
  });
}

/**
 * The notice above every figure.
 *
 * Rendered for EVERY verdict including `fresh`. Showing it only on failure would mean the absence of a
 * warning carried meaning, and an operator who has learned to read its absence as "fine" gets no signal at
 * all when the component fails to render for an unrelated reason.
 */
export function MetricVerdictNotice({
  result,
  isLoading,
  error,
}: {
  result: MetricResult | undefined;
  isLoading?: boolean;
  error?: unknown;
}) {
  if (isLoading) {
    return (
      <p className="monitoring-notice monitoring-notice--absent" role="status">
        Reading the metrics store. No figure below has been measured yet.
      </p>
    );
  }
  if (error) {
    return (
      <p className="monitoring-notice monitoring-notice--broken" role="alert">
        <strong>This panel could not be loaded.</strong> The request to this application failed, so
        nothing below describes the present. This is a different failure from the metrics store
        being unreachable: the request did not get far enough to find out.
      </p>
    );
  }
  if (!result) {
    return (
      <p className="monitoring-notice monitoring-notice--absent" role="status">
        No response yet.
      </p>
    );
  }
  return (
    <p
      className={VERDICT_TONE[result.verdict]}
      role={result.verdict === "unreachable" ? "alert" : "status"}
    >
      <strong>{VERDICT_HEADING[result.verdict]}.</strong> {result.explanation}
    </p>
  );
}

/**
 * A figure, or the reason there is not one.
 *
 * The signature makes the honest thing the easy thing: you cannot call this with a number alone.
 */
export function MetricFigure({
  result,
  value,
  unit,
  decimals = 2,
}: {
  result: MetricResult | undefined;
  value: number | null | undefined;
  unit?: string;
  decimals?: number;
}) {
  if (!result || !result.has_numbers || value === null || value === undefined) {
    return (
      <span className="monitoring-figure monitoring-figure--absent">
        {/* Not a dash, not a zero, not "0.00". Words, because a dash is read as "nothing" and nothing is
            read as zero. */}
        no figure to show
      </span>
    );
  }
  return (
    <span className="monitoring-figure">
      {value.toFixed(decimals)}
      {unit ? <span className="monitoring-figure__unit"> {unit}</span> : null}
    </span>
  );
}

/** Sums the newest point of every series. Returns null when there is nothing to sum -- never 0. */
export function latestTotal(result: MetricResult | undefined): number | null {
  if (!result || !result.has_numbers) return null;
  let total: number | null = null;
  for (const series of result.series) {
    const last = series.points[series.points.length - 1];
    if (!last || last.value === null) continue;
    total = (total ?? 0) + last.value;
  }
  return total;
}

/** The newest point per series, keyed by a label. Series whose newest point is NaN are omitted, not zeroed. */
export function latestByLabel(
  result: MetricResult | undefined,
  label: string,
): Array<{ key: string; value: number }> {
  if (!result || !result.has_numbers) return [];
  const out: Array<{ key: string; value: number }> = [];
  for (const series of result.series) {
    const last = series.points[series.points.length - 1];
    if (!last || last.value === null) continue;
    out.push({ key: series.labels[label] ?? "(unlabelled)", value: last.value });
  }
  return out.sort((a, b) => b.value - a.value);
}
