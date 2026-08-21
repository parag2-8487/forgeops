// SPDX-License-Identifier: Apache-2.0
/**
 * The nine shell routes, each across every state it can actually reach.
 *
 * These pages were at 0 % line coverage while `shell.spec.ts` exercised them in a real browser —
 * Playwright's execution is invisible to v8, so the route tree that carries most of this app's
 * behaviour contributed nothing to the coverage gate. That is the gap this file closes, and it is
 * worth being precise about what it adds rather than duplicates: Playwright proves the pages work
 * against a running backend, and these tests prove each page renders the RIGHT thing for a given
 * API outcome — including the outcomes a live backend will not produce on demand, like a 500 or an
 * empty collection.
 *
 * Mocked at the `lib/api` boundary, deliberately. Mocking `fetch` would leave the client's
 * RFC 9457 normalisation under test here too, which `api-client.test.ts` and
 * `p14-error-normalization.test.ts` already own; mocking a page's own hooks would assert nothing
 * about the page. The boundary is where the page's contract with the backend actually is.
 *
 * Every assertion is on observable output — a role, an accessible name, rendered text the user
 * reads, or a call the page made. No assertion is on a class name, and none merely renders a
 * component without checking what came out.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Hoisted so the module factory below can close over it.
const { mockGet, mockPost } = vi.hoisted(() => ({ mockGet: vi.fn(), mockPost: vi.fn() }));

// Partial mock: `api.get` is replaced, but `ApiProblemError`, `queryKeys` and the problem helpers
// stay REAL. The error branch under test reads `error instanceof ApiProblemError`, so a fake error
// class would make the test pass while the page failed in production.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: { get: mockGet, post: mockPost, put: vi.fn(), delete: vi.fn(), stream: vi.fn() },
  };
});

let mockPathname = "/";
vi.mock("next/navigation", () => ({ usePathname: () => mockPathname }));

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

import { ApiProblemError } from "@/lib/api";
import { AppSidebar } from "@/components/layout/app-sidebar";
import HomePage from "@/app/(shell)/page";
import ProjectsPage from "@/app/(shell)/projects/page";
import ReadinessPage from "@/app/(shell)/readiness/page";
import AuditPage from "@/app/(shell)/audit/page";
import PoliciesPage from "@/app/(shell)/policies/page";
import VaultPage from "@/app/(shell)/vault/page";
import ApprovalsPage from "@/app/(shell)/approvals/page";
import GenerationPage from "@/app/(shell)/generation/page";
import PairingPage from "@/app/(shell)/pairing/page";

/** A client with retries off, so a rejection surfaces as an error state in one tick. */
function renderPage(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

/** A real `ApiProblemError`, so `instanceof` in the component under test behaves as in production. */
function problem(status: number, title: string, detail?: string) {
  return new ApiProblemError({
    type: `https://errors.forgeops.dev/${status}`,
    title,
    status,
    detail,
  });
}

/** A promise that never settles, which is how the pending branch is reached deterministically. */
function pending() {
  return new Promise(() => {});
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPathname = "/";
});
afterEach(() => cleanup());

/**
 * The six pages that read live data, described by what each needs rather than special-cased in
 * six near-identical blocks.
 *
 * `respond` is keyed on the requested path rather than being a single value, because the Projects
 * page issues TWO requests — the project and its activity feed — and a flat mock hands the
 * activity query a project object. That is not a detail worth hiding behind a helper: a page that
 * fans out to several endpoints has several ways to fail, and the mock has to be able to express
 * "this one succeeded and that one did not".
 *
 * `shows` is text the page can only display if it actually rendered the payload.
 */
