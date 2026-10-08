// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CodebaseIndexPanel } from "@/features/codebase/CodebaseIndexPanel";
import { DeploymentDashboard } from "@/features/deployments/DeploymentDashboard";
import { DevToolsPanel } from "@/features/devtools/DevToolsPanel";
import {
  NotificationBell,
  NotificationPreferences,
} from "@/features/notifications/NotificationBell";
import { DockerDashboard } from "@/features/hostops/DockerDashboard";
import { ReleaseTimeline } from "@/features/releases/ReleaseTimeline";
import { KubernetesDashboard } from "@/features/hostops/KubernetesDashboard";
import { EnvironmentManager } from "@/features/environments/EnvironmentManager";
import { ChangeHistoryTimeline } from "@/features/approvals/ChangeHistoryTimeline";
import { SecretVault, type SecretRefUI } from "@/features/vault/SecretVault";
import {
  categoryLabel,
  type ActivityFeedItem,
  type ProjectResponse,
  type ReadinessReport,
} from "@/features/projects/types";
import { CloudDeployModal } from "@/features/integrations/CloudDeployModal";

/**
 * Everything about one project — phases.md §1.2 "Frontend: Project detail page".
 */
export default function ProjectDetailPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params.projectId;
  const [cloudDeployOpen, setCloudDeployOpen] = useState(false);

  const project = useQuery({
    queryKey: queryKeys.projects.detail(projectId),
    queryFn: () => api.get<ProjectResponse>(`/projects/${projectId}`),
    retry: false,
  });

  return (
    <div className="space-y-8 w-full pb-16">
      <div className="border-b border-border pb-5">
        <p className="text-xs text-muted-foreground">
          <Link
            href="/projects"
            className="inline-flex items-center gap-1 font-medium underline underline-offset-4 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            ← All projects
          </Link>
        </p>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-3xl font-bold tracking-tight">{project.data?.name ?? "Project"}</h1>
            <p className="font-mono text-xs text-muted-foreground rounded bg-muted px-2.5 py-1 mt-1 inline-block">
              {projectId}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <Button
              onClick={() => setCloudDeployOpen(true)}
              className="font-medium text-sm shadow-sm"
            >
              Push to GitHub / Deploy to Vercel
            </Button>
          </div>
        </div>
      </div>

      <CloudDeployModal
        projectId={projectId}
        projectName={project.data?.name ?? "project"}
        isOpen={cloudDeployOpen}
        onClose={() => setCloudDeployOpen(false)}
      />

      <AsyncState
        isPending={project.isPending}
        error={project.error}
        isEmpty={!project.data}
        label="project"
      >
        {project.data ? (
          <div className="space-y-10">
            <ProjectFacts project={project.data} />

            <TagEditor project={project.data} />

            <section
              aria-labelledby="index-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <h2 id="index-heading" className="text-lg font-semibold tracking-tight">
                  Codebase index
                </h2>
                <Badge variant="outline">§1.3</Badge>
              </div>
              <CodebaseIndexPanel projectId={projectId} projectPath={project.data.path} />
            </section>

            <section
              aria-labelledby="readiness-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <h2 id="readiness-heading" className="text-lg font-semibold tracking-tight">
                  Readiness
                </h2>
                <Badge variant="outline">§1.4</Badge>
              </div>
              <ReadinessSummary projectId={projectId} />
            </section>

            <section
              aria-labelledby="history-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <div>
                  <h2 id="history-heading" className="text-lg font-semibold tracking-tight">
                    Change history
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Every change set submitted for this project, newest first, with what each status
                    means. Read from <code>GET /api/v1/approvals?project_id=…</code>.
                  </p>
                </div>
                <Badge variant="outline">§1.7</Badge>
              </div>
              <ChangeHistoryTimeline projectId={projectId} />
            </section>

            <section
              aria-labelledby="environments-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <div>
                  <h2 id="environments-heading" className="text-lg font-semibold tracking-tight">
                    Environments
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    §2.1. The deployment targets of this project, in promotion order. Whether a
                    deployment here waits for a human is a property of the environment, and it is
                    stated in words on every row rather than left to a checkbox.
                  </p>
                </div>
                <Badge variant="outline">§2.1</Badge>
              </div>
              <EnvironmentManager projectId={projectId} />
            </section>

            <section
              aria-labelledby="deployments-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <div>
                  <h2 id="deployments-heading" className="text-lg font-semibold tracking-tight">
                    Deployments
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    §2.2. A deployment is a mutation, so it goes through the same governance
                    chokepoint as every other: policy, approval, blast radius, audit, rollback
                    handle.
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Button size="sm" variant="outline" onClick={() => setCloudDeployOpen(true)}>
                    Export to GitHub / Vercel
                  </Button>
                  <Badge variant="outline">§2.2</Badge>
                </div>
              </div>
              <DeploymentDashboard projectId={projectId} />
            </section>

            <section
              aria-labelledby="releases-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <div>
                  <h2 id="releases-heading" className="text-lg font-semibold tracking-tight">
                    Releases
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    2.3. Promotion and rollback are deployments: both travel the chokepoint, and the
                    target environment&apos;s approval requirement governs.
                  </p>
                </div>
                <Badge variant="outline">§2.3</Badge>
              </div>
              <ReleaseTimeline projectId={projectId} />
            </section>

            <section
              aria-labelledby="notifications-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <div>
                  <h2 id="notifications-heading" className="text-lg font-semibold tracking-tight">
                    Notifications
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    2.6. Every notification records what reached each channel. A webhook URL is a
                    credential and is never shown again.
                  </p>
                </div>
                <Badge variant="outline">§2.6</Badge>
              </div>
              <div className="space-y-4">
                <NotificationBell projectId={projectId} />
                <NotificationPreferences projectId={projectId} />
              </div>
            </section>

            <section
              aria-labelledby="devtools-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <div>
                  <h2 id="devtools-heading" className="text-lg font-semibold tracking-tight">
                    Developer tools
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    2.8. Five named kinds, no command field. The agent chooses the argument vector
                    from the workspace&apos;s own manifests.
                  </p>
                </div>
                <Badge variant="outline">§2.8</Badge>
              </div>
              <DevToolsPanel projectId={projectId} />
            </section>

            <section
              aria-labelledby="docker-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <div>
                  <h2 id="docker-heading" className="text-lg font-semibold tracking-tight">
                    Docker
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    2.4. Read from the operator&apos;s own daemon through the agent. Every measured
                    figure distinguishes &ldquo;not measured&rdquo; from zero.
                  </p>
                </div>
                <Badge variant="outline">§2.4</Badge>
              </div>
              <DockerDashboard projectId={projectId} />
            </section>

            <section
              aria-labelledby="kubernetes-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <div>
                  <h2 id="kubernetes-heading" className="text-lg font-semibold tracking-tight">
                    Kubernetes
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    2.9. Scale, restart and roll back are mutations and go through the chokepoint.
                  </p>
                </div>
                <Badge variant="outline">§2.9</Badge>
              </div>
              <KubernetesDashboard projectId={projectId} />
            </section>

            <section
              aria-labelledby="secrets-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <div>
                  <h2 id="secrets-heading" className="text-lg font-semibold tracking-tight">
                    Secret references
                  </h2>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Map secret keys and vault references to this project for injection into
                    deployment workloads.
                  </p>
                </div>
                <Badge variant="outline">§1.8</Badge>
              </div>
              <ProjectSecrets projectId={projectId} />
            </section>

            <section
              aria-labelledby="activity-heading"
              className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
            >
              <div className="flex items-center justify-between border-b border-border/60 pb-3">
                <h2 id="activity-heading" className="text-lg font-semibold tracking-tight">
                  Activity
                </h2>
                <Badge variant="outline">Audit</Badge>
              </div>
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
    <div className="rounded-lg border border-border bg-card p-4 shadow-sm">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground font-semibold">
        {label}
      </dt>
      <dd className={mono ? "mt-1 break-all font-mono text-xs" : "mt-1 text-sm font-medium"}>
        {value}
      </dd>
    </div>
  );
}

/**
 * Add and remove tags — PRD FR-02's write half.
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
    <section
      aria-labelledby="tags-heading"
      className="rounded-xl border border-border bg-card/40 p-6 shadow-sm space-y-4"
    >
      <div className="flex items-center justify-between border-b border-border/60 pb-3">
        <h2 id="tags-heading" className="text-lg font-semibold tracking-tight">
          Tags
        </h2>
        <Badge variant="outline">Metadata</Badge>
      </div>
      <div className="space-y-3">
        <ul className="flex flex-wrap gap-2">
          {(project.tags ?? []).length === 0 ? (
            <li className="text-sm text-muted-foreground">No tags yet.</li>
          ) : (
            (project.tags ?? []).map((tag) => (
              <li key={tag}>
                <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/40 px-3 py-1 text-xs font-medium">
                  {tag}
                  <button
                    type="button"
                    onClick={() => remove.mutate(tag)}
                    disabled={remove.isPending}
                    data-testid={`remove-tag-${tag}`}
                    aria-label={`Remove tag ${tag}`}
                    className="hover:text-destructive text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  >
                    ×
                  </button>
                </span>
              </li>
            ))
          )}
        </ul>

        <form
          className="flex flex-wrap items-center gap-2 pt-1"
          onSubmit={(event) => {
            event.preventDefault();
            const trimmed = draft.trim();
            if (trimmed !== "") add.mutate(trimmed);
          }}
        >
          <label htmlFor="tag-draft" className="text-xs text-muted-foreground font-medium">
            Add a tag
          </label>
          <input
            id="tag-draft"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            maxLength={64}
            placeholder="e.g. production"
            className="flex h-8 w-44 rounded-md border border-input bg-background px-2.5 py-1 text-xs shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <Button
            type="submit"
            size="sm"
            disabled={draft.trim() === "" || add.isPending}
            className="h-8 px-3 text-xs"
          >
            Add
          </Button>
          <span className="text-xs text-muted-foreground">stored lower-cased</span>
        </form>
      </div>
    </section>
  );
}

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
        <div className="space-y-4">
          {!readiness.data.indexed ? (
            <div
              data-testid="detail-readiness-unscanned"
              className="rounded-lg border border-border bg-muted/20 p-4 text-sm"
            >
              <p className="font-semibold text-foreground">Not scanned.</p>
              <p className="mt-1 text-xs text-muted-foreground">
                Readiness is measured from the codebase index, and this project has none, so there
                is no score — not a score of zero. Pair an agent or scan the codebase in the index
                panel above.
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="flex items-center gap-3">
                <div
                  data-testid="detail-readiness-score"
                  className="flex h-12 w-12 items-center justify-center rounded-lg bg-primary/10 text-xl font-bold text-primary"
                >
                  {readiness.data.score}
                </div>
                <div>
                  <span className="text-sm font-semibold">
                    Overall readiness score ({readiness.data.score}/100 — {readiness.data.level})
                  </span>
                  <p className="text-xs text-muted-foreground">
                    Based on Dockerfiles, K8s manifests, and deployment configuration
                  </p>
                </div>
              </div>

              {readiness.data.categories && Object.keys(readiness.data.categories).length > 0 && (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {Object.entries(readiness.data.categories).map(([key, score]) => (
                    <div
                      key={key}
                      className="rounded-lg border border-border bg-card p-3 shadow-sm"
                    >
                      <dt className="text-xs text-muted-foreground">{categoryLabel(key)}</dt>
                      <dd className="mt-1 text-lg font-bold">{score}</dd>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          <p className="text-xs pt-1">
            <Link
              href="/readiness"
              className="font-medium text-primary underline underline-offset-4 hover:opacity-80"
            >
              Open the full breakdown, with each check and why it matters →
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
    <AsyncState isPending={secrets.isPending} error={secrets.error} label="secret references">
      <div className="space-y-4">
        <div className="rounded-lg border border-border/80 bg-muted/20 p-4 text-xs space-y-2">
          <p className="font-semibold text-foreground text-sm">Managing Secrets for this Project</p>
          <p className="text-muted-foreground leading-relaxed">
            Secret references map sensitive credentials (such as database passwords, API tokens, or
            private keys) to your deployment manifests without ever hardcoding secrets in Git.
            Whenever your deployment manifests or developer tools reference an environment variable,
            register its key and target environment below.
          </p>
          <div className="pt-1">
            <Link
              href="/vault"
              className="inline-flex items-center text-xs font-medium text-primary underline underline-offset-4 hover:opacity-80"
            >
              Open global Vault dashboard →
            </Link>
          </div>
        </div>

        <SecretVault secrets={secrets.data ?? []} projectId={projectId} />
      </div>
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
      <ul className="divide-y divide-border rounded-lg border border-border bg-card shadow-sm">
        {activity.data?.map((item) => (
          <li key={item.id} className="p-4 text-sm">
            <div className="flex items-baseline justify-between gap-4">
              <span className="font-medium text-foreground">{item.action}</span>
              <time className="font-mono text-xs text-muted-foreground" dateTime={item.timestamp}>
                {item.timestamp}
              </time>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">{item.details}</p>
          </li>
        ))}
      </ul>
    </AsyncState>
  );
}
