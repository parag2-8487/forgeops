// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * Pick a repository from the connected GitHub account.
 *
 * REAL API DATA ONLY. Every row here came from `GET /user/repos` with the signed-in person's own
 * linked credential, fetched for this page. There is no fallback list and no placeholder row: a picker
 * that showed a plausible repository when it could not reach GitHub would have somebody choose one and
 * then fail at the clone, naming neither.
 *
 * SEARCH AND PAGING ARE THE SERVER'S. `GET /integrations/github/repositories` filters and pages over
 * what the account can actually reach — GitHub's own search endpoint searches all of GitHub, which
 * would offer a stranger's repository in a picker. The response carries `total` and `truncated`, and
 * both are rendered: "12 of 340" rather than implying the page is everything, and an explicit notice
 * when the walk hit its bound, because a user whose repository is past the bound would otherwise be
 * told it does not exist.
 *
 * THE THREE ABSENCES ARE RENDERED AS ABSENCES. No linked account, no repositories at all, and no
 * matches for a query are three different states with three different next steps, and none of them is
 * an empty table.
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
    // A picker must not serve a stale list: a repository renamed since the last look would be chosen
    // and then fail at the clone.
    staleTime: 0,
  });

  if (repositories.isPending) {
    return (
      <p data-testid="repo-picker-loading" className="text-sm text-muted-foreground">
        Reading your repositories from GitHub…
      </p>
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
      <p
        data-testid={absent ? "repo-picker-unlinked" : "repo-picker-error"}
        role={absent ? undefined : "alert"}
        className="text-sm"
      >
        {detail}
      </p>
    );
  }

  const listing = repositories.data;
  // AN UNUSABLE RESPONSE IS REPORTED, NOT THROWN. A component that crashes takes its whole page with
  // it, and the page this sits on is the one a new user meets first — so a body that is not a
  // repository page says so, which is also how this was caught: a sibling test stubbed the projects
  // endpoint only, this component fetched a different one, and the create form's page went blank.
  if (!listing || !Array.isArray(listing.items)) {
    return (
      <p data-testid="repo-picker-error" role="alert" className="text-sm">
        The repository list could not be read: the server did not return a repository page.
      </p>
    );
  }
  const pages = Math.max(1, Math.ceil(listing.total / listing.per_page));

  return (
    <div className="space-y-3">
      <div>
        <label htmlFor="repo-search" className="block text-sm font-medium">
          Find a repository
        </label>
        <input
          id="repo-search"
          data-testid="repo-picker-search"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            // Back to the first page, because page 3 of the old result set is not page 3 of the new
            // one and leaving it would show an empty page for a query that has matches.
            setPage(1);
          }}
          placeholder="name, owner/name, or language"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
        className="divide-y divide-border rounded-md border border-border"
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
                className={`flex w-full flex-col items-start gap-1 px-3 py-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                  chosen ? "bg-accent" : ""
                }`}
              >
                <span className="font-mono">{repository.full_name}</span>
                <span className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                  <span>{repository.private ? "private" : "public"}</span>
                  <span>default branch {repository.default_branch}</span>
                  {/* Absence rendered as absence: GitHub reported no primary language. */}
                  <span>{repository.language ? repository.language : "no language reported"}</span>
                  <span>
                    {repository.pushed_at ? `pushed ${repository.pushed_at}` : "never pushed"}
                  </span>
                  {repository.archived ? <span>archived</span> : null}
                </span>
              </button>
            </li>
          );
        })}
      </ul>

      {pages > 1 ? (
        <div className="flex items-center gap-3 text-sm">
          <button
            type="button"
            data-testid="repo-picker-prev"
            disabled={listing.page <= 1}
            onClick={() => setPage((current) => Math.max(1, current - 1))}
            className="rounded-md border border-border px-2 py-1 disabled:opacity-50"
          >
            Previous
          </button>
          <span data-testid="repo-picker-page">
            Page {listing.page} of {pages}
          </span>
          <button
            type="button"
            data-testid="repo-picker-next"
            disabled={listing.page >= pages}
            onClick={() => setPage((current) => current + 1)}
            className="rounded-md border border-border px-2 py-1 disabled:opacity-50"
          >
            Next
          </button>
        </div>
      ) : null}
    </div>
  );
}
