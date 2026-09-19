// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The multi-project workspace — PRD FR-01 through FR-05, phases.md §1.2.
 *
 * THE ASSERTIONS THAT MATTER ARE ABOUT REQUESTS, NOT ABOUT RENDERING.
 *
 * Search, tags and favourites are applied by `GET /api/v1/projects` in SQL, and the cheap wrong
 * version filters the fetched page in the browser. Both look identical on screen with six projects.
 * The difference only appears at scale — a browser-side filter pages the UNFILTERED sequence, so page
 * two of a search silently omits matches — so what is asserted is the query string the page sent,
 * which is the only observable that distinguishes the two implementations.
 *
 * The same reasoning applies to `readinessScore: 0`. Asserting that a score is absent proves nothing;
 * asserting that an unscanned project is DESCRIBED as unscanned is the property.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockGet, mockPost, mockPut, mockPatch, mockDelete, mockDeleteWith } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPut: vi.fn(),
  mockPatch: vi.fn(),
  mockDelete: vi.fn(),
  mockDeleteWith: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: mockGet,
      post: mockPost,
      put: mockPut,
      patch: mockPatch,
      delete: mockDelete,
      deleteWith: mockDeleteWith,
      stream: vi.fn(),
    },
  };
});

vi.mock("next/navigation", () => ({
  usePathname: () => "/projects",
  useParams: () => ({ projectId: "p-1" }),
}));