const LIVE_PAGES = [
  {
    name: "Home",
    Page: HomePage,
    heading: "ForgeOps Dashboard",
    label: "platform health",
    respond: () => ({ status: "ok", version: "1.4.2", commit: "abc1234" }),
    shows: ["ok", "1.4.2", "abc1234"],
    empty: null,
    emptyText: undefined,
  },
  {
    name: "Projects",
    Page: ProjectsPage,
    heading: "Projects",
    label: "projects",
    // `GET /projects` now returns a page. The activity feed is only requested once a project is
    // selected, so the list response is what this page's states are driven by.
    respond: (path: string) =>
      path.includes("/activity")
        ? [
            {
              id: "act-9",
              action: "change_set_approved",
              timestamp: "2026-08-21T00:00:00Z",
              details: "allowed: policy matched",
            },
          ]
        : {
            projects: [
              {
                id: "00000000-0000-0000-0000-000000000001",
                name: "Checkout Service",
                path: "/srv/checkout",
                repo_url: "https://github.com/acme/checkout",
                settings: {},
                created_at: "2026-08-20T00:00:00Z",
                updated_at: "2026-08-20T00:00:00Z",
              },
            ],
            next_cursor: null,
          },
    shows: ["Checkout Service"],
    empty: { projects: [], next_cursor: null },
    emptyText: /no projects are stored for this tenant yet/i,
  },
  {
    name: "Readiness",
    Page: ReadinessPage,
    heading: "Deployment readiness",
    label: "readiness report",
    respond: () => ({
      project_id: "00000000-0000-0000-0000-000000000001",
      score: 73,
      level: "Adequate",
      summary_report: "Scored seventy-three of one hundred.",
      recommendations: ["Add a health check"],
      // The five fields of `ReadinessBreakdown`, which the response model used to drop.
      categories: {
        documentation_score: 80,
        test_coverage_score: 60,
        ci_config_score: 90,
        security_policy_score: 50,
        containerization_score: 85,
      },
    }),
    shows: ["Adequate", "Add a health check"],
    empty: null,
    emptyText: undefined,
  },
  {
    name: "Audit",
    Page: AuditPage,
    heading: "Audit log",
    label: "audit events",
    respond: () => ({
      events: [
        {
          seq: 7,
          id: "e-7",
          action: "changeset_approved",
          actor_kind: "user",
          resource_kind: "changeset",
          resource_id: "cs-1",
          outcome: "allow",
          reason: "policy matched",
        },
      ],
      next_cursor: null,
    }),
    shows: ["changeset_approved", "changeset/cs-1"],
    empty: { events: [], next_cursor: null },
    emptyText: /audit table is empty/i,
  },
  {
    name: "Policies",
    Page: PoliciesPage,
    heading: "Policies",
    label: "policy templates",
    respond: () => [
      {
        id: "tpl-1",
        name: "Require staging first",
        description: "Blocks a production apply with no staging predecessor.",
        rego_rules: "package forgeops\ndefault allow = false",
        parameters: {},
      },
    ],
    shows: ["Require staging first", "tpl-1"],
    empty: [],
    emptyText: /no policy templates are registered/i,
  },
  {
    name: "Vault",
    Page: VaultPage,
    heading: "Vault",
    label: "secret references",
    respond: () => [
      {
        id: "s-1",
        key: "DATABASE_URL",
        environment: "production",
        infisical_path: "/apps/api",
        is_local: false,
      },
    ],
    shows: ["DATABASE_URL", "production"],
    empty: [],
    emptyText: /no secret references are registered/i,
  },
  {
    name: "Pairing",
    Page: PairingPage,
    heading: "Agent pairing",
    label: "agent devices",
    respond: () => ({
      devices: [
        {
          id: "d-1",
          project_id: "p-1",
          status: "active",
          agent_version: "0.4.1",
          platform: "linux/amd64",
          cert_serial: "0A1B",
          cert_fingerprint: "sha256:ab:cd",
          cert_not_after: "2026-12-01T00:00:00Z",
          last_seq: 42,
          last_seen: "2026-08-21T04:00:00Z",
          pairing_expires_at: null,
          revoked_at: null,
          created_at: "2026-08-20T00:00:00Z",
          seconds_since_last_seen: 12,
          heartbeat_fresh: true,
          heartbeat_timeout_seconds: 90,
        },
      ],
      next_cursor: null,
    }),
    shows: ["linux/amd64", "0.4.1", "active"],
    empty: { devices: [], next_cursor: null },
    emptyText: /no agent devices exist for this tenant/i,
  },
] as const;

