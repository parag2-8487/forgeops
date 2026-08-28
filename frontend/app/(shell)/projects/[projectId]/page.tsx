// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";
import { CodebaseIndexPanel } from "@/features/codebase/CodebaseIndexPanel";
import { ChangeHistoryTimeline } from "@/features/approvals/ChangeHistoryTimeline";
import { SecretVault, type SecretRefUI } from "@/features/vault/SecretVault";
import {
  categoryLabel,
  type ActivityFeedItem,
  type ProjectResponse,
  type ReadinessReport,
} from "@/features/projects/types";

/**
 * Everything about one project — phases.md §1.2 "Frontend: Project detail page".
 *
 * `GET /api/v1/projects/{id}` existed, was tested, and had no caller: the projects screen listed rows
 * and then rendered a pane saying "Viewing details for X", which is the name it already had. So the
 * one endpoint that answers "what is the state of this project" was unreachable, and the facts a user
 * needs before doing anything — has it been scanned, does it have a policy bundle, is an agent paired,
 * what has happened to it — were spread across five screens that each needed a project id typed in.
 *
 * This is a ROUTE rather than a pane, so it is linkable and refreshable, and every panel on it is
 * scoped by the id in the path rather than by a picker.
 */
export default function ProjectDetailPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params.projectId;

  const project = useQuery({
    queryKey: queryKeys.projects.detail(projectId),
    queryFn: () => api.get<ProjectResponse>(`/projects/${projectId}`),
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <p className="text-xs text-muted-foreground">
          <Link
            href="/projects"
            className="underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            ← All projects
          </Link>
        </p>
        <h1 className="mt-1 text-2xl font-bold tracking-tight">
          {project.data?.name ?? "Project"}
        </h1>
        <p className="mt-1 font-mono text-xs text-muted-foreground">{projectId}</p>
      </div>

      <AsyncState
        isPending={project.isPending}
        error={project.error}
        isEmpty={!project.data}
        label="project"
      >
        {project.data ? (
          <div className="space-y-8">
            <ProjectFacts project={project.data} />

            <TagEditor project={project.data} />

            <section aria-labelledby="index-heading" className="space-y-3">
              <h2 id="index-heading" className="text-lg font-semibold">
                Codebase index
              </h2>
              <CodebaseIndexPanel projectId={projectId} projectPath={project.data.path} />
            </section>

            <section aria-labelledby="readiness-heading" className="space-y-3">
              <h2 id="readiness-heading" className="text-lg font-semibold">
                Readiness
              </h2>
              <ReadinessSummary projectId={projectId} />
            </section>

            <section aria-labelledby="history-heading" className="space-y-3">
              <h2 id="history-heading" className="text-lg font-semibold">
                Change history
              </h2>
              <p className="text-sm text-muted-foreground">
                Every change set submitted for this project, newest first, with what each status
                means. Read from <code>GET /api/v1/approvals?project_id=…</code>.
              </p>
              <ChangeHistoryTimeline projectId={projectId} />
            </section>

            <section aria-labelledby="secrets-heading" className="space-y-3">
              <h2 id="secrets-heading" className="text-lg font-semibold">
                Secret references
              </h2>
              <ProjectSecrets projectId={projectId} />
            </section>

            <section aria-labelledby="activity-heading" className="space-y-3">
              <h2 id="activity-heading" className="text-lg font-semibold">
                Activity
              </h2>
              <ProjectActivity projectId={projectId} />
            </section>
          </div>
        ) : null}
      </AsyncState>
    </div>
  );
}

function ProjectFacts({ project }: { project: ProjectResponse }) {
  return (
    <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <Fact label="Working-tree path" value={project.path} mono />
      <Fact label="Repository" value={project.repo_url ?? "none recorded"} mono />
      <Fact label="Created" value={project.created_at ?? "unknown"} />
      <Fact
        label="State"
        value={project.archived_at ? `archived ${project.archived_at}` : "active"}
      />
    </dl>
  );
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="rounded-lg border border-border bg-background p-4">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className={mono ? "mt-1 break-all font-mono text-xs" : "mt-1 text-sm"}>{value}</dd>
    </div>
  );
}

/**
 * Add and remove tags — PRD FR-02's write half.
 *
 * Tags are lower-cased server-side, and the input says so rather than letting someone add `Prod` and
 * then wonder why the `prod` filter does not include it.
 */
