// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";
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
  const [installationId, setInstallationId] = useState("");
  const [owner, setOwner] = useState("");
  const [repo, setRepo] = useState("");

  const create = useMutation({
    // TWO ENDPOINTS, because the two sources are genuinely different operations and the backend
    // already models them that way.
    //
    // THE DEFECT THIS FIXES. `POST /projects/import/github` reads the repository over the real
    // GitHub API and records it, and this form never called it: choosing "A Git repository" posted
    // to `POST /projects` with the URL as metadata nothing reads, and still demanded a typed local
    // path. So the backend could import from GitHub and the only screen that offers to could not.
    // The same "exists with the right name, nothing calls it" shape the endpoint itself was added
    // to fix, one layer up.
    mutationFn: () =>
      source === "github"
        ? api.post<ProjectResponse>("/projects/import/github", {
            // The App INSTALLATION is what grants access; the App model has no notion of a token
            // that is not scoped to one, so there is no import without it.
            installation_id: Number(installationId.trim()),
            owner: owner.trim(),
            repo: repo.trim(),
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
      setInstallationId("");
      setOwner("");
      setRepo("");
      onCreated?.(project);
    },
  });

  // PER BRANCH, because the two operations need different things. An import takes its name from the
  // repository the API reports, so asking for one here would offer a value the backend ignores; and
  // it has no local path to give, which is what made the old single rule wrong — choosing GitHub
  // still demanded a directory the user had not cloned yet.
  const canSubmit =
    !create.isPending &&
    (source === "github"
      ? /^\d+$/.test(installationId.trim()) && owner.trim() !== "" && repo.trim() !== ""
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
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label htmlFor="project-owner" className="block text-sm font-medium">
                Owner
              </label>
              <input
                id="project-owner"
                value={owner}
                onChange={(event) => setOwner(event.target.value)}
                required
                placeholder="octocat"
                maxLength={39}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            <div>
              <label htmlFor="project-repo" className="block text-sm font-medium">
                Repository
              </label>
              <input
                id="project-repo"
                value={repo}
                onChange={(event) => setRepo(event.target.value)}
                required
                placeholder="hello-world"
                maxLength={100}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
          </div>

          <div>
            <label htmlFor="project-installation" className="block text-sm font-medium">
              App installation ID
            </label>
            <input
              id="project-installation"
              value={installationId}
              onChange={(event) => setInstallationId(event.target.value)}
              required
              inputMode="numeric"
              pattern="[0-9]*"
              placeholder="12345678"
              aria-describedby="project-installation-help"
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <p id="project-installation-help" className="mt-1 text-xs text-muted-foreground">
              Access is granted to an <em>installation</em> of the ForgeOps GitHub App, not to the
              app itself, so this identifies which one to read the repository through. It is in the
              URL of the installation&apos;s settings page on GitHub.
            </p>
          </div>

          <p className="text-xs text-muted-foreground">
            The import records the repository and derives the project&apos;s name from it. It does
            not clone anything: <strong>an agent still needs a local checkout to scan.</strong> The
            project stores <code>owner/repo</code> as its path, and the agent is pointed at a real
            directory through <code>AGENT_WORKSPACE_ROOT</code> when you run it. Nothing here reads
            your disk.
          </p>
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