vi.mock("next/link", () => ({
  default: ({
    children,
    href,
    ...props
  }: {
    children: React.ReactNode;
    href: string;
  } & React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

import ProjectsPage from "@/app/(shell)/projects/page";
import ProjectDetailPage from "@/app/(shell)/projects/[projectId]/page";
import type { ProjectResponse } from "@/features/projects/types";

function renderPage(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function project(overrides: Partial<ProjectResponse> = {}): ProjectResponse {
  return {
    id: "p-1",
    name: "Checkout Service",
    path: "/srv/checkout",
    repo_url: null,
    settings: {},
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
    archived_at: null,
    tags: [],
    favourite: false,
    indexed_file_count: 0,
    ...overrides,
  };
}

/** The list, the tag vocabulary, and everything else routed to `rest`. */
function serve(
  projects: ProjectResponse[],
  tags: string[] = [],
  rest: (path: string) => Promise<unknown> = (p) =>
    Promise.reject(new Error(`unexpected GET ${p}`)),
) {
  mockGet.mockImplementation((path: string) => {
    if (path.startsWith("/projects/tags")) return Promise.resolve(tags);
    if (path.startsWith("/projects?")) return Promise.resolve({ projects, next_cursor: null });
    // THE CREATE FORM LEGITIMATELY READS THIS. Its GitHub branch cannot decide what to render without
    // knowing whether a GitHub account is linked, so every test that mounts the projects page issues
    // this request. Rejecting it made twenty unrelated tests time out on a page that was busy
    // rendering an error for a request the harness had refused — the harness being wrong, not the page.
    //
    // A TEST'S OWN STUB WINS. `rest` is tried first and the default only answers when it declines, so
    // the tests that are ABOUT the picker can serve a linked account and a repository page, while the
    // twenty that are about something else get the honest default: nothing linked.
    if (path.startsWith("/integrations/github")) {
      return Promise.resolve(rest(path)).catch(() =>
        githubLink({ connected: false, login: null, credential_kind: null }),
      );
    }
    return rest(path);
  });
}

/**
 * The GitHub link status a connected user has. Built here rather than inline so the create-form tests
 * state only what they are about — three of them care solely about `connected`.
 */
function githubLink(overrides: Record<string, unknown> = {}) {
  return {
    configured: true,
    connected: true,
    token_link_available: true,
    configuration_hint: "",
    login: "octo-cat",
    avatar_url: null,
    scopes: [],
    credential_kind: "personal_token",
    connected_at: "2026-09-19T10:00:00+00:00",
    last_use_ok: null,
    last_used_at: null,
    last_use_detail: "",
    access_token_expires_at: null,
    ...overrides,
  };
}

/** One row of the repository picker, shaped as `GET /integrations/github/repositories` returns it. */
function repositoryRow(overrides: Record<string, unknown> = {}) {
  return {
    full_name: "octo-org/deploy-me",
    owner: "octo-org",
    name: "deploy-me",
    private: true,
    default_branch: "main",
    language: "Python",
    pushed_at: "2026-09-18T10:11:12Z",
    clone_url: "https://github.com/octo-org/deploy-me.git",
    html_url: "https://github.com/octo-org/deploy-me",
    size_kb: 1024,
    archived: false,
    ...overrides,
  };
}

/** The query string of the most recent list request. */
function lastListQuery(): string {
  const calls = mockGet.mock.calls
    .map((c) => String(c[0]))
    .filter((p) => p.startsWith("/projects?"));
  return calls[calls.length - 1] ?? "";
}

beforeEach(() => {
  for (const m of [mockGet, mockPost, mockPut, mockPatch, mockDelete, mockDeleteWith])
    m.mockReset();
});
afterEach(() => cleanup());

// ── PRD FR-01: create ────────────────────────────────────────────────────────────────────────────

describe("creating a project", () => {
  it("posts the name, the path and a null repo url when none was given", async () => {
    serve([]);
    mockPost.mockResolvedValue(project());
    renderPage(<ProjectsPage />);

    await userEvent.click(screen.getByLabelText(/directory on the machine/i));
    await userEvent.type(screen.getByLabelText("Name"), "Checkout Service");
    await userEvent.type(screen.getByLabelText(/working-tree path/i), "/srv/checkout");
    await userEvent.click(screen.getByRole("button", { name: /create project/i }));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/projects", {
        name: "Checkout Service",
        path: "/srv/checkout",
        // NULL, not "". An empty string would be stored as a repository URL that is not one, and both
        // the readiness engine and the import path branch on its presence.
        repo_url: null,
        settings: {},
      }),
    );
  });

  it("offers the connect step, and no picker, when no GitHub account is linked", async () => {
    // THE STATE EVERY NEW USER IS IN. The picker cannot work without a link, and a 409 rendered as an
    // error would send somebody looking for a fault. The connect step appears in its place — on this
    // form rather than only in Settings, because this is the one screen where its absence blocks the
    // thing the user came to do.
    serve([], [], (path) =>
      path.startsWith("/integrations/github")
        ? Promise.resolve(githubLink({ connected: false, login: null }))
        : Promise.reject(new Error(`unexpected GET ${path}`)),
    );
    renderPage(<ProjectsPage />);

    expect(await screen.findByTestId("project-github-connect")).toBeInTheDocument();
    expect(screen.queryByTestId("repo-picker-list")).not.toBeInTheDocument();
    // And the field nobody had is gone for good.
    expect(screen.queryByLabelText(/app installation id/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create project/i })).toBeDisabled();
  });

  it("creates the project from the repository picked out of the real listing", async () => {
    // THE DEFECT THIS PINS. The GitHub branch used to ask for an App installation id, an owner and a
    // repository typed by hand — and nobody has an installation id, so the path was unusable by the
    // people it was for. What is asserted is the REQUEST: the full name comes from the row the user
    // clicked, and the parent directory is passed through as typed.
    serve([], [], (path) => {
      if (path.startsWith("/integrations/github/repositories")) {
        return Promise.resolve({
          items: [repositoryRow()],
          total: 1,
          page: 1,
          per_page: 10,
          truncated: false,
        });
      }
      if (path.startsWith("/integrations/github")) return Promise.resolve(githubLink());
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    mockPost.mockResolvedValue(project());
    renderPage(<ProjectsPage />);

    await userEvent.click(await screen.findByTestId("repo-option-octo-org/deploy-me"));
    await userEvent.type(screen.getByTestId("project-parent-directory"), "/srv/workspaces");
    await userEvent.click(screen.getByRole("button", { name: /create project/i }));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/projects/from-github", {
        repo_full_name: "octo-org/deploy-me",
        parent_directory: "/srv/workspaces",
        // Blank means "use the repository's own name" and "use its default branch". The backend reads
        // both from the API rather than this form guessing them.
        directory_name: "",
        branch: "",
      }),
    );
  });

  it("says on the form where the clone lands and whose machine it is", async () => {
    // A browser cannot hand over an absolute path, so the parent is typed — and a typed path that is
    // not explained is how somebody types a directory on the SERVER and waits for a clone that will
    // never appear there.
    serve([], [], (path) => {
      if (path.startsWith("/integrations/github/repositories")) {
        return Promise.resolve({
          items: [repositoryRow()],
          total: 1,
          page: 1,
          per_page: 10,
          truncated: false,
        });
      }
      if (path.startsWith("/integrations/github")) return Promise.resolve(githubLink());
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    renderPage(<ProjectsPage />);

    await userEvent.click(await screen.findByTestId("repo-option-octo-org/deploy-me"));

    // The target is shown before anything is created, and it is composed from what the user chose.
    expect(screen.getByTestId("project-clone-target")).toHaveTextContent(
      "<agent workspace root>/deploy-me",
    );
    await userEvent.type(screen.getByTestId("project-parent-directory"), "/srv/ws");
    expect(screen.getByTestId("project-clone-target")).toHaveTextContent("/srv/ws/deploy-me");
    // Matched on the containing element, because the sentence is broken up by a `<strong>` — the
    // emphasis is on "your agent runs on", which is the half a user gets wrong.
    expect(screen.getByTestId("project-parent-help")).toHaveTextContent(
      /machine your agent runs on/i,
    );
    // And the honest state the project starts in.
    expect(screen.getByText(/awaiting clone/i)).toBeInTheDocument();
  });

  it("asks for no local path or name when the source is GitHub, because there is none to give", async () => {
    serve([], [], (path) =>
      path.startsWith("/integrations/github")
        ? Promise.resolve(githubLink({ connected: false, login: null }))
        : Promise.reject(new Error(`unexpected GET ${path}`)),
    );
    renderPage(<ProjectsPage />);
    await screen.findByTestId("project-github-connect");
    expect(screen.queryByLabelText(/working-tree path/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
  });

  it("explains why the path is typed rather than chosen with a file picker", async () => {
    serve([]);
    renderPage(<ProjectsPage />);
    // Only on the local branch now, which is the only branch that has a path at all.
    await userEvent.click(screen.getByLabelText(/directory on the machine/i));
    // The constraint is real and stated: a browser cannot report a directory's absolute path, so
    // there is no control that could fill this in. Leaving it unexplained makes the form look lazy.
    expect(screen.getByText(/a browser cannot report a directory/i)).toBeInTheDocument();
    expect(
      screen.getByText(/the backend records it as a reference and never opens it/i),
    ).toBeInTheDocument();
  });

  it("will not submit without both required fields", async () => {
    serve([]);
    renderPage(<ProjectsPage />);
    await userEvent.click(screen.getByLabelText(/directory on the machine/i));
    const submit = screen.getByRole("button", { name: /create project/i });
    expect(submit).toBeDisabled();

    await userEvent.type(screen.getByLabelText("Name"), "Only a name");
    expect(submit).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/working-tree path/i), "/srv/x");
    expect(submit).toBeEnabled();
  });

  it("will not submit the GitHub branch until a repository is chosen", async () => {
    serve([], [], (path) => {
      if (path.startsWith("/integrations/github/repositories")) {
        return Promise.resolve({
          items: [repositoryRow()],
          total: 1,
          page: 1,
          per_page: 10,
          truncated: false,
        });
      }
      if (path.startsWith("/integrations/github")) return Promise.resolve(githubLink());
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    renderPage(<ProjectsPage />);

    await screen.findByTestId("repo-picker-list");
    const submit = screen.getByRole("button", { name: /create project/i });
    // A linked account is not a chosen repository: submitting here would post an empty full name and
    // be refused by the backend's pattern, which is a 422 for a mistake the form can prevent.
    expect(submit).toBeDisabled();

    await userEvent.click(screen.getByTestId("repo-option-octo-org/deploy-me"));
    expect(submit).toBeEnabled();
  });

  it("renders a refusal from the server rather than claiming success", async () => {
    serve([]);
    const { ApiProblemError } = await import("@/lib/api");
    mockPost.mockRejectedValue(
      new ApiProblemError({
        type: "https://errors.forgeops.dev/validation-failed",
        title: "Request validation failed",
        status: 422,
        detail: "unknown project settings key(s) ['nope']",
      }),
    );
    renderPage(<ProjectsPage />);

    await userEvent.click(screen.getByLabelText(/directory on the machine/i));
    await userEvent.type(screen.getByLabelText("Name"), "X");
    await userEvent.type(screen.getByLabelText(/working-tree path/i), "/x");
    await userEvent.click(screen.getByRole("button", { name: /create project/i }));

    expect(await screen.findByTestId("governance-refusal")).toHaveTextContent(
      /unknown project settings key/i,
    );
    expect(screen.queryByText(/^Created\./)).not.toBeInTheDocument();
  });
});

// ── PRD FR-02, FR-03: server-side search, tags and favourites ────────────────────────────────────

describe("the filters are sent to the endpoint, not applied in the browser", () => {
  it("sends no filter parameters before anything is chosen", async () => {
    serve([project()]);
    renderPage(<ProjectsPage />);
    await waitFor(() => expect(lastListQuery()).not.toBe(""));
    const query = lastListQuery();
    expect(query).toContain("limit=25");
    expect(query).not.toContain("search=");
    expect(query).not.toContain("tag=");
    expect(query).not.toContain("favourite=");
    // Default view is ACTIVE projects, and `archived` is only sent when asked for.
    expect(query).not.toContain("archived=");
  });

  it("sends the search term as a query parameter when the form is submitted", async () => {
    serve([project()]);
    renderPage(<ProjectsPage />);
    await screen.findByTestId("project-list");

    await userEvent.type(screen.getByLabelText(/search name or path/i), "checkout");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    // THE PROPERTY. A browser-side filter would never produce this parameter, and would page the
    // unfiltered sequence — so page two of a search would drop matches.
    await waitFor(() => expect(lastListQuery()).toContain("search=checkout"));
  });

  it("does not search on every keystroke", async () => {
    serve([project()]);
    renderPage(<ProjectsPage />);
    await screen.findByTestId("project-list");
    const before = mockGet.mock.calls.length;

    await userEvent.type(screen.getByLabelText(/search name or path/i), "checkout");
    // Submitting is explicit, so one deliberate search is one request and the operator can see which
    // term produced the list they are reading.
    expect(mockGet.mock.calls.length).toBe(before);
  });

  it("sends every selected tag, repeated, so the endpoint can require all of them", async () => {
    serve([project()], ["eu", "prod"]);
    renderPage(<ProjectsPage />);

    await userEvent.click(await screen.findByTestId("tag-filter-prod"));
    await userEvent.click(screen.getByTestId("tag-filter-eu"));

    await waitFor(() => {
      const query = lastListQuery();
      expect(query).toContain("tag=prod");
      expect(query).toContain("tag=eu");
    });
  });

  it("exposes tag chips as toggles a screen reader can read the state of", async () => {
    serve([project()], ["prod"]);
    renderPage(<ProjectsPage />);
    const chip = await screen.findByTestId("tag-filter-prod");
    expect(chip).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(chip);
    expect(chip).toHaveAttribute("aria-pressed", "true");
  });

  it("says the tag filter narrows rather than widens", async () => {
    serve([project()], ["prod"]);
    renderPage(<ProjectsPage />);
    // Conjunctive is surprising unless stated: most tag filters are disjunctive.
    expect(await screen.findByText(/a project must carry every tag chosen/i)).toBeInTheDocument();
  });

  it("sends favourite=true only when the box is ticked", async () => {
    serve([project()]);
    renderPage(<ProjectsPage />);
    await userEvent.click(await screen.findByTestId("filter-favourites"));
    await waitFor(() => expect(lastListQuery()).toContain("favourite=true"));
  });

  it("sends archived=true as a separate view rather than an inclusive flag", async () => {
    serve([project({ archived_at: "2026-08-25T00:00:00Z" })]);
    renderPage(<ProjectsPage />);
    await userEvent.click(await screen.findByTestId("filter-archived"));
    await waitFor(() => expect(lastListQuery()).toContain("archived=true"));
  });

  it("says an empty filtered result is the whole answer, not one empty page", async () => {
    serve([]);
    renderPage(<ProjectsPage />);
    await userEvent.type(screen.getByLabelText(/search name or path/i), "nothing");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText(/the filters run server-side/i)).toBeInTheDocument();
  });

  it("stars and unstars through the two idempotent verbs", async () => {
    serve([project({ favourite: false })]);
    mockPut.mockResolvedValue(project({ favourite: true }));
    renderPage(<ProjectsPage />);

    await userEvent.click(await screen.findByTestId("favourite-p-1"));
    await waitFor(() => expect(mockPut).toHaveBeenCalledWith("/projects/p-1/favourite"));

    cleanup();
    serve([project({ favourite: true })]);
    mockDelete.mockResolvedValue(project({ favourite: false }));
    renderPage(<ProjectsPage />);
    await userEvent.click(await screen.findByTestId("favourite-p-1"));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/projects/p-1/favourite"));
  });
});

// ── Part D.1: the hardcoded score ────────────────────────────────────────────────────────────────

describe("the project row reports index state instead of a fabricated score", () => {
  it("describes an unscanned project in words rather than as a zero", async () => {
    serve([project({ indexed_file_count: 0 })]);
    renderPage(<ProjectsPage />);
    const cell = await screen.findByTestId("index-p-1");
    expect(cell).toHaveTextContent(/not scanned/i);
    // The literal that used to be rendered for EVERY project, regardless of its real score.
    expect(cell).not.toHaveTextContent("0/100");
    // The row says a score CANNOT be computed yet, which is different from displaying one. Asserting
    // the absence of the phrase "readiness score" would forbid the row from explaining itself, so what
    // is asserted is that no NUMBER is presented as a score.
    expect(cell).toHaveTextContent(/no readiness score can be computed yet/i);
    expect(cell.textContent ?? "").not.toMatch(/\d+\s*\/\s*100/);
  });

  it("reports the real file count for a scanned project", async () => {
    serve([project({ indexed_file_count: 1 }), project({ id: "p-2", indexed_file_count: 141 })]);
    renderPage(<ProjectsPage />);
    // Singular and plural, because "1 files indexed" is the kind of detail that makes a screen look
    // unfinished.
    expect(await screen.findByTestId("index-p-1")).toHaveTextContent("1 file indexed");
    expect(screen.getByTestId("index-p-2")).toHaveTextContent("141 files indexed");
  });
});

// ── PRD FR-05: archive and delete ────────────────────────────────────────────────────────────────

describe("archiving a project", () => {
  it("requires a reason before the control is usable", async () => {
    serve([project()]);
    renderPage(<ProjectsPage />);
    await userEvent.click(await screen.findByTestId("archive-p-1"));

    const confirm = screen.getByTestId("archive-confirm");
    // NFR-14 makes "why" one of the six fields every governance record carries, and the endpoint
    // refuses an empty one. A prefilled default would satisfy the validator and tell a reader nothing.
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason"), "work concluded");
    expect(confirm).toBeEnabled();
  });

  it("posts to archive with the reason", async () => {
    serve([project()]);
    mockPost.mockResolvedValue(project({ archived_at: "2026-08-28T00:00:00Z" }));
    renderPage(<ProjectsPage />);

    await userEvent.click(await screen.findByTestId("archive-p-1"));
    await userEvent.type(screen.getByLabelText("Reason"), "work concluded");
    await userEvent.click(screen.getByTestId("archive-confirm"));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/projects/p-1/archive", { reason: "work concluded" }),
    );
  });

  it("posts to unarchive for a project that is already archived", async () => {
    serve([project({ archived_at: "2026-08-25T00:00:00Z" })]);
    mockPost.mockResolvedValue(project());
    renderPage(<ProjectsPage />);

    // The same control, labelled for what it will do.
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await userEvent.type(screen.getByLabelText("Reason"), "resumed");
    await userEvent.click(screen.getByTestId("archive-confirm"));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/projects/p-1/unarchive", { reason: "resumed" }),
    );
  });

  it("says plainly that nothing is destroyed", async () => {
    serve([project()]);
    renderPage(<ProjectsPage />);
    await userEvent.click(await screen.findByTestId("archive-p-1"));
    expect(screen.getByText(/nothing is deleted, nothing is unindexed/i)).toBeInTheDocument();
  });
});

describe("deleting a project", () => {
  it("states the cascade before it happens, including what survives", async () => {
    serve([project()]);
    renderPage(<ProjectsPage />);
    await userEvent.click(await screen.findByTestId("delete-p-1"));

    expect(screen.getByText(/not reversible/i)).toBeInTheDocument();
    expect(screen.getByText(/both vector tables/i)).toBeInTheDocument();
    // The audit log surviving is the part a user would otherwise assume wrong in either direction.
    expect(screen.getByText(/audit log does not/i)).toBeInTheDocument();
  });

  it("requires the project's exact name to be typed back", async () => {
    serve([project()]);
    renderPage(<ProjectsPage />);
    await userEvent.click(await screen.findByTestId("delete-p-1"));
    await userEvent.type(screen.getByLabelText("Reason"), "no longer needed");

    const confirm = screen.getByTestId("delete-confirm-button");
    expect(confirm).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/type the project name/i), "Checkout Servic");
    expect(confirm).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/type the project name/i), "e");
    expect(confirm).toBeEnabled();
  });

  it("deletes with a body carrying the reason and the confirmation", async () => {
    serve([project()]);
    mockDeleteWith.mockResolvedValue({
      project_id: "p-1",
      cascaded: { file_tree: 141, change_sets: 2 },
      audit_events_retained: 9,
    });
    renderPage(<ProjectsPage />);

    await userEvent.click(await screen.findByTestId("delete-p-1"));
    await userEvent.type(screen.getByLabelText("Reason"), "no longer needed");
    await userEvent.type(screen.getByLabelText(/type the project name/i), "Checkout Service");
    await userEvent.click(screen.getByTestId("delete-confirm-button"));

    await waitFor(() =>
      expect(mockDeleteWith).toHaveBeenCalledWith("/projects/p-1", {
        reason: "no longer needed",
        confirm_name: "Checkout Service",
      }),
    );
  });

  it("reports what the cascade removed, and how many audit records were kept", async () => {
    serve([project()]);
    mockDeleteWith.mockResolvedValue({
      project_id: "p-1",
      cascaded: { file_tree: 141, change_sets: 2 },
      audit_events_retained: 9,
    });
    renderPage(<ProjectsPage />);

    await userEvent.click(await screen.findByTestId("delete-p-1"));
    await userEvent.type(screen.getByLabelText("Reason"), "r");
    await userEvent.type(screen.getByLabelText(/type the project name/i), "Checkout Service");
    await userEvent.click(screen.getByTestId("delete-confirm-button"));

    const report = await screen.findByTestId("deletion-report");
    // Counted before the row went, so this is what actually happened rather than an estimate.
    expect(report).toHaveTextContent("file_tree");
    expect(report).toHaveTextContent("141");
    expect(screen.getByText(/9/)).toBeInTheDocument();
    expect(screen.getByText(/history outlives the thing it describes/i)).toBeInTheDocument();
  });
});