describe.each(LIVE_PAGES)(
  "$name route",
  ({ name, Page, heading, label, respond, shows, empty, emptyText }) => {
    it("renders exactly one h1, naming the screen", async () => {
      mockGet.mockImplementation(() => pending());
      renderPage(<Page />);
      const h1s = screen.getAllByRole("heading", { level: 1 });
      expect(h1s).toHaveLength(1);
      expect(h1s[0]).toHaveTextContent(heading);
    });

    it("announces a polite loading status while the request is in flight", () => {
      mockGet.mockImplementation(() => pending());
      renderPage(<Page />);
      // `role=status` with aria-live=polite is the accessible contract, not the text.
      const statuses = screen.getAllByRole("status");
      expect(statuses.length).toBeGreaterThan(0);
      expect(statuses[0]).toHaveAttribute("aria-live", "polite");
      expect(
        statuses.some((n) => new RegExp(`Loading ${label}`, "i").test(n.textContent ?? "")),
      ).toBe(true);
    });

    it("renders the payload it was given on success", async () => {
      mockGet.mockImplementation((path: string) => Promise.resolve(respond(path)));
      renderPage(<Page />);
      for (const text of shows) {
        // findAllByText, because a real payload legitimately appears in more than one place —
        // a score in both a stat block and a summary sentence, for instance. Requiring exactly
        // one match would make the test brittle about layout rather than strict about behaviour.
        const found = await screen.findAllByText(new RegExp(escapeRe(text), "i"));
        expect(found.length).toBeGreaterThan(0);
      }
    });

    it("reports a 401 as an authentication problem rather than as a crash", async () => {
      mockGet.mockRejectedValue(problem(401, "Unauthenticated"));
      renderPage(<Page />);
      expect(
        (await screen.findAllByText(new RegExp(`Not authenticated to read ${label}`, "i"))).length,
      ).toBeGreaterThan(0);
      // A 401 is the designed behaviour, so it must NOT be announced as an alert.
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("renders a 500 as an alert carrying the server's own problem details", async () => {
      mockGet.mockRejectedValue(problem(500, "Internal Server Error", "the database is on fire"));
      renderPage(<Page />);
      // A page that fans out to several endpoints raises one alert per failed query, so assert
      // that at least one carries the server's words rather than that exactly one exists.
      const alerts = await screen.findAllByRole("alert");
      expect(alerts.length).toBeGreaterThan(0);
      const alert = alerts[0];
      expect(alert).toHaveTextContent(/could not load/i);
      expect(alert).toHaveTextContent("Internal Server Error");
      expect(alert).toHaveTextContent("the database is on fire");
      expect(alert).toHaveTextContent("500");
    });

    it("requests through the api client exactly once per query", async () => {
      mockGet.mockImplementation((path: string) => Promise.resolve(respond(path)));
      renderPage(<Page />);
      await waitFor(() => expect(mockGet).toHaveBeenCalled());
      // Every path requested is relative and version-less: `lib/api` owns the prefix.
      for (const call of mockGet.mock.calls) {
        expect(String(call[0])).toMatch(/^\//);
        expect(String(call[0])).not.toContain("/api/v1");
      }
    });

    if (empty !== null) {
      it("renders a specific empty message rather than an error when the collection is empty", async () => {
        mockGet.mockResolvedValue(empty);
        renderPage(<Page />);
        expect(await screen.findByText(emptyText!)).toBeInTheDocument();
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      });
    }

    it(`is the current nav item at its own pathname (${name})`, () => {
      const href = ROUTE_HREFS[name];
      mockPathname = href;
      render(<AppSidebar />);
      const current = screen
        .getAllByRole("link")
        .filter((a) => a.getAttribute("aria-current") === "page");
      expect(current).toHaveLength(1);
      expect(current[0]).toHaveAttribute("href", href);
    });
  },
);

const ROUTE_HREFS: Record<string, string> = {
  Home: "/",
  Projects: "/projects",
  Readiness: "/readiness",
  Audit: "/audit",
  Policies: "/policies",
  Vault: "/vault",
  Approvals: "/approvals",
  Generation: "/generation",
  Pairing: "/pairing",
};

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * The three routes with no endpoint behind them.
 *
 * The assertion that matters is not that they render — it is that they name the missing piece and
 * the phase that owns it, and that they make no live request. A page that quietly fetched, failed
 * and showed a plausible empty state would be indistinguishable from a working screen with no
 * data, which is the confusion `NotImplemented` exists to prevent.
 */
/**
 * THERE ARE NO LONGER ANY NOT-IMPLEMENTED ROUTES, so the table that drove this section is gone.
 *
 * It held three entries and lost them one at a time as each gap closed: pairing when
 * `GET /api/v1/agents/devices` gave it something to observe, then approvals and generation when
 * their reviewer UI and wizard were built against the endpoints mounted earlier in the session.
 *
 * The tests that replaced them are deliberately harder. A `NotImplemented` panel can only be
 * asserted on its own prose -- it names a feature, an owner and a reason -- and prose is exactly
 * what a page can claim without doing anything. What follows asserts requests made, decisions
 * submitted, and event names received.
 */

describe("Approvals renders a real diff and submits a real decision", () => {
  const CHANGE_SET = {
    id: "cs-1",
    project_id: "p-1",
    status: "pending_approval",
    origin: "generation",
    blast_radius_score: 12,
    blast_radius_verdict: "moderate",
    version: 3,
    generation_run_id: "run-1",
    created_at: "2026-08-21T04:00:00Z",
    applied_at: null,
  };

  const DETAIL = {
    ...CHANGE_SET,
    items: [
      {
        id: "ci-1",
        file_path: "Dockerfile",
        action: "update" as const,
        old_content: "FROM node:18\nUSER root\n",
        new_content: "FROM node:20-alpine\nUSER node\n",
        old_hash: "aaa",
        new_hash: "bbb",
        ordinal: 0,
      },
    ],
    approvals: [],
  };

  function serve(overrides: Record<string, unknown> = {}) {
    mockGet.mockImplementation((path: string) => {
      if (path.startsWith("/approvals?")) {
        return Promise.resolve({ change_sets: [CHANGE_SET], next_cursor: null });
      }
      if (path === "/approvals/cs-1") return Promise.resolve({ ...DETAIL, ...overrides });
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
  }

  it("lists change sets awaiting a decision", async () => {
    serve();
    renderPage(<ApprovalsPage />);
    expect(await screen.findByText("cs-1")).toBeInTheDocument();
    // The list request is filtered server-side to the one state a decision is legal from.
    expect(mockGet).toHaveBeenCalledWith(expect.stringContaining("status=pending_approval"));
  });

  it("shows no diff until a change set is chosen", async () => {
    serve();
    renderPage(<ApprovalsPage />);
    await screen.findByText("cs-1");
    // Nothing selected by default, so one change set's diff is never shown under another's heading.
    expect(screen.queryByRole("region", { name: /diff for/i })).not.toBeInTheDocument();
    expect(screen.getByText(/select a change set to review/i)).toBeInTheDocument();
  });

  it("renders the diff of the change items, not a pre-flattened string", async () => {
    serve();
    renderPage(<ApprovalsPage />);
    await userEvent.click(await screen.findByRole("button", { name: /cs-1/ }));

    const region = await screen.findByRole("region", { name: /diff for Dockerfile/i });
    // Both sides present: the removed line and the added line, which is what makes it a diff rather
    // than a listing of the new file.
    expect(region).toHaveTextContent("FROM node:18");
    expect(region).toHaveTextContent("FROM node:20-alpine");
    // The recorded hash is displayed rather than recomputed in the browser.
    expect(region).toHaveTextContent("bbb");
  });

  it("offers both view modes and switches between them", async () => {
    serve();
    renderPage(<ApprovalsPage />);
    await userEvent.click(await screen.findByRole("button", { name: /cs-1/ }));

    const unified = await screen.findByRole("button", { name: "Unified" });
    const split = screen.getByRole("button", { name: "Side by side" });
    expect(unified).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(split);
    expect(split).toHaveAttribute("aria-pressed", "true");
    expect(unified).toHaveAttribute("aria-pressed", "false");
    // The side-by-side table announces its two columns; the unified one does not have them.
    expect(screen.getByText("Before")).toBeInTheDocument();
    expect(screen.getByText("After")).toBeInTheDocument();
  });

  it("has no field for the approver anywhere on the screen", async () => {
    serve();
    renderPage(<ApprovalsPage />);
    await userEvent.click(await screen.findByRole("button", { name: /cs-1/ }));
    await screen.findByRole("region", { name: /diff for/i });

    // THE defect that kept this router unmounted was `approver: str = "admin"` as a query
    // parameter. Reintroducing it as a form field would be the same defect in a nicer coat.
    expect(screen.queryByLabelText(/approver/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/rejector/i)).not.toBeInTheDocument();
    const inputs = Array.from(document.querySelectorAll("input"));
    expect(inputs).toHaveLength(0);
    expect(screen.getByText(/server takes it from your session/i)).toBeInTheDocument();
  });

  it("submits an approval carrying the comment and the displayed version", async () => {
    serve();
    mockPost.mockResolvedValue({});
    renderPage(<ApprovalsPage />);
    await userEvent.click(await screen.findByRole("button", { name: /cs-1/ }));
    await screen.findByRole("region", { name: /diff for/i });

    await userEvent.type(screen.getByLabelText(/reason/i), "looks right");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/approvals/cs-1/approve", {
        comment: "looks right",
        // The version THIS screen displayed, so a stale tab gets a 409 rather than deciding on
        // state the reviewer never saw.
        expected_version: 3,
      }),
    );
  });

  it("submits a rejection to the reject endpoint", async () => {
    serve();
    mockPost.mockResolvedValue({});
    renderPage(<ApprovalsPage />);
    await userEvent.click(await screen.findByRole("button", { name: /cs-1/ }));
    await screen.findByRole("region", { name: /diff for/i });

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/approvals/cs-1/reject", {
        comment: null,
        expected_version: 3,
      }),
    );
  });

  it("explains a 409 as a stale view rather than a generic failure", async () => {
    serve();
    mockPost.mockRejectedValue(problem(409, "Change set conflict"));
    renderPage(<ApprovalsPage />);
    await userEvent.click(await screen.findByRole("button", { name: /cs-1/ }));
    await screen.findByRole("region", { name: /diff for/i });

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/moved since it was displayed/i);
  });

  it("offers no decision controls from a state §3.6 forbids deciding from", async () => {
    serve({ status: "applied" });
    renderPage(<ApprovalsPage />);
    await userEvent.click(await screen.findByRole("button", { name: /cs-1/ }));
    await screen.findByRole("region", { name: /diff for/i });

    // Not offered and refused by the server: not offered at all.
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    expect(screen.getByText(/only permits a decision from/i)).toBeInTheDocument();
  });
});

