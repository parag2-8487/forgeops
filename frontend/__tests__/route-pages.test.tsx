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
const { mockGet, mockPost, mockPatch, mockPut, mockDelete, mockDeleteWith } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPatch: vi.fn(),
  mockPut: vi.fn(),
  mockDelete: vi.fn(),
  mockDeleteWith: vi.fn(),
}));

// Partial mock: `api.get` is replaced, but `ApiProblemError`, `queryKeys` and the problem helpers
// stay REAL. The error branch under test reads `error instanceof ApiProblemError`, so a fake error
// class would make the test pass while the page failed in production.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: mockGet,
      post: mockPost,
      // `patch` and `deleteWith` are new verbs, added for secret rotation, policy update and the two
      // destructive routes that require a reason in the body. Mocked here so a page that calls one
      // fails on an assertion rather than on `api.patch is not a function`.
      patch: mockPatch,
      put: mockPut,
      delete: mockDelete,
      deleteWith: mockDeleteWith,
      stream: vi.fn(),
    },
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
  mockPatch.mockReset();
  mockPut.mockReset();
  mockDelete.mockReset();
  mockDeleteWith.mockReset();
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
    usesPicker: false,
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
    usesPicker: false,
    Page: ProjectsPage,
    heading: "Projects",
    label: "projects",
    // `GET /projects` returns a page, and the screen also reads the tenant's tag vocabulary so the
    // tag filter can be a set of chips rather than a free-text box that silently matches nothing.
    respond: (path: string) =>
      path.startsWith("/projects/tags")
        ? ["eu", "prod"]
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
                archived_at: null,
                tags: ["prod"],
                favourite: false,
                indexed_file_count: 141,
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
    // Picks a project from `GET /projects` before its own request, so the harness must answer both.
    usesPicker: true,
    Page: ReadinessPage,
    heading: "Deployment readiness",
    label: "readiness report",
    respond: () => ({
      project_id: "00000000-0000-0000-0000-000000000001",
      score: 73,
      level: "Adequate",
      summary_report: "Scored seventy-three of one hundred.",
      recommendations: ["Add a health check"],
      // §1.4's SIX weighted categories. It was five — documentation, test coverage, CI config,
      // security policy, containerisation — which omitted three of §1.4's and scored two it does not
      // name. Test evidence is now a check inside CI/CD rather than a category of its own.
      categories: {
        containerization_score: 85,
        ci_config_score: 90,
        orchestration_score: 40,
        env_config_score: 70,
        security_policy_score: 50,
        iac_score: 30,
      },
      indexed: true,
      evaluated_paths: 141,
      checks: [
        {
          id: "dockerfile_exists",
          category: "containerization_score",
          passed: true,
          points: 20,
          max_points: 20,
          evidence: "Dockerfile",
          why_it_matters: "Without a Dockerfile there is no reproducible build.",
        },
      ],
    }),
    shows: ["Adequate", "Add a health check"],
    empty: null,
    emptyText: undefined,
  },
  {
    name: "Audit",
    usesPicker: false,
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
    usesPicker: false,
    Page: PoliciesPage,
    heading: "Policies",
    // The primary collection is now STORED POLICIES rather than templates. `GET /api/v1/policies`
    // did not exist, which is why this screen was a read-only wall of templates.
    label: "policies",
    respond: (path: string) =>
      path.startsWith("/policies/templates")
        ? [
            {
              id: "tpl-1",
              name: "Require staging first",
              description: "Blocks a production apply with no staging predecessor.",
              rego_rules: "package forgeops\ndefault allow = false",
              parameters: {},
            },
          ]
        : {
            policies: [
              {
                id: "pol-1",
                project_id: null,
                tenant_id: null,
                name: "No Friday deploys",
                engine: "rego",
                rego_rules: 'package forgeops.governance\ndefault decision = "deny"',
                enabled: true,
                template_id: "scheduling",
                created_at: "2026-08-20T00:00:00Z",
                updated_at: "2026-08-21T00:00:00Z",
              },
            ],
            next_cursor: null,
          },
    shows: ["No Friday deploys", "Require staging first"],
    empty: { policies: [], next_cursor: null },
    emptyText: /the chokepoint has nothing to evaluate/i,
  },
  {
    name: "Vault",
    // Picks a project from `GET /projects` before its own request, so the harness must answer both.
    usesPicker: true,
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
    // The empty case is inside `SecretVault` rather than in the page's `AsyncState`, because the add
    // form has to render even when there is nothing to list — a screen whose empty state replaces the
    // control that fills it is a dead end.
    empty: [],
    emptyText: /none registered/i,
  },
  {
    name: "Pairing",
    // Mints codes, and a code is scoped to a project — so this screen picks one too.
    usesPicker: true,
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

/**
 * The project list, always served, so a page driven by `ProjectPicker` can get past choosing one.
 *
 * `/readiness`, `/vault` and `/generation` now pick from real projects instead of defaulting to an
 * invented UUID. That removed a defect -- `/readiness` opened on a 403 every time, because the id it
 * defaulted to is never created -- but it means those pages make TWO requests, and a test that made
 * the first one fail would be testing the picker rather than the panel.
 *
 * So the list is answered separately from the request under test, and the panel's own outcome is
 * still whatever the test asked for.
 */
const PROJECT_LIST = {
  projects: [{ id: "11111111-1111-1111-1111-111111111111", name: "picker-fixture" }],
  next_cursor: null,
};

/**
 * Serve the project list, and route everything else to `handler`.
 *
 * Scoped by `usesPicker` rather than applied everywhere, because `/projects` fetches the project list
 * AS its own subject -- intercepting there would answer the request under test with a fixture and the
 * page's real payload would never arrive.
 */
function serving(handler: (path: string) => Promise<unknown>, usesPicker = false) {
  if (!usesPicker) return handler;
  return (path: string) =>
    path.startsWith("/projects?limit=") ? Promise.resolve(PROJECT_LIST) : handler(path);
}

describe.each(LIVE_PAGES)(
  "$name route",
  ({ name, Page, heading, label, respond, shows, empty, emptyText, usesPicker }) => {
    it("renders exactly one h1, naming the screen", async () => {
      mockGet.mockImplementation(serving(() => pending(), usesPicker));
      renderPage(<Page />);
      const h1s = screen.getAllByRole("heading", { level: 1 });
      expect(h1s).toHaveLength(1);
      expect(h1s[0]).toHaveTextContent(heading);
    });

    it("announces a polite loading status while the request is in flight", async () => {
      mockGet.mockImplementation(serving(() => pending(), usesPicker));
      renderPage(<Page />);
      // Awaited rather than read synchronously. A picker-driven page resolves its project list
      // first, so at the instant of render the only status is the picker's own -- a synchronous
      // assertion would be testing the wrong element and would pass or fail on timing.
      await waitFor(() => {
        const statuses = screen.getAllByRole("status");
        expect(
          statuses.some((n) => new RegExp(`Loading ${label}`, "i").test(n.textContent ?? "")),
        ).toBe(true);
      });
      // `role=status` with aria-live=polite is the accessible contract, not the text.
      const statuses = screen.getAllByRole("status");
      expect(statuses.length).toBeGreaterThan(0);
      expect(statuses[0]).toHaveAttribute("aria-live", "polite");
    });

    it("renders the payload it was given on success", async () => {
      mockGet.mockImplementation(
        serving((path: string) => Promise.resolve(respond(path)), usesPicker),
      );
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
      mockGet.mockImplementation(
        serving(() => Promise.reject(problem(401, "Unauthenticated")), usesPicker),
      );
      renderPage(<Page />);
      expect(
        (await screen.findAllByText(new RegExp(`Not authenticated to read ${label}`, "i"))).length,
      ).toBeGreaterThan(0);
      // A 401 is the designed behaviour, so it must NOT be announced as an alert.
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("renders a 500 as an alert carrying the server's own problem details", async () => {
      mockGet.mockImplementation(
        serving(
          () => Promise.reject(problem(500, "Internal Server Error", "the database is on fire")),
          usesPicker,
        ),
      );
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
      mockGet.mockImplementation(
        serving((path: string) => Promise.resolve(respond(path)), usesPicker),
      );
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
        // Path-aware, because a page that reads two collections has two shapes to empty. The Policies
        // screen reads the stored policies AND the templates, and answering both with `{policies: []}`
        // handed the template list an object to `.map` over — a crash in the test harness that looked
        // like a defect in the page.
        mockGet.mockImplementation(
          serving((path: string) => Promise.resolve(emptyFor(name, path, empty)), usesPicker),
        );
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
 * The empty response for one request of a page that makes several.
 *
 * Only the Policies screen needs the distinction today: its subject is the stored-policy list, and it
 * also reads the immutable template list, which is an ARRAY. Emptying both with the page shape gave
 * the template list an object, and `.map` on an object throws during render — so the harness crashed
 * the page it was testing and the failure read as a defect in the page.
 */
function emptyFor(name: string, path: string, empty: unknown): unknown {
  if (name === "Policies" && path.startsWith("/policies/templates")) return [];
  return empty;
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

  /**
   * Serve the list and the detail.
   *
   * `queue` is the status the list answers FOR, because the screen now has four queues and asks for
   * one by name. A mock that answered every status with the same row would let a test pass while the
   * filter was ignored — and the filter being ignored is precisely how an applied change set would
   * appear in the pending queue.
   */
  function serve(overrides: Record<string, unknown> = {}, queue = "pending_approval") {
    mockGet.mockImplementation((path: string) => {
      if (path.startsWith("/approvals?")) {
        return Promise.resolve(
          path.includes(`status=${queue}`)
            ? { change_sets: [{ ...CHANGE_SET, ...overrides }], next_cursor: null }
            : { change_sets: [], next_cursor: null },
        );
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
    serve({ status: "rejected" }, "rejected");
    renderPage(<ApprovalsPage />);
    await userEvent.click(await screen.findByTestId("queue-rejected"));
    await userEvent.click(await screen.findByRole("button", { name: /cs-1/ }));
    await screen.findByRole("region", { name: /diff for/i });

    // Not offered and refused by the server: not offered at all.
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    // And no revert either: §3.6's `applied → reverted` edge starts from `applied`, not from
    // `rejected`, so a revert control here would be a button whose only outcome is a refusal.
    expect(screen.queryByTestId("revert-change-set")).not.toBeInTheDocument();
    expect(screen.getByText(/permits a decision only from/i)).toBeInTheDocument();
  });

  /**
   * REVERT WAS UNREACHABLE FOR TWO REASONS, and both are asserted here.
   *
   * The mutation's type was narrowed to `"approve" | "reject"`, so the endpoint could not be named.
   * And the list was filtered to `pending_approval` only, so an APPLIED change set never appeared on
   * the screen at all — fixing the type alone would have produced a control with no row to attach it
   * to.
   */
  it("offers revert only from the applied queue, and posts to the revert endpoint", async () => {
    serve({ status: "applied", applied_at: "2026-08-21T05:00:00Z" }, "applied");
    mockPost.mockResolvedValue({
      change_set_id: "cs-1",
      status: "reverted",
      outcome: "allowed",
      audit_seq: 91,
      approval_id: null,
      blast_radius_score: 4,
      blast_radius_verdict: "low",
      reverse_change_set_id: "cs-2",
      command_delivered: true,
    });
    renderPage(<ApprovalsPage />);

    await userEvent.click(await screen.findByTestId("queue-applied"));
    await userEvent.click(await screen.findByRole("button", { name: /cs-1/ }));

    await userEvent.click(await screen.findByTestId("revert-change-set"));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/approvals/cs-1/revert", undefined));

    const outcome = await screen.findByTestId("revert-outcome");
    expect(outcome).toHaveTextContent("cs-2");
    // Whether a signed command was delivered, not the envelope: it carries an authority token and a
    // nonce, which a reviewer must not be handed.
    expect(outcome).toHaveTextContent(/command delivered to an agent/i);
    expect(outcome).toHaveTextContent("yes");
  });

  it("presents an escalated revert as an outcome rather than as an error", async () => {
    serve({ status: "applied", applied_at: "2026-08-21T05:00:00Z" }, "applied");
    // `approval-required` is registered at status 202, which is inside the 2xx range — so `fetch`
    // reports `res.ok` and the client returns the PROBLEM DOCUMENT as a success body. Treating that as
    // a failure would report the design working as the design broken: §3.6's `applied → reverted` edge
    // is only reachable because a blocked revert escalates.
    mockPost.mockResolvedValue({
      type: "https://errors.forgeops.dev/approval-required",
      title: "Approval required",
      status: 202,
      detail: "The reverse change set is held for review.",
    });
    renderPage(<ApprovalsPage />);

    await userEvent.click(await screen.findByTestId("queue-applied"));
    await userEvent.click(await screen.findByRole("button", { name: /cs-1/ }));
    await userEvent.click(await screen.findByTestId("revert-change-set"));

    const escalated = await screen.findByTestId("revert-escalated");
    expect(escalated).toHaveTextContent(/escalated to approval/i);
    expect(escalated).toHaveTextContent(/not refused/i);
    expect(escalated).toHaveTextContent("The reverse change set is held for review.");
    // Announced as a status, not an alert: nothing went wrong.
    expect(escalated).toHaveAttribute("role", "status");
  });
});

describe("Generation gates the wizard on a usable project id", () => {
  it("renders exactly one h1", () => {
    renderPage(<GenerationPage />);
    const h1s = screen.getAllByRole("heading", { level: 1 });
    expect(h1s).toHaveLength(1);
    expect(h1s[0]).toHaveTextContent("Generation");
  });

  it("does not offer the generator when the tenant has no projects", async () => {
    mockGet.mockImplementation(() => Promise.resolve({ projects: [], next_cursor: null }));
    renderPage(<GenerationPage />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    // A button that cannot succeed is worse than one that is not there: the run has to be
    // attributed to a project to be submitted as a change set.
    expect(screen.queryByRole("button", { name: /generate artifacts/i })).not.toBeInTheDocument();
    expect(await screen.findByText(/no projects yet/i)).toBeInTheDocument();
  });

  it("offers the generator once a real project is selected", async () => {
    // Chosen from `GET /projects`, not typed. The field used to be a free-text UUID box defaulting
    // to an invented id, which is why `/readiness` opened on a 403 every time.
    mockGet.mockImplementation((path: string) =>
      path.startsWith("/projects?limit=")
        ? Promise.resolve({
            projects: [{ id: "11111111-1111-1111-1111-111111111111", name: "picker-fixture" }],
            next_cursor: null,
          })
        : Promise.reject(new Error(`unexpected GET ${path}`)),
    );
    renderPage(<GenerationPage />);
    expect(await screen.findByRole("button", { name: /generate artifacts/i })).toBeInTheDocument();
  });

  it("submits nothing merely from being rendered", async () => {
    mockGet.mockImplementation(() => Promise.resolve({ projects: [], next_cursor: null }));
    renderPage(<GenerationPage />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    // Reading the project list is expected; POSTing a run is not.
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
describe("the project picker drives the readiness request", () => {
  const PROJECTS = {
    projects: [
      { id: "11111111-1111-1111-1111-111111111111", name: "first" },
      { id: "22222222-2222-2222-2222-222222222222", name: "second" },
    ],
    next_cursor: null,
  };

  function serveReadiness() {
    mockGet.mockImplementation((path: string) => {
      if (path.startsWith("/projects?limit=")) return Promise.resolve(PROJECTS);
      return Promise.resolve({
        project_id: "x",
        score: 1,
        level: "Low",
        summary_report: "s",
        recommendations: [],
        categories: { containerization_score: 1 },
        indexed: true,
        evaluated_paths: 1,
        checks: [],
      });
    });
  }

  it("requests the first real project rather than an invented id", async () => {
    serveReadiness();
    renderPage(<ReadinessPage />);
    // THE DEFECT THIS REPLACES: the field defaulted to `00000000-…-0001`, which is never created, so
    // the screen opened on a 403 every single time. The response was correct -- §4.2 makes "may not
    // read" and "does not exist" identical -- but a default that guarantees it teaches the operator
    // to ignore the error rather than read it.
    await waitFor(() => {
      const requested = mockGet.mock.calls.map((c) => String(c[0]));
      expect(requested.some((p) => p.includes("11111111-1111-1111-1111-111111111111"))).toBe(true);
    });
    const requested = mockGet.mock.calls.map((c) => String(c[0]));
    expect(requested.some((p) => p.includes("00000000-0000-0000-0000-000000000001"))).toBe(false);
  });

  it("refetches when a different project is selected", async () => {
    serveReadiness();
    renderPage(<ReadinessPage />);
    const select = await screen.findByLabelText("Project");
    expect(select).toHaveAttribute("id", "readiness-project");

    await userEvent.selectOptions(select, "22222222-2222-2222-2222-222222222222");

    await waitFor(() => {
      const requested = mockGet.mock.calls.map((c) => String(c[0]));
      expect(
        requested.some(
          (p) => p.includes("22222222-2222-2222-2222-222222222222") && /\/readiness$/.test(p),
        ),
      ).toBe(true);
    });
  });

  it("offers no id box when the tenant has no projects", async () => {
    mockGet.mockImplementation(() => Promise.resolve({ projects: [], next_cursor: null }));
    renderPage(<ReadinessPage />);
    expect(await screen.findByText(/no projects yet/i)).toBeInTheDocument();
    // Deliberately no free-text field: typing an id that does not exist produces a 403 that reads
    // as a permissions fault.
    expect(document.querySelector("input")).toBeNull();
  });
});

describe("Readiness category breakdown", () => {
  /** §1.4's six weighted categories, which is what the engine now computes. */
  const categories = {
    containerization_score: 85,
    ci_config_score: 90,
    orchestration_score: 40,
    env_config_score: 70,
    security_policy_score: 50,
    iac_score: 30,
  };

  const CHECKS = [
    {
      id: "dockerfile_exists",
      category: "containerization_score",
      passed: true,
      points: 20,
      max_points: 20,
      evidence: "Dockerfile",
      why_it_matters: "Without a Dockerfile there is no reproducible build of this service.",
    },
    {
      id: "dockerfile_non_root",
      category: "containerization_score",
      passed: false,
      points: 0,
      max_points: 10,
      evidence: "no USER directive found in Dockerfile",
      why_it_matters: "A container running as root turns a process compromise into a host one.",
    },
  ];

  function serve(report: Record<string, unknown>) {
    mockGet.mockImplementation((path: string) =>
      path.startsWith("/projects?limit=")
        ? Promise.resolve({
            projects: [{ id: "11111111-1111-1111-1111-111111111111", name: "picker-fixture" }],
            next_cursor: null,
          })
        : Promise.resolve(report),
    );
  }

  it("renders one bar per category the engine computed, labelled readably", async () => {
    serve({
      project_id: "x",
      score: 70,
      level: "Adequate",
      summary_report: "s",
      recommendations: [],
      categories,
      indexed: true,
      evaluated_paths: 12,
      checks: [],
    });
    renderPage(<ReadinessPage />);
    // `containerization_score` becomes `Containerization`. The chart used to render a single bar
    // called "Overall", because the breakdown was computed server-side and dropped by the response
    // model.
    for (const label of [
      "Containerization",
      "Ci Config",
      "Orchestration",
      "Env Config",
      "Security Policy",
      "Iac",
    ]) {
      expect((await screen.findAllByText(new RegExp(label, "i"))).length).toBeGreaterThan(0);
    }
    expect(screen.queryByText("Overall")).not.toBeInTheDocument();
  });

  it("renders no category bars when the engine reported none, rather than inventing five", async () => {
    serve({
      project_id: "x",
      score: 0,
      level: "blocked",
      summary_report: "s",
      recommendations: [],
      categories: {},
      indexed: true,
      evaluated_paths: 3,
      checks: [],
    });
    renderPage(<ReadinessPage />);
    expect((await screen.findAllByText(/blocked/i)).length).toBeGreaterThan(0);
    expect(screen.queryByText("Documentation")).not.toBeInTheDocument();
  });

  /**
   * §1.4 "Frontend: Detailed category breakdown with expandable items", and PRD FR-19's
   * "why it matters".
   *
   * The six numbers on the radar chart cannot be acted on: "Security 40" says there is a problem and
   * not what it is, and a category at 40 with no visible evidence is indistinguishable from a bug in
   * the scorer. So the assertion is that the checks are HIDDEN until asked for and then carry the
   * indexed path and the reason — not merely that a panel exists.
   */
  it("keeps the checks collapsed until the category is expanded", async () => {
    serve({
      project_id: "x",
      score: 70,
      level: "Adequate",
      summary_report: "s",
      recommendations: [],
      categories,
      indexed: true,
      evaluated_paths: 12,
      checks: CHECKS,
    });
    renderPage(<ReadinessPage />);

    const toggle = await screen.findByTestId("category-toggle-containerization_score");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("check-dockerfile_exists")).not.toBeInTheDocument();

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    // `aria-controls` names the region the trigger reveals, so the relationship is announced rather
    // than only visible.
    expect(toggle).toHaveAttribute("aria-controls", "readiness-checks-containerization_score");
  });

  it("shows the indexed evidence and why the check matters, not only a pass or fail", async () => {
    serve({
      project_id: "x",
      score: 70,
      level: "Adequate",
      summary_report: "s",
      recommendations: [],
      categories,
      indexed: true,
      evaluated_paths: 12,
      checks: CHECKS,
    });
    renderPage(<ReadinessPage />);
    await userEvent.click(await screen.findByTestId("category-toggle-containerization_score"));

    const failing = screen.getByTestId("check-dockerfile_non_root");
    // The word, not the colour: a red dot is unavailable to a colour-blind reader and to a screen
    // reader alike.
    expect(failing).toHaveTextContent("Fail");
    expect(failing).toHaveTextContent("no USER directive found in Dockerfile");
    expect(failing).toHaveTextContent(/turns a process compromise into a host one/i);
    expect(failing).toHaveTextContent("0 of 10 points");
  });

  it("says there is nothing to break down for a project that was never scanned", async () => {
    serve({
      project_id: "x",
      score: 0,
      level: "blocked",
      summary_report: "no indexed files",
      recommendations: ["Run an agent scan"],
      categories,
      indexed: false,
      evaluated_paths: 0,
      checks: [],
    });
    renderPage(<ReadinessPage />);
    // Zero because nothing was measured, not because everything failed — and the provenance panel
    // says so rather than showing a zero that reads as a measurement.
    expect(await screen.findByTestId("readiness-provenance")).toHaveTextContent(/never scanned/i);
    expect(screen.getByText(/no check has been evaluated/i)).toBeInTheDocument();
    expect(screen.queryByTestId("category-toggle-containerization_score")).not.toBeInTheDocument();
  });
});

/**
 * The home page no longer enumerates which routes are "live".
 *
 * IT USED TO, AND IT WAS WRONG. The list said approvals and generation had "mounted, authenticated
 * backend surfaces ... but their reviewer and wizard screens are not built", long after both were
 * built. A list whose every entry says the same thing carries no information and is one edit from
 * being wrong again, so what the page states instead is the part a reader cannot check from the
 * navigation: which facts the app can observe and which it cannot.
 */
describe("Home states what the app cannot observe", () => {
  it("names the facts no endpoint reports, rather than claiming completeness", async () => {
    mockGet.mockResolvedValue({ status: "ok", version: "1", commit: "c" });
    renderPage(<HomePage />);
    expect(await screen.findByText(/what this app can and cannot observe/i)).toBeInTheDocument();
    // The four honest gaps, each of which a screen elsewhere declines to fill in.
    expect(screen.getByText(/whether a policy bundle is published/i)).toBeInTheDocument();
    expect(screen.getByText(/whether an agent is attested/i)).toBeInTheDocument();
    expect(screen.getByText(/a readiness score on the project list/i)).toBeInTheDocument();
    expect(screen.getByText(/whether a model endpoint is up right now/i)).toBeInTheDocument();
  });

  it("points a new installation at the ordered path rather than at a feature list", async () => {
    mockGet.mockResolvedValue({ status: "ok", version: "1", commit: "c" });
    renderPage(<HomePage />);
    const link = await screen.findByRole("link", { name: /eight-step path/i });
    expect(link).toHaveAttribute("href", "/onboarding");
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
