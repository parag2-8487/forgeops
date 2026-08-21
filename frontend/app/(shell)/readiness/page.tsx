// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { ProjectPicker } from "@/components/ui/project-picker";
import { ReadinessRadarChart } from "@/features/readiness/RadarChart";

/** Mirrors `ReadinessReportResponse` in `backend/src/projects/routes.py`. */
interface ReadinessReport {
  project_id: string;
  score: number;
  level: string;
  summary_report: string;
  recommendations: string[];
  /**
   * The five fields of `ReadinessBreakdown`, which the engine has always computed and the response
   * model used to drop. That omission is why this screen previously rendered a one-bar chart
   * labelled "Overall": the per-category data the radar chart was built for was not on the wire.
   */
  categories: Record<string, number>;
}

/** Turn `documentation_score` into `Documentation` for display, without inventing categories. */
function categoryLabel(key: string): string {
  return key
    .replace(/_score$/, "")
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export default function ReadinessPage() {
  const [projectId, setProjectId] = useState("");

  const readiness = useQuery({
    queryKey: queryKeys.projects.readiness(projectId),
    queryFn: () => api.get<ReadinessReport>(`/projects/${projectId}/readiness`),
    // Not fired without a project. It used to default to an invented id that is never created, so
    // this screen opened on a 403 every time -- a correct response (§4.2 makes "may not read" and
    // "does not exist" identical) to a request that should never have been made.
    enabled: projectId !== "",
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Deployment readiness</h1>
        <p className="mt-1 text-muted-foreground">
          Scored by the backend&apos;s <code>ReadinessEngine</code>, read from{" "}
          <code>GET /api/v1/projects/{"{id}"}/readiness</code>.
        </p>
      </div>

      <ProjectPicker value={projectId} onChange={setProjectId} id="readiness-project" />

      <AsyncState
        isPending={readiness.isPending}
        error={readiness.error}
        isEmpty={!readiness.data}
        label="readiness report"
      >
        {readiness.data ? (
          <div className="space-y-6">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div className="rounded-lg border border-border bg-background p-4">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Score</p>
                <p className="mt-1 text-3xl font-bold" data-testid="readiness-score">
                  {readiness.data.score}
                  <span className="text-base font-normal text-muted-foreground">/100</span>
                </p>
              </div>
              <div className="rounded-lg border border-border bg-background p-4">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Level</p>
                <p className="mt-1 text-lg font-semibold">{readiness.data.level}</p>
              </div>
            </div>

            {/*
              The radar chart, finally holding the data it was built for.

              `ReadinessEngine` computes a five-category breakdown — documentation, test coverage,
              CI config, security policy, containerisation — and `ReadinessReportResponse` used to
              expose only the total, so this rendered a single bar labelled "Overall". The
              categories are now on the wire, mapped straight from the engine's own field names, so
              there is no risk of rendering a category the engine does not compute.
            */}
            <ReadinessRadarChart
              scores={Object.entries(readiness.data.categories).map(([key, score]) => ({
                category: categoryLabel(key),
                score,
              }))}
            />

            <div className="rounded-lg border border-border bg-background p-4">
              <h2 className="text-sm font-semibold">Summary</h2>
              <p className="mt-2 text-sm text-muted-foreground">{readiness.data.summary_report}</p>
            </div>

            {readiness.data.recommendations.length > 0 ? (
              <div className="rounded-lg border border-border bg-background p-4">
                <h2 className="text-sm font-semibold">Recommendations</h2>
                <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                  {readiness.data.recommendations.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        ) : null}
      </AsyncState>

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">What is real here, and what is not.</p>
        <p className="mt-2">
          The score, level, summary, recommendations and the five-category breakdown are all
          computed by <code>ReadinessEngine</code> — real arithmetic, not stored numbers. The
          project must exist: this endpoint used to score any id at all, so it would return a
          readiness figure for a project that had never been created.
        </p>
        <p className="mt-2">
          <strong>The remaining limit, stated plainly.</strong> What the engine scores is derived
          from the project&apos;s <em>stored</em> settings and repository reference, not from a walk
          of its working tree. So the figure is a real evaluation of real stored data, and it is not
          yet an analysis of your source. Wiring repository contents into the engine is analysis
          work, and the summary text says which of the two it is rather than leaving it to be
          assumed.
        </p>
      </aside>
    </div>
  );
}