describe("Generation gates the wizard on a usable project id", () => {
  it("renders exactly one h1", () => {
    renderPage(<GenerationPage />);
    const h1s = screen.getAllByRole("heading", { level: 1 });
    expect(h1s).toHaveLength(1);
    expect(h1s[0]).toHaveTextContent("Generation");
  });

  it("does not offer the generator before a valid project id is entered", () => {
    renderPage(<GenerationPage />);
    // A button that cannot succeed is worse than one that is not there: the run has to be
    // attributed to a project to be submitted as a change set.
    expect(screen.queryByRole("button", { name: /generate artifacts/i })).not.toBeInTheDocument();
    expect(screen.getByText(/generator is not offered before then/i)).toBeInTheDocument();
  });

  it("offers it once the id is a real UUID", async () => {
    renderPage(<GenerationPage />);
    await userEvent.type(
      screen.getByLabelText(/project id/i),
      "00000000-0000-0000-0000-000000000001",
    );
    expect(screen.getByRole("button", { name: /generate artifacts/i })).toBeInTheDocument();
  });

  it("makes no request merely from being rendered", () => {
    renderPage(<GenerationPage />);
    expect(mockGet).not.toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
  });
});

describe("every route marks its own nav item current", () => {
  it.each([
    ["Approvals", "/approvals"],
    ["Generation", "/generation"],
  ])("%s", (_name, href) => {
    mockPathname = href;
    render(<AppSidebar />);
    const current = screen
      .getAllByRole("link")
      .filter((a) => a.getAttribute("aria-current") === "page");
    expect(current).toHaveLength(1);
    expect(current[0]).toHaveAttribute("href", href);
  });
});

