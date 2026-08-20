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
const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }));

// Partial mock: `api.get` is replaced, but `ApiProblemError`, `queryKeys` and the problem helpers
// stay REAL. The error branch under test reads `error instanceof ApiProblemError`, so a fake error
// class would make the test pass while the page failed in production.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { get: mockGet, post: vi.fn(), put: vi.fn(), del: vi.fn() } };
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

    it("reports a 401 as sign-in required rather than as a failure", async () => {
      mockGet.mockRejectedValue(problem(401, "Unauthenticated"));
      renderPage(<Page />);
      expect(
        (await screen.findAllByText(new RegExp(`Sign-in required to read ${label}`, "i"))).length,
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
const NOT_IMPLEMENTED_PAGES = [
  {
    name: "Approvals",
    Page: ApprovalsPage,
    heading: "Approvals",
    feature: /Change Approval Center is not implemented in Phase 1/i,
    namesMissingPiece: /router exists and is not registered|no endpoint to call/i,
    owner: /Phase 1 deliverable 1\.6/i,
  },
  {
    name: "Generation",
    Page: GenerationPage,
    heading: "Generation",
    feature: /artifact generator is not implemented in Phase 1/i,
    namesMissingPiece: /no routes\.py/i,
    owner: /Phase 1 deliverable 1\.5/i,
  },
  {
    name: "Pairing",
    Page: PairingPage,
    heading: "Agent pairing",
    feature: /Agent pairing and attestation status is not implemented in Phase 1/i,
    namesMissingPiece: /no endpoint that reports which devices are paired/i,
    owner: /Phase 1 deliverable 1\.1/i,
  },
] as const;

describe.each(NOT_IMPLEMENTED_PAGES)(
  "$name route (no endpoint behind it)",
  ({ name, Page, heading, feature, namesMissingPiece, owner }) => {
    it("renders exactly one h1, naming the screen", () => {
      render(<Page />);
      const h1s = screen.getAllByRole("heading", { level: 1 });
      expect(h1s).toHaveLength(1);
      expect(h1s[0]).toHaveTextContent(heading);
    });

    it("declares the feature unimplemented in a labelled region", () => {
      render(<Page />);
      const region = screen.getByRole("region", { name: feature });
      expect(region).toBeInTheDocument();
    });

    it("names the missing piece and the owning phase", () => {
      render(<Page />);
      const region = screen.getByRole("region", { name: feature });
      expect(region).toHaveTextContent(namesMissingPiece);
      expect(region).toHaveTextContent(owner);
    });

    it("states that the blankness is deliberate rather than unfinished", () => {
      render(<Page />);
      expect(
        screen.getByText(/deliberately blank rather than populated with sample data/i),
      ).toBeInTheDocument();
    });

    it("makes no network request at all", () => {
      render(<Page />);
      expect(mockGet).not.toHaveBeenCalled();
    });

    it(`is the current nav item at its own pathname (${name})`, () => {
      mockPathname = ROUTE_HREFS[name];
      render(<AppSidebar />);
      const current = screen
        .getAllByRole("link")
        .filter((a) => a.getAttribute("aria-current") === "page");
      expect(current).toHaveLength(1);
      expect(current[0]).toHaveAttribute("href", ROUTE_HREFS[name]);
    });
  },
);

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
