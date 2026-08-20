// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { DEFAULT_PROJECT_ID, ProjectIdField } from "@/components/ui/project-id-field";
import { ReadinessRadarChart } from "@/features/readiness/RadarChart";

/** Mirrors `ReadinessReportResponse` in `backend/src/projects/routes.py`. */
interface ReadinessReport {
  project_id: string;
  score: number;
  level: string;
  summary_report: string;
  recommendations: string[];
}

export default function ReadinessPage() {
  const [projectId, setProjectId] = useState(DEFAULT_PROJECT_ID);

  const readiness = useQuery({
    queryKey: queryKeys.projects.readiness(projectId),
    queryFn: () => api.get<ReadinessReport>(`/projects/${projectId}/readiness`),
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

      <ProjectIdField value={projectId} onChange={setProjectId} id="readiness-project-id" />

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
              One bar, holding the one number the API actually returns.

              `ReadinessEngine` computes a five-category breakdown — documentation, test coverage,
              CI config, security policy, containerisation — but `ReadinessReportResponse` does not
              expose it, so the per-category data this chart was built for is not on the wire.
              Rendering five invented bars would be the exact defect this pass exists to remove, so
              it gets the real total and the gap is noted below.
            */}
            <ReadinessRadarChart scores={[{ category: "Overall", score: readiness.data.score }]} />

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
          The score, level, summary and recommendations are computed by <code>ReadinessEngine</code>{" "}
          — real arithmetic, not a stored number. What it scores, however, is a{" "}
          <strong>hardcoded input</strong>: the route passes a fixed
          <code> {'{manifests: ["Dockerfile"], config_files: ["README.md"]}'} </code>
          rather than analysing the project at the id you typed, so the same figure comes back for
          every project. The engine is finished; the wiring from real repository contents into it is
          not.
        </p>
        <p className="mt-2">
          The five-category breakdown the chart above was designed for is computed server-side but
          dropped by the response model, so only the total crosses the wire.
        </p>
      </aside>
    </div>
  );
}