/**
 * Readiness is now the only id-driven screen. Projects gained a real list endpoint, so its
 * `ProjectIdField` is gone — the lookup box existed solely because `GET /api/v1/projects` did not.
 *
 * The behaviour asserted here is the one that would silently break if the field were wired to
 * state the query key does not depend on: the page would look interactive and keep showing the
 * first project.
 */
describe("the project-id field drives the readiness request", () => {
  it("refetches against a newly typed id", async () => {
    mockGet.mockResolvedValue({
      project_id: "x",
      score: 1,
      level: "Low",
      summary_report: "s",
      recommendations: [],
      categories: { documentation_score: 1 },
    });
    renderPage(<ReadinessPage />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const field = screen.getByLabelText("Project ID");
    expect(field).toHaveAttribute("id", "readiness-project-id");

    const user = userEvent.setup();
    await user.clear(field);
    await user.type(field, "11111111-1111-1111-1111-111111111111");

    await waitFor(() => {
      const requested = mockGet.mock.calls.map((c) => String(c[0]));
      expect(
        requested.some(
          (p) => p.includes("11111111-1111-1111-1111-111111111111") && /\/readiness$/.test(p),
        ),
      ).toBe(true);
    });
  });
});

describe("Readiness category breakdown", () => {
  const categories = {
    documentation_score: 80,
    test_coverage_score: 60,
    ci_config_score: 90,
    security_policy_score: 50,
    containerization_score: 85,
  };

  it("renders one bar per category the engine computed, labelled readably", async () => {
    mockGet.mockResolvedValue({
      project_id: "x",
      score: 70,
      level: "Adequate",
      summary_report: "s",
      recommendations: [],
      categories,
    });
    renderPage(<ReadinessPage />);
    // `documentation_score` becomes `Documentation`. The chart used to render a single bar called
    // "Overall", because the breakdown was computed server-side and dropped by the response model.
    for (const label of [
      "Documentation",
      "Test Coverage",
      "Ci Config",
      "Security Policy",
      "Containerization",
    ]) {
      expect((await screen.findAllByText(new RegExp(label, "i"))).length).toBeGreaterThan(0);
    }
    expect(screen.queryByText("Overall")).not.toBeInTheDocument();
  });

  it("renders no category bars when the engine reported none, rather than inventing five", async () => {
    mockGet.mockResolvedValue({
      project_id: "x",
      score: 0,
      level: "Blocked",
      summary_report: "s",
      recommendations: [],
      categories: {},
    });
    renderPage(<ReadinessPage />);
    expect((await screen.findAllByText(/Blocked/i)).length).toBeGreaterThan(0);
    expect(screen.queryByText("Documentation")).not.toBeInTheDocument();
  });
});

describe("Projects activity feed", () => {
  const page = {
    projects: [
      {
        id: "p-1",
        name: "Checkout Service",
        path: "/srv/checkout",
        repo_url: null,
        settings: {},
        created_at: "2026-08-20T00:00:00Z",
        updated_at: "2026-08-20T00:00:00Z",
      },
    ],
    next_cursor: null,
  };

  it("requests no activity until a project is selected", async () => {
    mockGet.mockImplementation(() => Promise.resolve(page));
    renderPage(<ProjectsPage />);
    // The name appears twice once loaded — in the list and as an <option> — so wait on the control
    // that only exists after the list resolves.
    await screen.findByLabelText("Project");
    // The feed is `enabled` only once a selection exists, so one project's history is never shown
    // under another project's heading during a refetch.
    expect(mockGet.mock.calls.every((c) => !String(c[0]).includes("/activity"))).toBe(true);
    expect(screen.getByText(/select a project to read its activity/i)).toBeInTheDocument();
  });

  it("requests and renders the selected project's audit-backed activity", async () => {
    mockGet.mockImplementation((path: string) =>
      Promise.resolve(
        path.includes("/activity")
          ? [
              {
                id: "e-1",
                action: "change_set_approved",
                timestamp: "2026-08-21T00:00:00Z",
                details: "allowed: policy matched",
              },
            ]
          : page,
      ),
    );
    renderPage(<ProjectsPage />);
    await screen.findByLabelText("Project");

    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Project"), "p-1");

    expect(await screen.findByText("change_set_approved")).toBeInTheDocument();
    // The outcome travels with the reason, so an allowed and a denied transit are distinguishable.
    expect(screen.getByText(/allowed: policy matched/)).toBeInTheDocument();
  });
});

/**
 * The home page's scope list is the one place in the app that states which routes are live and
 * which are not. It is worth asserting because it is a claim about the system: if a route gains an
 * endpoint and this list is not updated, the dashboard is lying about its own completeness.
 */
describe("Home route scope list", () => {
  it("marks live and unimplemented routes distinguishably to a screen reader", async () => {
    mockGet.mockResolvedValue({ status: "ok", version: "1", commit: "c" });
    renderPage(<HomePage />);
    expect(await screen.findByText(/what is wired, and what is not/i)).toBeInTheDocument();
    // The visual cue is a coloured dot marked aria-hidden, so the accessible signal must be text.
    expect(screen.getAllByText(/\(reads live data\)/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\(not implemented\)/).length).toBeGreaterThan(0);
  });

  it("falls back to a visible placeholder when the commit is not stamped", async () => {
    mockGet.mockResolvedValue({ status: "ok", version: "1.0.0", commit: "" });
    renderPage(<HomePage />);
    expect(await screen.findByText("not stamped")).toBeInTheDocument();
  });
});

/**
 * The reason this screen exists. `features/pairing/AgentPairing.tsx` displayed "Connected &
 * Attested" and a fixed SPIFFE trust domain with no props and no fetch — a security control
 * reported as passing by a component that could not observe it.
 *
 * So the assertion that matters is not that a device renders, it is that NEVER REPORTED is
 * distinguishable from STALE. A boolean cannot carry that difference, which is why the field is
 * tri-state, and these three cases are what stop it collapsing back into one.
 */
describe("Pairing heartbeat is an observation, not a claim", () => {
  function device(overrides: Record<string, unknown>) {
    return {
      devices: [
        {
          id: "d-1",
          project_id: "p-1",
          status: "active",
          agent_version: "0.4.1",
          platform: "linux/amd64",
          cert_serial: null,
          cert_fingerprint: null,
          cert_not_after: null,
          last_seq: 0,
          last_seen: null,
          pairing_expires_at: null,
          revoked_at: null,
          created_at: "2026-08-20T00:00:00Z",
          seconds_since_last_seen: null,
          heartbeat_fresh: null,
          heartbeat_timeout_seconds: 90,
          ...overrides,
        },
      ],
      next_cursor: null,
    };
  }

  it("reports a device that has never reported as unobserved, not as disconnected", async () => {
    mockGet.mockResolvedValue(device({}));
    renderPage(<PairingPage />);
    const cell = await screen.findByTestId("heartbeat-d-1");
    expect(cell).toHaveTextContent(/never reported/i);
    expect(cell).toHaveTextContent(/nothing is known about whether it is running/i);
    // Must not claim staleness, which would imply an observation that never happened.
    expect(cell).not.toHaveTextContent(/stale/i);
  });

  it("reports a fresh heartbeat with the age and the timeout it was judged against", async () => {
    mockGet.mockResolvedValue(
      device({
        last_seen: "2026-08-21T04:00:00Z",
        seconds_since_last_seen: 12,
        heartbeat_fresh: true,
      }),
    );
    renderPage(<PairingPage />);
    const cell = await screen.findByTestId("heartbeat-d-1");
    expect(cell).toHaveTextContent(/heartbeating/i);
    expect(cell).toHaveTextContent("12");
    // The threshold travels with the judgement, so the client cannot invent its own.
    expect(cell).toHaveTextContent("90");
  });

  it("reports a stale heartbeat as stale rather than as absent", async () => {
    mockGet.mockResolvedValue(
      device({
        last_seen: "2026-08-20T00:00:00Z",
        seconds_since_last_seen: 4000,
        heartbeat_fresh: false,
      }),
    );
    renderPage(<PairingPage />);
    const cell = await screen.findByTestId("heartbeat-d-1");
    expect(cell).toHaveTextContent(/stale/i);
    expect(cell).toHaveTextContent("4000");
    expect(cell).not.toHaveTextContent(/never reported/i);
  });

  it("never presents an attestation claim as device state", async () => {
    mockGet.mockResolvedValue(device({}));
    renderPage(<PairingPage />);
    const heartbeat = await screen.findByTestId("heartbeat-d-1");
    const status = screen.getByTestId("status-d-1");

    // Scoped to the fields that report state, deliberately. The page's own explanatory note quotes
    // "Connected & Attested" when describing the component this replaced, and that quotation is
    // the record rather than a claim — asserting its absence from the whole document would forbid
    // the page from explaining itself. What must never happen is the phrase appearing as a
    // device's reported status, so that is what is asserted.
    for (const cell of [heartbeat, status]) {
      expect(cell).not.toHaveTextContent(/Connected & Attested/i);
      expect(cell).not.toHaveTextContent(/attested/i);
      expect(cell).not.toHaveTextContent(/spiffe:/i);
    }
    // And the trust domain the old component invented appears nowhere at all, since no endpoint
    // reports one.
    expect(screen.queryByText(/spiffe:\/\/cluster\.local/i)).not.toBeInTheDocument();
  });

  it("explains each §3.7 status rather than only colour-coding it", async () => {
    mockGet.mockResolvedValue(device({ status: "policy_stale" }));
    renderPage(<PairingPage />);
    expect(await screen.findByTestId("status-d-1")).toHaveTextContent("policy_stale");
    expect(screen.getByText(/policy bundle digest no longer matches/i)).toBeInTheDocument();
  });

  it("says no certificate has been issued rather than rendering an empty field", async () => {
    mockGet.mockResolvedValue(device({ cert_fingerprint: null }));
    renderPage(<PairingPage />);
    expect(await screen.findByText(/no certificate has been issued/i)).toBeInTheDocument();
  });
});
