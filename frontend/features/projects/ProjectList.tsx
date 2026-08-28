// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { indexSummary, type ProjectResponse } from "./types";

/**
 * One row per project: its tags, its favourite star, its real index state, and a link to its detail.
 *
 * WHAT THIS REPLACES
 * The old component took `{ id, name, repository, readinessScore }` and the page filled
 * `readinessScore` with the literal `0` for every project. So every project displayed a zero score,
 * regardless of the score the backend would have computed, and nothing on screen distinguished "we
 * scored it and it scored zero" from "nobody has ever scanned this". A number that is not the number
 * is worse than no number, because it looks like information.
 *
 * There is no score here now. `indexed_file_count` is a fact the list endpoint can produce cheaply,
 * and it answers the question the score was standing in for — "is there anything to score yet?".
 * The score itself is on the detail page, where exactly one `ReadinessEngine` evaluation runs. See
 * `hydrate_projects` in the backend for why the list does not run twenty-five of them.
 *
 * It also stopped being a master/detail pane. The old version kept a `selectedId` and rendered
 * "Viewing details for X" beside the list — a detail pane that showed the name it already had. Real
 * detail is a route now, so it is linkable, refreshable and shareable.
 */
export function ProjectList({
  projects,
  onArchive,
  onDelete,
}: {
  projects: ProjectResponse[];
  /** Opens the archive confirmation. Held by the parent because it owns the dialog state. */
  onArchive?: (project: ProjectResponse) => void;
  onDelete?: (project: ProjectResponse) => void;
}) {
  const queryClient = useQueryClient();

  const favourite = useMutation({
    // PUT to star, DELETE to unstar. Both are idempotent server-side, so a double click is not an
    // error — which matters for a control people click quickly.
    mutationFn: ({ id, next }: { id: string; next: boolean }) =>
      next
        ? api.put<ProjectResponse>(`/projects/${id}/favourite`)
        : api.delete<ProjectResponse>(`/projects/${id}/favourite`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projects.all }),
  });

  return (
    <ul className="space-y-3" data-testid="project-list">
      {projects.map((project) => (
        <li
          key={project.id}
          className="rounded-lg border border-border bg-background p-4"
          data-testid={`project-${project.id}`}
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h3 className="text-sm font-semibold">
                <Link
                  href={`/projects/${project.id}`}
                  className="underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {project.name}
                </Link>
              </h3>
              <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground">
                {project.repo_url ?? project.path}
              </p>
            </div>

            <div className="flex items-center gap-2">
              <button
                type="button"
                // `aria-pressed` rather than two labels: this is one toggle in two states, and a
                // screen reader should hear the state rather than infer it from a changing name.
                aria-pressed={project.favourite}
                aria-label={`${project.favourite ? "Remove" : "Add"} ${project.name} ${
                  project.favourite ? "from" : "to"
                } your favourites`}
                onClick={() => favourite.mutate({ id: project.id, next: !project.favourite })}
                disabled={favourite.isPending}
                data-testid={`favourite-${project.id}`}
                className="rounded-md border border-border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
              >
                {project.favourite ? "★ Favourite" : "☆ Favourite"}
              </button>

              {onArchive ? (
                <button
                  type="button"
                  onClick={() => onArchive(project)}
                  data-testid={`archive-${project.id}`}
                  className="rounded-md border border-border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {project.archived_at ? "Restore" : "Archive"}
                </button>
              ) : null}

              {onDelete ? (
                <button
                  type="button"
                  onClick={() => onDelete(project)}
                  data-testid={`delete-${project.id}`}
                  className="rounded-md border border-destructive/50 px-2 py-1 text-xs text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Delete
                </button>
              ) : null}
            </div>
          </div>

          <dl className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-xs">
            <div className="flex items-center gap-1.5">
              <dt className="font-medium">Index</dt>
              <dd
                data-testid={`index-${project.id}`}
                className={project.indexed_file_count === 0 ? "text-muted-foreground" : undefined}
              >
                {indexSummary(project.indexed_file_count)}
              </dd>
            </div>

            <div className="flex items-center gap-1.5">
              <dt className="font-medium">Tags</dt>
              <dd data-testid={`tags-${project.id}`}>
                {project.tags.length === 0 ? (
                  <span className="text-muted-foreground">none</span>
                ) : (
                  <span className="flex flex-wrap gap-1">
                    {project.tags.map((tag) => (
                      <span key={tag} className="rounded bg-muted px-1.5 py-0.5">
                        {tag}
                      </span>
                    ))}
                  </span>
                )}
              </dd>
            </div>

            {project.archived_at ? (
              <div className="flex items-center gap-1.5">
                <dt className="font-medium">Archived</dt>
                <dd>
                  <time dateTime={project.archived_at}>{project.archived_at}</time>
                </dd>
              </div>
            ) : null}
          </dl>
        </li>
      ))}
    </ul>
  );
}
