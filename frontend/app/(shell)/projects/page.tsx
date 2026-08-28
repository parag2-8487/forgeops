// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";
import { ProjectList } from "@/features/projects/ProjectList";
import { ProjectCreateForm } from "@/features/projects/ProjectCreateForm";
import type { DeletionReport, ProjectPage, ProjectResponse } from "@/features/projects/types";

const PAGE_LIMIT = 25;

/**
 * The multi-project workspace — phases.md §1.2, PRD FR-01 through FR-05.
 *
 * Four things arrive here that were `- [ ]` or absent entirely: a create form (FR-01, P0, no form
 * existed), search, tags and favourites (FR-02, FR-03, both P0), and archive and delete (FR-05, P0,
 * no route on either side). The fifth change is a removal: `readinessScore: 0`.
 *
 * THE FILTERS ARE QUERY PARAMETERS, NOT ARRAY OPERATIONS. Every one of them is applied by
 * `GET /api/v1/projects` in SQL. Filtering the fetched page in the browser would look identical at
 * six projects and be broken at six hundred: the cursor would describe the unfiltered sequence, so
 * page two of a search would drop matches. That is why the filter state is part of the query key —
 * see `queryKeys.projects.filtered`.
 */
export default function ProjectsPage() {
  const queryClient = useQueryClient();

  // `search` is the committed term; `searchDraft` is what is in the box. Submitting is explicit
  // rather than debounced-on-keystroke, so one deliberate search is one request and the operator
  // can see exactly which term produced the list they are reading.
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [favouritesOnly, setFavouritesOnly] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  const [archiving, setArchiving] = useState<ProjectResponse | null>(null);
  const [deleting, setDeleting] = useState<ProjectResponse | null>(null);

  const filters = {
    limit: PAGE_LIMIT,
    search,
    tags: selectedTags,
    favourite: favouritesOnly,
    archived: showArchived,
  };

  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (search !== "") params.set("search", search);
  for (const tag of selectedTags) params.append("tag", tag);
  if (favouritesOnly) params.set("favourite", "true");
  if (showArchived) params.set("archived", "true");

  const projects = useQuery({
    queryKey: queryKeys.projects.filtered(filters),
    queryFn: () => api.get<ProjectPage>(`/projects?${params.toString()}`),
    retry: false,
  });

  // The tenant's whole tag vocabulary, so the filter is a set of chips rather than a free-text box
  // that silently matches nothing when it is misspelled.
  const tags = useQuery({
    queryKey: queryKeys.projects.tags(),
    queryFn: () => api.get<string[]>("/projects/tags"),
    retry: false,
  });

  const filtersActive = search !== "" || selectedTags.length > 0 || favouritesOnly || showArchived;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Projects</h1>
        <p className="mt-1 text-muted-foreground">
          Read from <code>GET /api/v1/projects</code>. Search, tags and favourites are applied by
          the endpoint, so paging a filtered list pages the matches rather than the whole tenant.
        </p>
      </div>

      <ProjectCreateForm />

      <section aria-labelledby="filters-heading" className="space-y-3">
        <h2 id="filters-heading" className="text-lg font-semibold">
          Find a project
        </h2>

        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            setSearch(searchDraft.trim());
          }}
        >
          <div className="min-w-[16rem] flex-1">
            <label htmlFor="project-search" className="block text-sm font-medium">
              Search name or path
            </label>
            <input
              id="project-search"
              type="search"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
          <button
            type="submit"
            className="rounded-md border border-border px-3 py-2 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Search
          </button>
        </form>

        <div className="flex flex-wrap items-center gap-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={favouritesOnly}
              onChange={(event) => setFavouritesOnly(event.target.checked)}
              data-testid="filter-favourites"
              className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            My favourites only
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(event) => setShowArchived(event.target.checked)}
              data-testid="filter-archived"
              className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            Show archived instead of active
          </label>
        </div>

        {tags.data && tags.data.length > 0 ? (
          <fieldset>
            <legend className="text-sm font-medium">Tags</legend>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Selecting more than one narrows: a project must carry every tag chosen.
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {tags.data.map((tag) => {
                const on = selectedTags.includes(tag);
                return (
                  <button
                    key={tag}
                    type="button"
                    aria-pressed={on}
                    data-testid={`tag-filter-${tag}`}
                    onClick={() =>
                      setSelectedTags((current) =>
                        on ? current.filter((t) => t !== tag) : [...current, tag],
                      )
                    }
                    className={
                      on
                        ? "rounded-full border border-primary bg-primary px-3 py-1 text-xs text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        : "rounded-full border border-border px-3 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    }
                  >
                    {tag}
                  </button>
                );
              })}
            </div>
          </fieldset>
        ) : null}
      </section>

      <AsyncState
        isPending={projects.isPending}
        error={projects.error}
        isEmpty={projects.data?.projects.length === 0}
        emptyMessage={
          filtersActive
            ? "No project matches these filters. The filters run server-side, so this is the whole tenant's answer rather than one page of it."
            : "No projects are stored for this tenant yet. The form above creates one."
        }
        label="projects"
      >
        <ProjectList
          projects={projects.data?.projects ?? []}
          onArchive={setArchiving}
          onDelete={setDeleting}
        />
      </AsyncState>

      {projects.data?.next_cursor ? (
        <p className="text-xs text-muted-foreground">
          More projects match than fit on one page. Narrow the filters above to reach them; the
          cursor walks the filtered set, not the whole tenant.
        </p>
      ) : null}

      {archiving ? (
        <ArchiveDialog
          project={archiving}
          onDone={() => {
            setArchiving(null);
            void queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
          }}
          onCancel={() => setArchiving(null)}
        />
      ) : null}

      {deleting ? (
        <DeleteDialog
          project={deleting}
          onDone={() => {
            setDeleting(null);
            void queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
          }}
          onCancel={() => setDeleting(null)}
        />
      ) : null}
    </div>
  );
}