function TagEditor({ project }: { project: ProjectResponse }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
  };

  const add = useMutation({
    mutationFn: (tag: string) => api.put<ProjectResponse>(`/projects/${project.id}/tags`, { tag }),
    onSuccess: () => {
      setDraft("");
      invalidate();
    },
  });

  const remove = useMutation({
    mutationFn: (tag: string) =>
      api.delete<ProjectResponse>(`/projects/${project.id}/tags/${encodeURIComponent(tag)}`),
    onSuccess: invalidate,
  });

  return (
    <section aria-labelledby="tags-heading" className="space-y-3">
      <h2 id="tags-heading" className="text-lg font-semibold">
        Tags
      </h2>
      <ul className="flex flex-wrap gap-2">
        {project.tags.length === 0 ? (
          <li className="text-sm text-muted-foreground">No tags yet.</li>
        ) : (
          project.tags.map((tag) => (
            <li key={tag}>
              <span className="inline-flex items-center gap-2 rounded-full border border-border px-3 py-1 text-xs">
                {tag}
                <button
                  type="button"
                  aria-label={`Remove the tag ${tag}`}
                  data-testid={`remove-tag-${tag}`}
                  onClick={() => remove.mutate(tag)}
                  disabled={remove.isPending}
                  className="text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                >
                  ×
                </button>
              </span>
            </li>
          ))
        )}
      </ul>

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          const tag = draft.trim();
          if (tag !== "") add.mutate(tag);
        }}
      >
        <div className="min-w-[12rem]">
          <label htmlFor="new-tag" className="block text-sm font-medium">
            Add a tag
          </label>
          <input
            id="new-tag"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            maxLength={64}
            aria-describedby="new-tag-help"
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <p id="new-tag-help" className="mt-1 text-xs text-muted-foreground">
            Stored lower-cased, so <code>Prod</code> and <code>prod</code> are one tag rather than
            two that split the filter.
          </p>
        </div>
        <button
          type="submit"
          disabled={draft.trim() === "" || add.isPending}
          className="rounded-md border border-border px-3 py-2 text-sm font-medium disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Add
        </button>
      </form>

      <GovernanceRefusal error={add.error ?? remove.error} action="change this project's tags" />
    </section>
  );
}

/**
 * The score, with a link to the full breakdown rather than a second copy of it.
 *
 * One `ReadinessEngine` evaluation per view of one project. That is why the list screen reports an
 * index file count instead of a score: this is the query that costs an index walk, and running it per
 * row of a list would run it twenty-five times.
 */
function ReadinessSummary({ projectId }: { projectId: string }) {
  const readiness = useQuery({
    queryKey: queryKeys.projects.readiness(projectId),
    queryFn: () => api.get<ReadinessReport>(`/projects/${projectId}/readiness`),
    retry: false,
  });

  return (
    <AsyncState
      isPending={readiness.isPending}
      error={readiness.error}
      isEmpty={!readiness.data}
      label="readiness"
    >
      {readiness.data ? (
        <div className="rounded-lg border border-border bg-background p-4">
          {readiness.data.indexed ? (
            <>
              <p className="text-sm">
                <span className="text-2xl font-bold" data-testid="detail-readiness-score">
                  {readiness.data.score}
                </span>
                <span className="text-muted-foreground">/100 — {readiness.data.level}</span>
              </p>
              <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3">
                {Object.entries(readiness.data.categories).map(([key, score]) => (
                  <div key={key} className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">{categoryLabel(key)}</dt>
                    <dd className="font-medium">{score}</dd>
                  </div>
                ))}
              </dl>
            </>
          ) : (
            <p className="text-sm text-muted-foreground" data-testid="detail-readiness-unscanned">
              <strong>Not scanned.</strong> Readiness is measured from the codebase index, and this
              project has none, so there is no score — not a score of zero. The scan command is in
              the index panel above.
            </p>
          )}
          <p className="mt-3 text-xs">
            <Link
              href="/readiness"
              className="underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Open the full breakdown, with each check and why it matters
            </Link>
          </p>
        </div>
      ) : null}
    </AsyncState>
  );
}

function ProjectSecrets({ projectId }: { projectId: string }) {
  const secrets = useQuery({
    queryKey: queryKeys.secrets.list(projectId),
    queryFn: () => api.get<SecretRefUI[]>(`/secrets?project_id=${projectId}`),
    retry: false,
  });

  return (
    <AsyncState
      isPending={secrets.isPending}
      error={secrets.error}
      isEmpty={secrets.data?.length === 0}
      emptyMessage="No secret references are registered for this project. The Vault screen adds them."
      label="secret references"
    >
      <SecretVault secrets={secrets.data ?? []} projectId={projectId} readOnly />
    </AsyncState>
  );
}

function ProjectActivity({ projectId }: { projectId: string }) {
  const activity = useQuery({
    queryKey: queryKeys.projects.activity(projectId),
    queryFn: () => api.get<ActivityFeedItem[]>(`/projects/${projectId}/activity`),
    retry: false,
  });

  return (
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
              <time className="font-mono text-xs text-muted-foreground" dateTime={item.timestamp}>
                {item.timestamp}
              </time>
            </div>
            <p className="mt-1 text-muted-foreground">{item.details}</p>
          </li>
        ))}
      </ul>
    </AsyncState>
  );
}
