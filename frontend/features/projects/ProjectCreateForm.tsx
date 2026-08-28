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
  const [repoUrl, setRepoUrl] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.post<ProjectResponse>("/projects", {
        name: name.trim(),
        path: path.trim(),
        // Empty means absent. An empty string would be stored as a repository URL that is not one,
        // and the readiness engine and the import path both branch on its presence.
        repo_url: source === "github" && repoUrl.trim() !== "" ? repoUrl.trim() : null,
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
      setRepoUrl("");
      onCreated?.(project);
    },
  });

  const canSubmit = name.trim() !== "" && path.trim() !== "" && !create.isPending;

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

      {source === "github" ? (
        <div>
          <label htmlFor="project-repo" className="block text-sm font-medium">
            Repository URL
          </label>
          <input
            id="project-repo"
            type="url"
            value={repoUrl}
            onChange={(event) => setRepoUrl(event.target.value)}
            placeholder="https://github.com/owner/repo"
            maxLength={1024}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
      ) : null}

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
          Typed, not chosen with a file picker — a browser cannot report a directory&apos;s absolute
          path, so there is no control that could fill this in for you. The backend records it as a
          reference and never opens it; the agent running on that machine is what reads the tree. If
          the path is wrong, scans will find nothing, and the project&apos;s index status says so.
        </p>
      </div>

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

      <GovernanceRefusal error={create.error} action="create this project" />
    </form>
  );
}
