// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { ProjectPicker } from "@/components/ui/project-picker";
import { ReadinessRadarChart } from "@/features/readiness/RadarChart";
import { ReadinessBreakdown } from "@/features/readiness/ReadinessBreakdown";
import { categoryLabel, type ReadinessReport } from "@/features/projects/types";

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
          Scored by the backend&apos;s <code>ReadinessEngine</code> from the project&apos;s codebase
          index, read from <code>GET /api/v1/projects/{"{id}"}/readiness</code>.
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
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
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
              {/*
                Scanned-or-not, on the face of the panel rather than only in the summary sentence.
                A score of zero from a real evaluation and a score of zero because nothing was
                measured are completely different facts, and the number alone cannot carry the
                difference.
              */}
              <div className="rounded-lg border border-border bg-background p-4">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  Measured from
                </p>
                <p className="mt-1 text-sm font-semibold" data-testid="readiness-provenance">
                  {readiness.data.indexed
                    ? `${readiness.data.evaluated_paths} indexed path${
                        readiness.data.evaluated_paths === 1 ? "" : "s"
                      }`
                    : "Nothing — never scanned"}
                </p>
              </div>
            </div>

            <ReadinessRadarChart
              scores={Object.entries(readiness.data.categories).map(([key, score]) => ({
                category: categoryLabel(key),
                score,
              }))}
            />

            <section aria-labelledby="breakdown-heading" className="space-y-3">
              <h2 id="breakdown-heading" className="text-lg font-semibold">
                Category breakdown
              </h2>
              <p className="text-sm text-muted-foreground">
                Each category expands into the individual checks behind its score, with the indexed
                path that satisfied it and why the check exists.
              </p>
              <ReadinessBreakdown report={readiness.data} />
            </section>

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

      {/*
        THIS PANEL WAS STALE, AND THAT IS WHY IT IS WORTH A NOTE RATHER THAN A QUIET EDIT.

        It read: "What the engine scores is derived from the project's STORED SETTINGS and repository
        reference, not from a walk of its working tree... it is not yet an analysis of your source",
        and it described a FIVE-category breakdown. Both statements had been false since the engine
        moved to index-derived scoring: there are six categories, they are phases.md §1.4's, and they
        are computed from `file_tree` and `file_contents` — the rows an agent scan persists. The
        behaviour changed and the copy did not, which is a worse failure than the copy never having
        existed: a reader who trusts it concludes the score is meaningless and stops looking at it.
      */}
      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">What this score is measured from.</p>
        <p className="mt-2">
          The six categories above are phases.md §1.4&apos;s — Containerization, CI/CD,
          Orchestration, Env Config, Security and IaC — and every one is computed from this
          project&apos;s <strong>codebase index</strong>: the file tree and redacted file contents
          an agent scan persisted through <code>POST /api/v1/analysis/codebase/{"{id}"}/index</code>
          . Each check names the indexed path that satisfied it, so a score is traceable to the
          evidence behind it rather than being a number you have to take on trust.
        </p>
        <p className="mt-2">
          <code>projects.settings</code> participates only as a <em>refinement</em>:{" "}
          <code>ignore_globs</code> removes paths from the evidence, because a path you have
          declared out of scope is not evidence about your deployment. It cannot add points. An
          earlier version of the engine scored the settings themselves — <code>config_files</code>{" "}
          was literally the list of settings keys — so a project earned documentation points for
          having been configured. That is gone.
        </p>
        <p className="mt-2">
          <strong>A project with no indexed files scores zero and says so.</strong> The panel above
          reports what the score was measured from, and an unscanned project reads &ldquo;never
          scanned&rdquo; rather than showing a zero that looks like a measurement. Run a scan — the
          exact command is on the project&apos;s own page.
        </p>
      </aside>
    </div>
  );
}
