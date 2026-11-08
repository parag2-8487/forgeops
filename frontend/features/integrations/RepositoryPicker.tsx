// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * Pick a repository from the connected GitHub account.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiProblemError, queryKeys } from "@/lib/api";

/** Mirrors `RepositoryItem` in `backend/src/integrations/routes.py`. */
export interface RepositoryItem {
  full_name: string;
  owner: string;
  name: string;
  private: boolean;
  default_branch: string;
  /** Empty means GitHub reported no primary language. Rendered as absence, never as a word. */
  language: string;
  pushed_at: string;
  clone_url: string;
  html_url: string;
  size_kb: number;
  archived: boolean;
}

/** Mirrors `RepositoryPage`. */
export interface RepositoryPage {
  items: RepositoryItem[];
  total: number;
  page: number;
  per_page: number;
  truncated: boolean;
}

const PER_PAGE = 10;

export function RepositoryPicker({
  selected,
  onSelect,
}: {
  selected: RepositoryItem | null;
  onSelect: (repository: RepositoryItem) => void;
}) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);

  const repositories = useQuery<RepositoryPage>({
    queryKey: queryKeys.integrations.githubRepositories(query, page, PER_PAGE),
    queryFn: () =>
      api.get<RepositoryPage>(
        `/integrations/github/repositories?query=${encodeURIComponent(query)}&page=${page}&per_page=${PER_PAGE}`,
      ),
    staleTime: 0,
  });

  if (repositories.isPending) {
    return (
      <div className="rounded-lg border border-border p-4 text-sm text-muted-foreground">
        <p data-testid="repo-picker-loading">Reading your repositories from GitHub…</p>
      </div>
    );
  }

  if (repositories.isError) {
    const problem = repositories.error as ApiProblemError | Error;
    const detail =
      problem instanceof ApiProblemError
        ? problem.problem.detail || problem.problem.title
        : problem.message;
    const absent =
      problem instanceof ApiProblemError &&
      Boolean(problem.problem.type?.endsWith("github-link-absent"));
    return (
      <div
        className={`rounded-lg border p-4 text-sm ${absent ? "border-border bg-muted/20" : "border-destructive/40 bg-destructive/10 text-destructive"}`}
      >
        <p
          data-testid={absent ? "repo-picker-unlinked" : "repo-picker-error"}
          role={absent ? undefined : "alert"}
        >
          {detail}
        </p>
      </div>
    );
  }

  const listing = repositories.data;
  if (!listing || !Array.isArray(listing.items)) {
    return (
      <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
        <p data-testid="repo-picker-error" role="alert">
          The repository list could not be read: the server did not return a repository page.
        </p>
      </div>
    );
  }
  const pages = Math.max(1, Math.ceil(listing.total / listing.per_page));

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <label htmlFor="repo-search" className="block text-sm font-medium">
          Find a repository
        </label>
        <input
          id="repo-search"
          data-testid="repo-picker-search"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setPage(1);
          }}
          placeholder="Search by name, owner/name, or language"
          className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>

      <p data-testid="repo-picker-count" className="text-xs text-muted-foreground">
        {listing.total === 0
          ? query
            ? `No repository matches “${query}”.`
            : "Your linked GitHub account can reach no repositories. Install the ForgeOps GitHub App on a repository, or use a token with access to one."
          : `Showing ${listing.items.length} of ${listing.total}`}
        {listing.truncated
          ? " — this is the first 1,000 repositories your account can reach; narrow the search to see further."
          : ""}
      </p>

      <ul
        data-testid="repo-picker-list"
        className="divide-y divide-border rounded-lg border border-border bg-card overflow-hidden shadow-sm"
      >
        {listing.items.map((repository) => {
          const chosen = selected?.full_name === repository.full_name;
          return (
            <li key={repository.full_name}>
              <button
                type="button"
                data-testid={`repo-option-${repository.full_name}`}
                aria-pressed={chosen}
                onClick={() => onSelect(repository)}
                className={`flex w-full flex-col items-start gap-1.5 px-4 py-3 text-left text-sm transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                  chosen
                    ? "bg-accent text-accent-foreground font-medium border-l-4 border-l-primary"
                    : ""
                }`}
              >
                <span className="font-mono text-sm font-semibold">{repository.full_name}</span>
                <span className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span className="rounded bg-muted px-1.5 py-0.5 font-medium">
                    {repository.private ? "private" : "public"}
                  </span>
                  <span>default branch {repository.default_branch}</span>
                  {repository.language ? (
                    <span className="rounded bg-muted px-1.5 py-0.5">{repository.language}</span>
                  ) : (
                    <span>no language reported</span>
                  )}
                  <span>
                    {repository.pushed_at ? `pushed ${repository.pushed_at}` : "never pushed"}
                  </span>
                  {repository.archived ? (
                    <span className="rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 px-1.5 py-0.5">
                      archived
                    </span>
                  ) : null}
                </span>
              </button>
            </li>
          );
        })}
      </ul>

      {pages > 1 ? (
        <div className="flex items-center justify-between pt-1 text-sm">
          <button
            type="button"
            data-testid="repo-picker-prev"
            disabled={listing.page <= 1}
            onClick={() => setPage((current) => Math.max(1, current - 1))}
            className="rounded-md border border-border px-3 py-1.5 text-xs font-medium disabled:opacity-50 hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Previous
          </button>
          <span data-testid="repo-picker-page" className="text-xs text-muted-foreground">
            Page {listing.page} of {pages}
          </span>
          <button
            type="button"
            data-testid="repo-picker-next"
            disabled={listing.page >= pages}
            onClick={() => setPage((current) => current + 1)}
            className="rounded-md border border-border px-3 py-1.5 text-xs font-medium disabled:opacity-50 hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Next
          </button>
        </div>
      ) : null}
    </div>
  );
}
