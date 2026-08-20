// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { DEFAULT_PROJECT_ID, ProjectIdField } from "@/components/ui/project-id-field";
import { ProjectList } from "@/features/projects/ProjectList";

/** Mirrors `ProjectResponse` in `backend/src/projects/routes.py`. */
interface ProjectResponse {
  id: string;
  name: string;
  path: string;
  repo_url: string | null;
  settings: Record<string, unknown>;
}

/** Mirrors `ActivityFeedItem`. */
interface ActivityFeedItem {
  id: string;
  action: string;
  timestamp: string;
  details: string;
}

export default function ProjectsPage() {
  const [projectId, setProjectId] = useState(DEFAULT_PROJECT_ID);

  const project = useQuery({
    queryKey: queryKeys.projects.detail(projectId),
    queryFn: () => api.get<ProjectResponse>(`/projects/${projectId}`),
    retry: false,
  });

  const activity = useQuery({
    queryKey: queryKeys.projects.activity(projectId),
    queryFn: () => api.get<ActivityFeedItem[]>(`/projects/${projectId}/activity`),
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Projects</h1>
        <p className="mt-1 text-muted-foreground">
          Fetched through <code>lib/api</code> from <code>GET /api/v1/projects/{"{id}"}</code>.
        </p>
      </div>

      <ProjectIdField value={projectId} onChange={setProjectId} />

      <AsyncState
        isPending={project.isPending}
        error={project.error}
        isEmpty={!project.data}
        emptyMessage="The backend returned no project for that id."
        label="project"
      >
        <ProjectList
          projects={
            project.data
              ? [
                  {
                    id: project.data.id,
                    name: project.data.name,
                    repository: project.data.repo_url ?? project.data.path,
                    readinessScore: 0,
                  },
                ]
              : []
          }
        />
      </AsyncState>

      <section aria-labelledby="activity-heading" className="space-y-3">
        <h2 id="activity-heading" className="text-lg font-semibold">
          Activity
        </h2>
        <AsyncState
          isPending={activity.isPending}
          error={activity.error}
          isEmpty={activity.data?.length === 0}
          emptyMessage="No activity recorded for this project."
          label="activity"
        >
          <ul className="divide-y divide-border rounded-lg border border-border bg-background">
            {activity.data?.map((item) => (
              <li key={item.id} className="p-4 text-sm">
                <div className="flex items-baseline justify-between gap-4">
                  <span className="font-medium">{item.action}</span>
                  <time className="font-mono text-xs text-muted-foreground">{item.timestamp}</time>
                </div>
                <p className="mt-1 text-muted-foreground">{item.details}</p>
              </li>
            ))}
          </ul>
        </AsyncState>
      </section>

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          Two limits of this screen, stated rather than hidden.
        </p>
        <p className="mt-2">
          <strong>There is no list endpoint.</strong> Phase 1 serves create, get-by-id, activity and
          readiness — so this is a lookup by id, not a browsable list. A list would have to invent
          its contents.
        </p>
        <p className="mt-2">
          <strong>The handler behind it is a fixture.</strong> The HTTP call, the RFC 9457 error
          handling and the render are real, but `get_project` in the backend returns a fixed record
          for any id and does not read a database — so the <em>name</em> you see is the
          backend&apos;s constant, not stored data. The readiness score is shown as 0 here for the
          same reason: the real number comes from the readiness endpoint, on its own screen, where
          it is genuinely computed.
        </p>
      </aside>
    </div>
  );
}
