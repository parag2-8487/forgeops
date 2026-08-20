// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { ProjectList } from "@/features/projects/ProjectList";

/** Mirrors `ProjectResponse` in `backend/src/projects/routes.py`. */
interface ProjectResponse {
  id: string;
  name: string;
  path: string;
  repo_url: string | null;
  settings: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

/** Mirrors `ProjectPage`: a keyset page plus the cursor that fetches the next one. */
interface ProjectPage {
  projects: ProjectResponse[];
  next_cursor: string | null;
}

/** Mirrors `ActivityFeedItem`. */
interface ActivityFeedItem {
  id: string;
  action: string;
  timestamp: string;
  details: string;
}

const PAGE_LIMIT = 25;

export default function ProjectsPage() {
  const projects = useQuery({
    queryKey: queryKeys.projects.list(PAGE_LIMIT),
    queryFn: () => api.get<ProjectPage>(`/projects?limit=${PAGE_LIMIT}`),
    retry: false,
  });

  // Selection drives the activity feed. Defaults to nothing selected rather than to the first
  // project, so the feed below is never showing one project's history under another's heading
  // during a refetch.
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const activity = useQuery({
    queryKey: queryKeys.projects.activity(selectedId ?? ""),
    queryFn: () => api.get<ActivityFeedItem[]>(`/projects/${selectedId}/activity`),
    enabled: selectedId !== null,
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Projects</h1>
        <p className="mt-1 text-muted-foreground">
          Read from <code>GET /api/v1/projects</code>, which returns the projects stored for your
          tenant.
        </p>
      </div>

      <AsyncState
        isPending={projects.isPending}
        error={projects.error}
        isEmpty={projects.data?.projects.length === 0}
        emptyMessage="No projects are stored for this tenant yet. Creating one is served by POST /api/v1/projects."
        label="projects"
      >
        <ProjectList
          projects={(projects.data?.projects ?? []).map((p) => ({
            id: p.id,
            name: p.name,
            repository: p.repo_url ?? p.path,
            readinessScore: 0,
          }))}
        />
      </AsyncState>

      {projects.data?.projects.length ? (
        <section aria-labelledby="activity-heading" className="space-y-3">
          <h2 id="activity-heading" className="text-lg font-semibold">
            Activity
          </h2>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
            <div className="flex-1">
              <label htmlFor="activity-project" className="block text-sm font-medium">
                Project
              </label>
              <select
                id="activity-project"
                value={selectedId ?? ""}
                onChange={(e) => setSelectedId(e.target.value || null)}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="">Select a project…</option>
                {projects.data.projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {selectedId === null ? (
            <p role="status" className="text-sm text-muted-foreground">
              Select a project to read its activity.
            </p>
          ) : (
            <AsyncState
              isPending={activity.isPending}
              error={activity.error}
              isEmpty={activity.data?.length === 0}
              emptyMessage="No governance events have been recorded against this project yet."
              label="activity"
            >
              <ul className="divide-y divide-border rounded-lg border border-border bg-background">
                {activity.data?.map((item) => (
                  <li key={item.id} className="p-4 text-sm">
                    <div className="flex items-baseline justify-between gap-4">
                      <span className="font-medium">{item.action}</span>
                      <time className="font-mono text-xs text-muted-foreground">
                        {item.timestamp}
                      </time>
                    </div>
                    <p className="mt-1 text-muted-foreground">{item.details}</p>
                  </li>
                ))}
              </ul>
            </AsyncState>
          )}
        </section>
      ) : null}

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">What this screen reads.</p>
        <p className="mt-2">
          Both panels are stored data. Until this pass the handlers behind them used no database at
          all: <code>POST</code> echoed its request body and returned a fresh id without inserting a
          row, <code>GET /projects/{"{id}"}</code> returned a fixed record named{" "}
          <code>Sample Project</code> for <em>any</em> id including ones that had never existed, and
          the activity feed was a single hardcoded entry. There was also no list endpoint, which is
          why this screen used to be a lookup box for a project id.
        </p>
        <p className="mt-2">
          The activity feed now reads the append-only <code>audit_events</code> table, so it cannot
          disagree with the audit viewer about what happened, and an empty feed means nothing has
          been recorded rather than that the screen is unfinished. Creating and editing projects is
          served by the API but not surfaced here.
        </p>
      </aside>
    </div>
  );
}
