// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";

/**
 * Choose one of the tenant's real projects.
 *
 * WHAT THIS REPLACES, AND WHY IT WAS A DEFECT RATHER THAN A ROUGH EDGE
 * `ProjectIdField` was a free-text UUID box defaulting to `DEFAULT_PROJECT_ID`, the invented value
 * `00000000-0000-0000-0000-000000000001`. Its own comment explained that it existed because Phase 1
 * served no `GET /api/v1/projects`, so a screen could not enumerate anything.
 *
 * That endpoint exists now, and the placeholder had become actively misleading: no project with that
 * id is ever created, so `/readiness` opened on a **403 every single time**. The response is correct
 * — §4.2 makes "you may not read it" and "it does not exist" indistinguishable, deliberately, so
 * nobody can enumerate ids — but a screen whose default value guarantees that response teaches the
 * operator to distrust the error rather than read it.
 *
 * So the choice is made from real rows. If the tenant has no projects the component says so and
 * points at the screen that creates one, instead of leaving a box that can only fail.
 */

interface ProjectSummary {
  id: string;
  name: string;
}

interface ProjectPage {
  projects: ProjectSummary[];
  next_cursor: string | null;
}

const PAGE_SIZE = 100;

export function ProjectPicker({
  value,
  onChange,
  id = "project-picker",
  label = "Project",
}: {
  value: string;
  onChange: (next: string) => void;
  id?: string;
  label?: string;
}) {
  const projects = useQuery({
    queryKey: queryKeys.projects.list(PAGE_SIZE),
    queryFn: () => api.get<ProjectPage>(`/projects?limit=${PAGE_SIZE}`),
    retry: false,
  });

  // Memoised because the effect below depends on it: a fresh array literal on every render would
  // re-run the effect on every render.
  const rows = useMemo(() => projects.data?.projects ?? [], [projects.data]);

  // Select the first project once the list arrives, so the panel below has something real to show
  // rather than an empty state that looks like a failure. Only when nothing is chosen yet: a
  // reselection on every refetch would fight the operator.
  useEffect(() => {
    if (value === "" && rows.length > 0) onChange(rows[0].id);
  }, [value, rows, onChange]);

  if (projects.isPending) {
    return (
      <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
        Loading projects…
      </p>
    );
  }

  // A failure to LIST is reported here rather than swallowed, because the panel below would
  // otherwise blame the project for an error that belongs to the list request.
  if (projects.error) {
    return (
      <p role="alert" className="text-sm text-destructive">
        The project list could not be read, so no project can be chosen. The panel below has nothing
        to act on until it can.
      </p>
    );
  }

  if (rows.length === 0) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        This tenant has no projects yet. Create one on the{" "}
        <a className="underline" href="/projects">
          Projects
        </a>{" "}
        screen — there is deliberately no id box here, because typing an id that does not exist
        produces a <code>403</code> that looks like a permissions fault.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
      <div className="flex-1">
        <label htmlFor={id} className="block text-sm font-medium">
          {label}
        </label>
        <select
          id={id}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {rows.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name} — {project.id}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
