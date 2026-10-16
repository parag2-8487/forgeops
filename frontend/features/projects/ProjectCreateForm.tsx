// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";
import { GitHubConnection, useGitHubLink } from "@/features/integrations/GitHubConnection";
import { RepositoryPicker, type RepositoryItem } from "@/features/integrations/RepositoryPicker";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { ProjectResponse } from "./types";

/**
 * Create a project — PRD FR-01, which is P0 and had no form at all.
 */
export function ProjectCreateForm({
  onCreated,
}: {
  onCreated?: (project: ProjectResponse) => void;
}) {
  const queryClient = useQueryClient();
  const [source, setSource] = useState<"github" | "local">("github");
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [repository, setRepository] = useState<RepositoryItem | null>(null);
  const [parentDirectory, setParentDirectory] = useState("");

  const link = useGitHubLink();

  const create = useMutation({
    mutationFn: () =>
      source === "github"
        ? api.post<ProjectResponse>("/projects/from-github", {
            repo_full_name: repository?.full_name ?? "",
            parent_directory: parentDirectory.trim(),
            directory_name: "",
            branch: "",
          })
        : api.post<ProjectResponse>("/projects", {
            name: name.trim(),
            path: path.trim(),
            repo_url: null,
            settings: {},
          }),
    onSuccess: (project) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
      setName("");
      setPath("");
      setRepository(null);
      setParentDirectory("");
      onCreated?.(project);
    },
  });

  const canSubmit =
    !create.isPending &&
    (source === "github"
      ? repository !== null && Boolean(link.data?.connected)
      : name.trim() !== "" && path.trim() !== "");

  const unconfigured = Boolean(
    (create.error as { problem?: { type?: string } } | null)?.problem?.type?.endsWith(
      "repository-import-unconfigured",
    ),
  );

  const handleSubmit = (event?: React.FormEvent) => {
    if (event) event.preventDefault();
    if (canSubmit) create.mutate();
  };

  return (
    <Card className="border border-border shadow-sm">
      <CardHeader>
        <CardTitle className="text-lg font-semibold">Create a project</CardTitle>
        <CardDescription>
          A project is the scope everything else hangs off: an agent pairs to one, a policy bundle
          is published for one, and a change set belongs to one.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-6">
          <fieldset>
            <legend className="text-sm font-semibold mb-3">Choose Source</legend>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <label
                className={`relative flex cursor-pointer rounded-lg border p-4 transition-all focus-within:ring-2 focus-within:ring-ring ${
                  source === "github"
                    ? "border-primary bg-primary/5 shadow-sm"
                    : "border-border hover:border-border/80 bg-card"
                }`}
              >
                <div className="flex items-start gap-3">
                  <input
                    type="radio"
                    name="project-source"
                    value="github"
                    checked={source === "github"}
                    onChange={() => setSource("github")}
                    className="mt-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                  <div>
                    <span className="block text-sm font-medium text-foreground">
                      A Git repository
                    </span>
                    <span className="mt-1 block text-xs text-muted-foreground">
                      Clone from GitHub into your agent&apos;s workspace and track branches.
                    </span>
                  </div>
                </div>
              </label>

              <label
                className={`relative flex cursor-pointer rounded-lg border p-4 transition-all focus-within:ring-2 focus-within:ring-ring ${
                  source === "local"
                    ? "border-primary bg-primary/5 shadow-sm"
                    : "border-border hover:border-border/80 bg-card"
                }`}
              >
                <div className="flex items-start gap-3">
                  <input
                    type="radio"
                    name="project-source"
                    value="local"
                    checked={source === "local"}
                    onChange={() => setSource("local")}
                    className="mt-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                  <div>
                    <span className="block text-sm font-medium text-foreground">
                      A directory on the machine the agent runs on
                    </span>
                    <span className="mt-1 block text-xs text-muted-foreground">
                      Point to an existing directory that a local agent already owns.
                    </span>
                  </div>
                </div>
              </label>
            </div>
          </fieldset>

          {source === "local" ? (
            <div className="space-y-4 rounded-lg border border-border bg-muted/20 p-4">
              <div className="space-y-1.5">
                <label htmlFor="project-name" className="block text-sm font-medium">
                  Name
                </label>
                <input
                  id="project-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && canSubmit) handleSubmit(event);
                  }}
                  required
                  maxLength={200}
                  placeholder="e.g. checkout-service"
                  className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
              </div>

              <div className="space-y-1.5">
                <label htmlFor="project-path" className="block text-sm font-medium">
                  Working-tree path
                </label>
                <input
                  id="project-path"
                  value={path}
                  onChange={(event) => setPath(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && canSubmit) handleSubmit(event);
                  }}
                  required
                  maxLength={1024}
                  placeholder="/srv/projects/checkout"
                  aria-describedby="project-path-help"
                  className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
                <p id="project-path-help" className="mt-1.5 text-xs text-muted-foreground">
                  Typed, not chosen with a file picker — a browser cannot report a directory&apos;s
                  absolute path, so there is no control that could fill this in for you. The backend
                  records it as a reference and never opens it; the agent running on that machine is
                  what reads the tree. If the path is wrong, scans will find nothing, and the
                  project&apos;s index status says so.
                </p>
              </div>
            </div>
          ) : null}

          {source === "github" ? (
            <div className="space-y-4">
              {link.isPending ? (
                <div className="rounded-lg border border-border p-4 text-sm text-muted-foreground">
                  <p data-testid="project-github-checking">Checking your GitHub connection…</p>
                </div>
              ) : link.data?.connected !== true ? (
                <div data-testid="project-github-connect" className="space-y-4">
                  <div className="rounded-md border border-border/80 bg-muted/30 p-3 text-sm text-muted-foreground">
                    Connect a GitHub account to choose a repository. It takes one step and stays on
                    this page — no GitHub sign-in or account-selection screen.
                  </div>
                  <GitHubConnection />
                </div>
              ) : (
                <div className="space-y-4 rounded-lg border border-border bg-muted/20 p-4">
                  <RepositoryPicker selected={repository} onSelect={setRepository} />

                  <div className="space-y-1.5 pt-2">
                    <label htmlFor="project-parent" className="block text-sm font-medium">
                      Clone into
                    </label>
                    <input
                      id="project-parent"
                      data-testid="project-parent-directory"
                      value={parentDirectory}
                      onChange={(event) => setParentDirectory(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && canSubmit) handleSubmit(event);
                      }}
                      placeholder="leave blank to use the agent's workspace root"
                      aria-describedby="project-parent-help"
                      className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    />
                    <p
                      id="project-parent-help"
                      data-testid="project-parent-help"
                      className="mt-1.5 text-xs text-muted-foreground leading-relaxed"
                    >
                      This is a directory on the machine <strong>your agent runs on</strong>, not on
                      this server — a browser cannot read or choose a path on your disk, so it is
                      typed. The repository is cloned to{" "}
                      <code
                        data-testid="project-clone-target"
                        className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs"
                      >
                        {(parentDirectory.trim() || "<agent workspace root>") +
                          "/" +
                          (repository?.name ?? "<repository>")}
                      </code>{" "}
                      by the agent, shallow at depth 1. It must be inside the agent&apos;s
                      <code> AGENT_WORKSPACE_ROOT</code>; anything else is refused by the agent, not
                      by this form.
                    </p>
                  </div>

                  <div className="rounded-md border border-border/60 bg-background/50 p-3 text-xs text-muted-foreground">
                    The project is created straight away, pointing at that path, and reports{" "}
                    <strong>awaiting clone</strong> until the agent has fetched it — so a project
                    never silently points at a directory that does not exist. Pair an agent for the
                    project, then start the clone from the project page. After that, scanning,
                    readiness, generation, approval and apply work exactly as for a local directory.
                  </div>
                </div>
              )}
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-3 pt-2">
            <Button
              type="button"
              disabled={!canSubmit}
              onClick={() => handleSubmit()}
              className="px-5 py-2 font-medium"
            >
              {create.isPending ? "Creating…" : "Create project"}
            </Button>
            {create.isSuccess ? (
              <p
                role="status"
                className="text-sm text-emerald-600 dark:text-emerald-400 font-medium"
              >
                Created. Next: mint a pairing code so an agent can scan it.
              </p>
            ) : null}
          </div>

          {unconfigured ? (
            <div
              role="alert"
              className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300"
            >
              <p className="font-medium">GitHub import is not configured on this deployment.</p>
              <p className="mt-1 text-xs text-muted-foreground">
                Set <code>GITHUB_APP_ID</code> and <code>GITHUB_APP_PRIVATE_KEY</code> in the
                backend&apos;s environment and restart it. Both ship unset, so this is the state of
                a fresh install rather than a fault. Until then, choose{" "}
                <strong>A directory on the machine the agent runs on</strong> — that path needs no
                credentials at all.
              </p>
            </div>
          ) : (
            <GovernanceRefusal
              error={create.error}
              action={source === "github" ? "import this repository" : "create this project"}
            />
          )}
        </div>
      </CardContent>
    </Card>
  );
}