// ── phases.md §1.2: the detail page ──────────────────────────────────────────────────────────────

describe("the project detail page", () => {
  function serveDetail(overrides: Partial<ProjectResponse> = {}) {
    mockGet.mockImplementation((path: string) => {
      if (path === "/projects/p-1") return Promise.resolve(project(overrides));
      if (path.endsWith("/readiness")) {
        return Promise.resolve({
          project_id: "p-1",
          score: 0,
          level: "blocked",
          summary_report: "no indexed files",
          recommendations: [],
          categories: {},
          indexed: false,
          evaluated_paths: 0,
          checks: [],
        });
      }
      if (path.includes("/analysis/codebase/")) {
        return Promise.resolve({
          indexed_files: 0,
          total_chunks: 0,
          languages: [],
          status: "empty",
          total_bytes: 0,
          resolved_dependencies: 0,
          unresolved_dependencies: 0,
          last_indexed_at: null,
        });
      }
      if (path.startsWith("/approvals?")) {
        return Promise.resolve({ change_sets: [], next_cursor: null });
      }
      if (path.startsWith("/secrets?")) return Promise.resolve([]);
      if (path.endsWith("/activity")) return Promise.resolve([]);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
  }

  it("reads the one project by the id in the path", async () => {
    serveDetail();
    renderPage(<ProjectDetailPage />);
    // `GET /projects/{id}` existed, was tested, and had no caller at all — the list screen rendered a
    // pane that showed the name it already had.
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/projects/p-1"));
    expect(
      await screen.findByRole("heading", { level: 1, name: /Checkout Service/ }),
    ).toBeInTheDocument();
  });

  it("gathers the facts that were previously spread across five id-driven screens", async () => {
    serveDetail();
    renderPage(<ProjectDetailPage />);
    for (const heading of [
      "Codebase index",
      "Readiness",
      "Change history",
      "Secret references",
      "Activity",
      "Tags",
    ]) {
      expect(await screen.findByRole("heading", { level: 2, name: heading })).toBeInTheDocument();
    }
  });

  it("says an unscanned project has no score rather than showing it a zero", async () => {
    serveDetail();
    renderPage(<ProjectDetailPage />);
    const panel = await screen.findByTestId("detail-readiness-unscanned");
    expect(panel).toHaveTextContent(/not scanned/i);
    expect(panel).toHaveTextContent(/not a score of zero/i);
    expect(screen.queryByTestId("detail-readiness-score")).not.toBeInTheDocument();
  });

  it("adds a tag through the idempotent PUT", async () => {
    serveDetail();
    mockPut.mockResolvedValue(project({ tags: ["prod"] }));
    renderPage(<ProjectDetailPage />);

    await userEvent.type(await screen.findByLabelText(/add a tag/i), "Prod");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    // Sent as typed; the server lower-cases. The help text says so, so the operator is not surprised.
    await waitFor(() =>
      expect(mockPut).toHaveBeenCalledWith("/projects/p-1/tags", { tag: "Prod" }),
    );
    expect(screen.getByText(/stored lower-cased/i)).toBeInTheDocument();
  });

  it("removes a tag, url-encoding it so an unusual tag cannot break the path", async () => {
    serveDetail({ tags: ["needs review"] });
    mockDelete.mockResolvedValue(project({ tags: [] }));
    renderPage(<ProjectDetailPage />);

    await userEvent.click(await screen.findByTestId("remove-tag-needs review"));
    await waitFor(() =>
      expect(mockDelete).toHaveBeenCalledWith("/projects/p-1/tags/needs%20review"),
    );
  });
});