/**
 * Archive or restore, with the reason the audit record requires.
 *
 * The reason is not optional and not defaulted. NFR-14 makes "why" one of the six fields every
 * governance record carries, and the endpoint refuses an empty one — so a prefilled "archived via
 * UI" would satisfy the validator while making the field worthless, which is the failure the
 * requirement exists to prevent.
 */
function ArchiveDialog({
  project,
  onDone,
  onCancel,
}: {
  project: ProjectResponse;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const restoring = project.archived_at !== null;

  const submit = useMutation({
    mutationFn: () =>
      api.post<ProjectResponse>(`/projects/${project.id}/${restoring ? "unarchive" : "archive"}`, {
        reason: reason.trim(),
      }),
    onSuccess: onDone,
  });

  return (
    <section
      aria-labelledby="archive-heading"
      className="space-y-3 rounded-lg border border-border bg-background p-4"
    >
      <h2 id="archive-heading" className="text-sm font-semibold">
        {restoring ? "Restore" : "Archive"} {project.name}
      </h2>
      <p className="text-xs text-muted-foreground">
        {restoring
          ? "Clears the archive date and returns the project to the active list."
          : "Reversible. Nothing is deleted, nothing is unindexed, no device is unpaired — the project simply leaves the active list. Delete is the other operation."}
      </p>
      <div>
        <label htmlFor="archive-reason" className="block text-sm font-medium">
          Reason
        </label>
        <input
          id="archive-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          required
          maxLength={500}
          aria-describedby="archive-reason-help"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <p id="archive-reason-help" className="mt-1 text-xs text-muted-foreground">
          Written to the append-only audit log against your identity. Required, and not prefilled: a
          default would satisfy the validator and tell a future reader nothing.
        </p>
      </div>
      <div className="flex gap-3">
        <button
          type="button"
          onClick={() => submit.mutate()}
          disabled={reason.trim() === "" || submit.isPending}
          data-testid="archive-confirm"
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {submit.isPending ? "Working…" : restoring ? "Restore" : "Archive"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-4 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Cancel
        </button>
      </div>
      <GovernanceRefusal
        error={submit.error}
        action={restoring ? "restore this project" : "archive this project"}
      />
    </section>
  );
}

/**
 * Delete, with the cascade stated before it happens and counted after.
 *
 * The name has to be typed back. That is what the endpoint requires and it is the right requirement:
 * a checkbox is clicked by reflex, and typing the name is the smallest gesture that proves the
 * operator knows which project this is.
 */
function DeleteDialog({
  project,
  onDone,
  onCancel,
}: {
  project: ProjectResponse;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const [confirmName, setConfirmName] = useState("");
  const [report, setReport] = useState<DeletionReport | null>(null);

  const submit = useMutation({
    mutationFn: () =>
      api.deleteWith<DeletionReport>(`/projects/${project.id}`, {
        reason: reason.trim(),
        confirm_name: confirmName,
      }),
    onSuccess: setReport,
  });

  if (report) {
    return (
      <section
        aria-labelledby="deleted-heading"
        className="space-y-3 rounded-lg border border-border bg-background p-4"
      >
        <h2 id="deleted-heading" className="text-sm font-semibold">
          {project.name} was deleted
        </h2>
        <p className="text-xs text-muted-foreground">
          Counted before the row went, so this is what the cascade actually removed rather than an
          estimate.
        </p>
        <dl
          data-testid="deletion-report"
          className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-xs"
        >
          {Object.entries(report.cascaded).map(([table, count]) => (
            <div key={table} className="col-span-2 grid grid-cols-subgrid">
              <dt className="font-mono">{table}</dt>
              <dd>{count}</dd>
            </div>
          ))}
        </dl>
        <p className="text-xs text-muted-foreground">
          <strong>{report.audit_events_retained}</strong> audit record
          {report.audit_events_retained === 1 ? "" : "s"} about this project were{" "}
          <strong>kept</strong>. The audit log is append-only and holds no foreign key to a project,
          so the history outlives the thing it describes — the events happened, and deleting the
          project does not unhappen them.
        </p>
        <button
          type="button"
          onClick={onDone}
          className="rounded-md border border-border px-4 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Close
        </button>
      </section>
    );
  }

  return (
    <section
      aria-labelledby="delete-heading"
      className="space-y-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4"
    >
      <h2 id="delete-heading" className="text-sm font-semibold">
        Delete {project.name}
      </h2>
      <p className="text-xs text-muted-foreground">
        Not reversible. The codebase index, the file contents, the dependency graph, both vector
        tables, every change set and generation run, any paired devices, the secret references, the
        policies and policy bundles scoped to this project, its tags and its favourites all go with
        it. The <strong>audit log does not</strong> — those records survive and keep this
        project&apos;s id. Archive is the reversible option.
      </p>
      <div>
        <label htmlFor="delete-reason" className="block text-sm font-medium">
          Reason
        </label>
        <input
          id="delete-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          required
          maxLength={500}
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
      <div>
        <label htmlFor="delete-confirm" className="block text-sm font-medium">
          Type the project name to confirm
        </label>
        <input
          id="delete-confirm"
          value={confirmName}
          onChange={(event) => setConfirmName(event.target.value)}
          autoComplete="off"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
      <div className="flex gap-3">
        <button
          type="button"
          onClick={() => submit.mutate()}
          // Checked here as well as server-side. The server is the authority; this is so the button
          // does not offer an action it can already tell will be refused.
          disabled={reason.trim() === "" || confirmName !== project.name || submit.isPending}
          data-testid="delete-confirm-button"
          className="rounded-md bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {submit.isPending ? "Deleting…" : "Delete permanently"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-4 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Cancel
        </button>
      </div>
      <GovernanceRefusal error={submit.error} action="delete this project" />
    </section>
  );
}
