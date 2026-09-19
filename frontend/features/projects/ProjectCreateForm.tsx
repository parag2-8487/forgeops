// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";
import { GitHubConnection, useGitHubLink } from "@/features/integrations/GitHubConnection";
import { RepositoryPicker, type RepositoryItem } from "@/features/integrations/RepositoryPicker";
import type { ProjectResponse } from "./types";

/**
 * Create a project — PRD FR-01, which is P0 and had no form at all.
 *
 * `POST /api/v1/projects` has existed and been tested since the projects surface was fixed; nothing
 * called it, so the only way to get a project into the system was curl. That is the first step of the
 * onboarding path, so its absence made every screen downstream unreachable for a new user.
 *
 * THE LOCAL-PATH CONSTRAINT, STATED HONESTLY
 * FR-01 says "import from GitHub or a local folder", and a browser cannot deliver a local folder.
 * `<input type="file" webkitdirectory>` yields file contents and relative names, never the absolute
 * path on disk — by design, and no permission prompt changes it. The three options were:
 *
 *   1. a typed absolute path;
 *   2. upload the directory's contents through the browser;
 *   3. have the AGENT report a directory it already owns.
 *
 * (2) is wrong: it would make the backend hold source it has no store for, duplicate what the agent's
 * scan already does properly, and route an entire tree through an HTTP request. (3) is the right
 * long-term shape and is §1.2's own "Agent: Register project directory" — but it needs a paired
 * agent, and pairing is scoped to a project, so a project must exist first. Using (3) alone would
 * make project creation circular.
 *
 * So this ships (1), and says so on the form: the path is recorded as a REFERENCE for the agent that
 * will later scan it, and the backend never opens it. A wrong path is not a security problem, it is a
 * project whose scans find nothing — which the index status on the detail page reports plainly rather
 * than leaving to be discovered. When (3) exists, it becomes a second option here rather than a
 * replacement, because a typed path is still the only way to describe a directory before an agent is
 * paired.
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

  // Whether this user has a GitHub account linked. Read here rather than assumed, because the GitHub
  // branch of this form is unusable without one and the honest thing is to offer the connect step in
  // place of a picker that would answer 409.
  const link = useGitHubLink();

  const create = useMutation({
    // TWO ENDPOINTS, because the two sources are genuinely different operations and the backend
    // already models them that way.
    //
    // THE GITHUB BRANCH NO LONGER ASKS FOR AN INSTALLATION ID. It posts the `full_name` the picker
    // listed, which the backend re-reads from the API with this user's own credential before writing
    // anything — so a repository renamed or made private since the list was fetched fails there with
    // GitHub's reason rather than later as a clone that cannot authenticate.
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
            // Empty means absent. An empty string would be stored as a repository URL that is not
            // one, and the readiness engine branches on its presence.
            repo_url: null,
            settings: {},
          }),
    onSuccess: (project) => {
      // Invalidated at the ROOT of the projects key space, not at one list key. Every filtered list,
      // the tag vocabulary and the picker all have to see the new row, and the filters are part of
      // the key — so invalidating only the current filter would leave a project the user just made
      // missing from the list they navigate to next.
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
      setName("");
      setPath("");
      setRepository(null);
      setParentDirectory("");
      onCreated?.(project);
    },
  });

  // PER BRANCH, because the two operations need different things. The GitHub branch needs a chosen
  // repository and nothing else: the name comes from the repository the API reports, and the parent
  // directory may legitimately be blank, which means "the agent's workspace root".
  const canSubmit =
    !create.isPending &&
    (source === "github"
      ? repository !== null && Boolean(link.data?.connected)
      : name.trim() !== "" && path.trim() !== "");

  // `GitHubAppNotConfiguredError` maps to a 503 with this type, and it is the state of EVERY fresh
  // install: `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` ship unset. Surfacing the bare status
  // would read as "the server is broken" when the answer is two settings, so it is named here.
  //
  // Read from `problem.type`, which is where `ApiProblemError` keeps the RFC 9457 body — the error
  // object itself has no `type`, and reading one off it would make this branch permanently dead.
  // Matched on `endsWith` because a deployment may serve the type as a full URI.
  const unconfigured = Boolean(
    (create.error as { problem?: { type?: string } } | null)?.problem?.type?.endsWith(
      "repository-import-unconfigured",
    ),
  );

  return (
    <form
      className="space-y-4 rounded-lg border border-border bg-background p-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSubmit) create.mutate();
      }}
    >
      <div>
        <h2 className="text-sm font-semibold">Create a project</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          A project is the scope everything else hangs off: an agent pairs to one, a policy bundle
          is published for one, and a change set belongs to one.
        </p>
      </div>

      <fieldset>
        <legend className="text-sm font-medium">Source</legend>
        <div className="mt-2 flex flex-wrap gap-4">
          {(
            [
              ["github", "A Git repository"],
              ["local", "A directory on the machine the agent runs on"],
            ] as const
          ).map(([value, label]) => (
            <label key={value} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="project-source"
                value={value}
                checked={source === value}
                onChange={() => setSource(value)}
                className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
              {label}
            </label>
          ))}
        </div>
      </fieldset>

      {source === "local" ? (
        <div>
          <label htmlFor="project-name" className="block text-sm font-medium">
            Name
          </label>
          <input
            id="project-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            maxLength={200}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
      ) : null}

      {source === "github" ? (
        <>
          {/*
            WHAT REPLACED THE THREE TYPED FIELDS. This branch used to ask for an App installation id,
            an owner and a repository name — and nobody has the first of those, so the GitHub path was
            unusable by the people it was for. It now lists the repositories the signed-in person's
            linked account can actually reach and they pick one.
          */}
          {link.isPending ? (
            <p data-testid="project-github-checking" className="text-sm text-muted-foreground">
              Checking your GitHub connection…
            </p>
          ) : link.data?.connected !== true ? (
            <div data-testid="project-github-connect" className="space-y-2">
              <p className="text-sm">
                Connect a GitHub account to choose a repository. It takes one step and stays on this
                page — no GitHub sign-in or account-selection screen.
              </p>
              <GitHubConnection />
            </div>
          ) : (
            <>
              <RepositoryPicker selected={repository} onSelect={setRepository} />

              <div>
                <label htmlFor="project-parent" className="block text-sm font-medium">
                  Clone into
                </label>
                <input
                  id="project-parent"
                  data-testid="project-parent-directory"
                  value={parentDirectory}
                  onChange={(event) => setParentDirectory(event.target.value)}
                  placeholder="leave blank to use the agent's workspace root"
                  aria-describedby="project-parent-help"
                  className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
                <p
                  id="project-parent-help"
                  data-testid="project-parent-help"
                  className="mt-1 text-xs text-muted-foreground"
                >
                  {/*
                    SAYS WHAT IS HAPPENING, as the local-path field does and for the same reason: a
                    browser cannot hand over an absolute path, so this is typed. The agent refuses
                    anything outside the workspace root it was started with, which is why blank is the
                    normal answer.
                  */}
                  This is a directory on the machine <strong>your agent runs on</strong>, not on
                  this server — a browser cannot read or choose a path on your disk, so it is typed.
                  The repository is cloned to{" "}
                  <code data-testid="project-clone-target">
                    {(parentDirectory.trim() || "<agent workspace root>") +
                      "/" +
                      (repository?.name ?? "<repository>")}
                  </code>{" "}
                  by the agent, shallow at depth 1. It must be inside the agent&apos;s
                  <code> AGENT_WORKSPACE_ROOT</code>; anything else is refused by the agent, not by
                  this form.
                </p>
              </div>

              <p className="text-xs text-muted-foreground">
                The project is created straight away, pointing at that path, and reports{" "}
                <strong>awaiting clone</strong> until the agent has fetched it — so a project never
                silently points at a directory that does not exist. Pair an agent for the project,
                then start the clone from the project page. After that, scanning, readiness,
                generation, approval and apply work exactly as for a local directory.
              </p>
            </>
          )}
        </>
      ) : null}

      {source === "local" ? (
        <div>
          <label htmlFor="project-path" className="block text-sm font-medium">
            Working-tree path
          </label>
          <input
            id="project-path"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            required
            maxLength={1024}
            placeholder="/srv/projects/checkout"
            aria-describedby="project-path-help"
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <p id="project-path-help" className="mt-1 text-xs text-muted-foreground">
            Typed, not chosen with a file picker — a browser cannot report a directory&apos;s
            absolute path, so there is no control that could fill this in for you. The backend
            records it as a reference and never opens it; the agent running on that machine is what
            reads the tree. If the path is wrong, scans will find nothing, and the project&apos;s
            index status says so.
          </p>
        </div>
      ) : null}

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={!canSubmit}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {create.isPending ? "Creating…" : "Create project"}
        </button>
        {create.isSuccess ? (
          <p role="status" className="text-sm text-muted-foreground">
            Created. Next: mint a pairing code so an agent can scan it.
          </p>
        ) : null}
      </div>

      {unconfigured ? (
        <div
          role="alert"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm"
        >
          <p className="font-medium">GitHub import is not configured on this deployment.</p>
          <p className="mt-1 text-muted-foreground">
            Set <code>GITHUB_APP_ID</code> and <code>GITHUB_APP_PRIVATE_KEY</code> in the
            backend&apos;s environment and restart it. Both ship unset, so this is the state of a
            fresh install rather than a fault. Until then, choose{" "}
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
    </form>
  );
}
