// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The five capabilities that had tested endpoints and no UI at all, plus the path that orders them.
 *
 * `POST /agents/pairing-codes`, `DELETE /agents/{id}`, `GET /agents/devices/{id}`,
 * `GET /audit/verify`, `GET /ai/tiers` and `POST /analysis/plan` were every one of them served,
 * tested, and called by nothing. Two of them are role-gated, which is why the role plumbing had to
 * come first: without it the controls were offered to everyone and a viewer's first feedback was a
 * 403 that §4.2 makes deliberately uninformative.
 *
 * The onboarding path is tested last because it is the integration: it asserts which steps are checked
 * against a real endpoint and — more importantly — that the ones which cannot be checked say so rather
 * than showing a tick nothing justified.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockGet, mockPost, mockDeleteWith } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockDeleteWith: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: mockGet,
      post: mockPost,
      patch: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
      deleteWith: mockDeleteWith,
      stream: vi.fn(),
    },
  };
});

vi.mock("next/navigation", () => ({ usePathname: () => "/" }));

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

import PairingPage from "@/app/(shell)/pairing/page";
import AuditPage from "@/app/(shell)/audit/page";
import ModelTiersPage from "@/app/(shell)/models/page";
import PlanAnalysisPage from "@/app/(shell)/analysis/page";
import OnboardingPage from "@/app/(shell)/onboarding/page";
import { ApiProblemError } from "@/lib/api";
import { setSession, clearSession, type Role } from "@/lib/session";

function renderPage(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function signInAs(role: Role | null) {
  setSession("token", { subject: "s", sessionId: null, role });
}

const PROJECTS = {
  projects: [{ id: "11111111-1111-1111-1111-111111111111", name: "picker-fixture" }],
  next_cursor: null,
};

/**
 * One observed device.
 *
 * `status` is widened to the union rather than inferred as the literal `"active"`, so a test can build a
 * pending or revoked device by spreading this without the type narrowing fighting it.
 */
const DEVICE: {
  id: string;
  project_id: string;
  status: "pending" | "active" | "policy_stale" | "revoked" | "abandoned";
  agent_version: string;
  platform: string;
  cert_serial: string | null;
  cert_fingerprint: string | null;
  cert_not_after: string | null;
  last_seq: number;
  last_seen: string | null;
  pairing_expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
  seconds_since_last_seen: number | null;
  heartbeat_fresh: boolean | null;
  heartbeat_timeout_seconds: number;
} = {
  id: "d-1",
  project_id: "11111111-1111-1111-1111-111111111111",
  status: "active",
  agent_version: "0.4.1",
  platform: "linux/amd64",
  cert_serial: "0A1B",
  cert_fingerprint: "sha256:ab:cd",
  cert_not_after: "2026-12-01T00:00:00Z",
  last_seq: 42,
  last_seen: "2026-08-28T04:00:00Z",
  pairing_expires_at: null,
  revoked_at: null,
  created_at: "2026-08-20T00:00:00Z",
  seconds_since_last_seen: 12,
  heartbeat_fresh: true,
  heartbeat_timeout_seconds: 90,
};

beforeEach(() => {
  for (const m of [mockGet, mockPost, mockDeleteWith]) m.mockReset();
  clearSession();
});
afterEach(() => cleanup());

// ── C.3: mint a pairing code ─────────────────────────────────────────────────────────────────────

describe("minting a pairing code", () => {
  function serve(devices = [DEVICE]) {
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/projects?limit=")) return Promise.resolve(PROJECTS);
      if (path.startsWith("/agents/devices?"))
        return Promise.resolve({ devices, next_cursor: null });
      if (path.startsWith("/agents/devices/")) return Promise.resolve(DEVICE);
      return Promise.resolve(null);
    });
  }

  it("posts the chosen project id", async () => {
    signInAs("admin");
    serve();
    mockPost.mockResolvedValue({
      code: "K7X2QP",
      device_id: "d-2",
      expires_at: "2026-08-28T10:05:00Z",
    });
    renderPage(<PairingPage />);

    await userEvent.click(await screen.findByTestId("mint-code"));
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/agents/pairing-codes", {
        project_id: "11111111-1111-1111-1111-111111111111",
      }),
    );
  });

  it("shows the code once, with a live countdown rather than a raw timestamp, and says it cannot be recovered", async () => {
    signInAs("admin");
    serve();
    mockPost.mockResolvedValue({
      code: "K7X2QP",
      device_id: "d-2",
      expires_at: "2026-08-28T10:05:00Z",
    });
    renderPage(<PairingPage />);
    await userEvent.click(await screen.findByTestId("mint-code"));

    const panel = await screen.findByTestId("pairing-code");
    expect(screen.getByTestId("pairing-code-value")).toHaveTextContent("K7X2QP");
    // THE RAW TIMESTAMP IS GONE, DELIBERATELY. It used to be printed as `expires_at` verbatim, which
    // left the user to subtract two times in their head to learn how long they had — and on a first
    // run the code always expired before it could be used. It is replaced by a live countdown, or by
    // an expired notice with a re-mint button, and this fixture's expiry is in the past.
    expect(panel).not.toHaveTextContent("2026-08-28T10:05:00Z");
    expect(screen.getByTestId("code-expired")).toBeInTheDocument();
    expect(screen.getByTestId("remint-code")).toBeInTheDocument();
    expect(panel).toHaveTextContent(/cannot be extended/i);
    // The code exists in the clear in exactly one place: this response body. Only its HMAC is stored,
    // so there is no endpoint that could show it again — a "reveal" control would have nothing to
    // reveal. Saying so is what stops someone closing the panel expecting to find it later.
    expect(panel).toHaveTextContent(/not recoverable/i);
    expect(panel).toHaveTextContent(/in no log, no\s+audit record and no database column/i);
    // Announced assertively, because it is shown once.
    expect(panel).toHaveAttribute("role", "alert");
    expect(panel).toHaveAttribute("aria-live", "assertive");
  });

  it("does not put the code on the clipboard by itself", async () => {
    signInAs("admin");
    serve();
    const writeText = vi.fn();
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    mockPost.mockResolvedValue({ code: "K7X2QP", device_id: "d-2", expires_at: "x" });
    renderPage(<PairingPage />);
    await userEvent.click(await screen.findByTestId("mint-code"));
    await screen.findByTestId("pairing-code");
    // A credential silently placed on the clipboard is a credential in every clipboard-reading
    // application on the machine, and the operator did not ask for that.
    expect(writeText).not.toHaveBeenCalled();
  });

  it("is not offered to a viewer, with the requirement stated", async () => {
    signInAs("viewer");
    serve();
    renderPage(<PairingPage />);
    expect(await screen.findByText(/requires the admin or developer role/i)).toBeInTheDocument();
    expect(screen.queryByTestId("mint-code")).not.toBeInTheDocument();
  });

  it("is offered to a developer, which the backend permits", async () => {
    signInAs("developer");
    serve();
    renderPage(<PairingPage />);
    // `require_role(ADMIN, DEVELOPER)` on the route, so hiding it from a developer would remove a
    // capability they have.
    expect(await screen.findByTestId("mint-code")).toBeInTheDocument();
  });

  it("is offered when the role is unknown, rather than hidden on a guess", async () => {
    signInAs(null);
    serve();
    renderPage(<PairingPage />);
    // Unknown means the server did not say. Hiding the control would leave someone entitled to it with
    // no route to the feature at all; offering it costs one 403 that the page renders properly.
    expect(await screen.findByTestId("mint-code")).toBeInTheDocument();
  });
});

// ── C.4, C.5: revoke a device, read one device ───────────────────────────────────────────────────

describe("revoking a device", () => {
  function serve() {
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/projects?limit=")) return Promise.resolve(PROJECTS);
      if (path.startsWith("/agents/devices?")) {
        return Promise.resolve({ devices: [DEVICE], next_cursor: null });
      }
      if (path.startsWith("/agents/devices/")) return Promise.resolve(DEVICE);
      return Promise.resolve(null);
    });
  }

  it("is offered only to an admin", async () => {
    signInAs("developer");
    serve();
    renderPage(<PairingPage />);
    await screen.findByTestId("status-d-1");
    expect(screen.queryByTestId("revoke-d-1")).not.toBeInTheDocument();
    // Stated once below the list rather than as a greyed button on every row: a permanently disabled
    // control on each row is visual noise that teaches people to ignore disabled states.
    expect(screen.getByText(/requires the admin role/i)).toBeInTheDocument();
  });

  it("sends the reason in the request body", async () => {
    signInAs("admin");
    serve();
    mockDeleteWith.mockResolvedValue(undefined);
    renderPage(<PairingPage />);

    await userEvent.click(await screen.findByTestId("revoke-d-1"));
    await userEvent.type(screen.getByLabelText("Reason"), "laptop stolen");
    await userEvent.click(screen.getByTestId("confirm-revoke"));

    // A body, not a query parameter: a reason in the query string lands in access logs and browser
    // history.
    await waitFor(() =>
      expect(mockDeleteWith).toHaveBeenCalledWith("/agents/d-1", { reason: "laptop stolen" }),
    );
  });

  it("will not submit without a reason", async () => {
    signInAs("admin");
    serve();
    renderPage(<PairingPage />);
    await userEvent.click(await screen.findByTestId("revoke-d-1"));
    expect(screen.getByTestId("confirm-revoke")).toBeDisabled();
  });

  it("says revocation takes effect per message rather than per connection", async () => {
    signInAs("admin");
    serve();
    renderPage(<PairingPage />);
    await userEvent.click(await screen.findByTestId("revoke-d-1"));
    // An agent mid-session is cut off at its next envelope, not at its next reconnect. An operator
    // revoking a stolen laptop needs to know which.
    expect(
      screen.getByText(/checked per message rather than once per connection/i),
    ).toBeInTheDocument();
  });

  it("is not offered for a device that is already revoked", async () => {
    signInAs("admin");
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/projects?limit=")) return Promise.resolve(PROJECTS);
      if (path.startsWith("/agents/devices?")) {
        return Promise.resolve({
          devices: [{ ...DEVICE, status: "revoked", revoked_at: "2026-08-27T00:00:00Z" }],
          next_cursor: null,
        });
      }
      return Promise.resolve(null);
    });
    renderPage(<PairingPage />);
    await screen.findByTestId("status-d-1");
    expect(screen.queryByTestId("revoke-d-1")).not.toBeInTheDocument();
  });
});

describe("reading one device", () => {
  it("re-reads it from the detail route rather than waiting for the next poll", async () => {
    signInAs("admin");
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/projects?limit=")) return Promise.resolve(PROJECTS);
      if (path.startsWith("/agents/devices?")) {
        return Promise.resolve({ devices: [DEVICE], next_cursor: null });
      }
      if (path === "/agents/devices/d-1") return Promise.resolve(DEVICE);
      return Promise.resolve(null);
    });
    renderPage(<PairingPage />);

    await userEvent.click(await screen.findByTestId("device-detail-d-1"));
    // `queryKeys.devices.detail` existed and was unused, and so was the route. The list is polled every
    // fifteen seconds; after a revoke or a pairing an operator wants ONE device's state now.
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/agents/devices/d-1"));
    const panel = await screen.findByTestId("device-detail-panel-d-1");
    expect(panel).toHaveTextContent("0A1B");
  });

  it("says which credential columns are deliberately absent", async () => {
    signInAs("admin");
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/projects?limit=")) return Promise.resolve(PROJECTS);
      if (path.startsWith("/agents/devices?")) {
        return Promise.resolve({ devices: [DEVICE], next_cursor: null });
      }
      return Promise.resolve(DEVICE);
    });
    renderPage(<PairingPage />);
    await userEvent.click(await screen.findByTestId("device-detail-d-1"));
    expect(
      await screen.findByText(/would turn “list my\s+devices” into credential exfiltration/i),
    ).toBeInTheDocument();
  });
});

// ── C.7: audit chain verification ────────────────────────────────────────────────────────────────

describe("verifying the audit chain", () => {
  function serveEvents() {
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/audit/events"))
        return Promise.resolve({ events: [], next_cursor: null });
      if (path.startsWith("/audit/verify")) {
        return Promise.resolve({
          ok: true,
          tenant_id: null,
          from_seq: 0,
          rows_checked: 91,
          divergence: null,
        });
      }
      return Promise.resolve(null);
    });
  }

  it("is not offered to a non-admin", async () => {
    signInAs("developer");
    serveEvents();
    renderPage(<AuditPage />);
    expect(await screen.findByText(/requires the admin role/i)).toBeInTheDocument();
    expect(screen.queryByTestId("verify-chain")).not.toBeInTheDocument();
  });

  it("makes no verification request until asked", async () => {
    signInAs("admin");
    serveEvents();
    renderPage(<AuditPage />);
    await screen.findByTestId("verify-chain");
    // The operation reads every row from `since_seq`, so running it on page load would make the
    // database everybody's problem the moment somebody opened the audit screen.
    expect(mockGet.mock.calls.every((c) => !String(c[0]).startsWith("/audit/verify"))).toBe(true);
  });

  it("reports an intact chain with the number of rows it actually recomputed", async () => {
    signInAs("admin");
    serveEvents();
    renderPage(<AuditPage />);
    await userEvent.click(await screen.findByTestId("verify-chain"));

    const panel = await screen.findByTestId("verification-ok");
    expect(panel).toHaveTextContent(/the chain is intact/i);
    expect(panel).toHaveTextContent("91");
    expect(panel).toHaveAttribute("role", "status");
  });

  it("refuses to call zero rows checked evidence of integrity", async () => {
    signInAs("admin");
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/audit/events"))
        return Promise.resolve({ events: [], next_cursor: null });
      return Promise.resolve({
        ok: true,
        tenant_id: null,
        from_seq: 0,
        rows_checked: 0,
        divergence: null,
      });
    });
    renderPage(<AuditPage />);
    await userEvent.click(await screen.findByTestId("verify-chain"));
    // `ok: true` over an empty range is vacuously true. Presenting it as a clean bill of health is the
    // exact shape of a gate that cannot fail.
    expect(await screen.findByText(/nothing was checked/i)).toBeInTheDocument();
  });

  it("presents a divergence as a finding, not as a broken verifier", async () => {
    signInAs("admin");
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/audit/events"))
        return Promise.resolve({ events: [], next_cursor: null });
      return Promise.resolve({
        ok: false,
        tenant_id: null,
        from_seq: 0,
        rows_checked: 40,
        divergence: {
          seq: 37,
          kind: "hash_mismatch",
          detail: "recomputed hash differs from the stored value",
          expected_hash: "aa",
          stored_hash: "bb",
        },
      });
    });
    renderPage(<AuditPage />);
    await userEvent.click(await screen.findByTestId("verify-chain"));

    const panel = await screen.findByTestId("verification-divergence");
    expect(panel).toHaveTextContent("37");
    expect(panel).toHaveTextContent("hash_mismatch");
    // The endpoint answers 200 with ok:false rather than 5xx, so that "the chain is broken" and "the
    // verifier is broken" stay distinguishable. This panel keeps that distinction, and says what a
    // divergence implies given the table refuses UPDATE and DELETE.
    expect(panel).toHaveAttribute("role", "alert");
    expect(panel).toHaveTextContent(/modified the database directly/i);
    expect(panel).toHaveTextContent(/treat it as an incident/i);
  });

  it("renders a refusal from the endpoint through the problem path", async () => {
    signInAs("admin");
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/audit/events"))
        return Promise.resolve({ events: [], next_cursor: null });
      return Promise.reject(
        new ApiProblemError({
          type: "https://errors.forgeops.dev/forbidden",
          title: "Forbidden",
          status: 403,
        }),
      );
    });
    renderPage(<AuditPage />);
    await userEvent.click(await screen.findByTestId("verify-chain"));
    expect(await screen.findByTestId("governance-refusal")).toBeInTheDocument();
  });
});

// ── C.10: model tier health ──────────────────────────────────────────────────────────────────────

describe("model tier health", () => {
  const TIERS = {
    tiers: [
      {
        name: "self_hosted",
        primary_endpoint: "qwen3-coder-next",
        primary_protocol: "openai",
        available: true,
        breaker_state: "closed",
      },
      {
        name: "high_coding",
        primary_endpoint: "gpt-5.6-sol",
        primary_protocol: "openai",
        available: false,
        breaker_state: "open",
      },
    ],
  };

  it("reports each tier's endpoint, availability and breaker state", async () => {
    mockGet.mockResolvedValue(TIERS);
    renderPage(<ModelTiersPage />);

    expect(await screen.findByTestId("tier-self_hosted")).toBeInTheDocument();
    expect(screen.getByTestId("availability-high_coding")).toHaveTextContent("unavailable");
    expect(screen.getByTestId("breaker-high_coding")).toHaveTextContent("open");
  });

  it("explains what each breaker state means for the next request", async () => {
    mockGet.mockResolvedValue(TIERS);
    renderPage(<ModelTiersPage />);
    // The lifecycle is the reason this screen exists: a generation that silently fell back to a
    // secondary endpoint or a safe template looked identical to one that did not.
    expect(await screen.findByText(/no connection is attempted at all/i)).toBeInTheDocument();
    expect(screen.getByText(/five inside thirty seconds opens it/i)).toBeInTheDocument();
  });

  it("describes an unknown breaker state without inventing a meaning", async () => {
    mockGet.mockResolvedValue({
      tiers: [{ ...TIERS.tiers[0], breaker_state: "quarantined" }],
    });
    renderPage(<ModelTiersPage />);
    expect(await screen.findByTestId("breaker-self_hosted")).toHaveTextContent("quarantined");
    expect(screen.getByText(/no description here/i)).toBeInTheDocument();
  });

  it("says availability is a last observation rather than a probe", async () => {
    mockGet.mockResolvedValue(TIERS);
    renderPage(<ModelTiersPage />);
    // A page that probed every configured model on load would put load on every vendor every time
    // somebody glanced at a dashboard.
    expect(
      await screen.findByText(/loading\s+this page does not test any endpoint/i),
    ).toBeInTheDocument();
  });

  it("states the limit that only self-hosted endpoints have served a live call", async () => {
    mockGet.mockResolvedValue(TIERS);
    renderPage(<ModelTiersPage />);
    expect(
      await screen.findByText(/a tier can appear here with an endpoint it has never/i),
    ).toBeInTheDocument();
  });

  it("says an empty registry means generation has nothing to route to", async () => {
    mockGet.mockResolvedValue({ tiers: [] });
    renderPage(<ModelTiersPage />);
    expect(
      await screen.findByText(/an empty registry means generation has nothing/i),
    ).toBeInTheDocument();
  });
});

// ── C.9: plan analysis ───────────────────────────────────────────────────────────────────────────

describe("plan analysis", () => {
  it("posts the parsed plan document", async () => {
    mockPost.mockResolvedValue({
      findings: [],
      blast_radius: null,
      verdict: "allow",
      approval_decision: null,
    });
    renderPage(<PlanAnalysisPage />);

    await userEvent.click(screen.getByTestId("analyse-plan"));
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith(
        "/analysis/plan",
        expect.objectContaining({ plan: expect.objectContaining({ format_version: "1.2" }) }),
      ),
    );
  });

  it("reports a malformed plan itself rather than sending it", async () => {
    renderPage(<PlanAnalysisPage />);
    const box = screen.getByTestId("plan-json");
    await userEvent.clear(box);
    await userEvent.type(box, "{{nope");

    await userEvent.click(screen.getByTestId("analyse-plan"));
    expect(await screen.findByTestId("plan-local-error")).toHaveTextContent(/not valid JSON/i);
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("explains the verdict in terms of what the chokepoint would do", async () => {
    mockPost.mockResolvedValue({
      findings: [],
      blast_radius: {
        score: 71,
        destructive_count: 3,
        affected_resources: 12,
        stateful_deletions: ["aws_db_instance.main"],
        verdict: "block",
      },
      verdict: "block",
      approval_decision: "block",
    });
    renderPage(<PlanAnalysisPage />);
    await userEvent.click(screen.getByTestId("analyse-plan"));

    expect(await screen.findByTestId("plan-verdict")).toHaveTextContent("block");
    expect(screen.getByText(/refuse this rather than queue it/i)).toBeInTheDocument();
    expect(screen.getByTestId("blast-score")).toHaveTextContent("71");
  });

  it("names stateful deletions, and says why they are weighted differently", async () => {
    mockPost.mockResolvedValue({
      findings: [],
      blast_radius: {
        score: 71,
        destructive_count: 1,
        affected_resources: 1,
        stateful_deletions: ["aws_db_instance.main"],
        verdict: "block",
      },
      verdict: "block",
      approval_decision: null,
    });
    renderPage(<PlanAnalysisPage />);
    await userEvent.click(screen.getByTestId("analyse-plan"));

    expect(await screen.findByText("aws_db_instance.main")).toBeInTheDocument();
    // Deleting a stateless resource is recoverable by re-creating it; deleting one that holds state is
    // not. That asymmetry is the whole reason for the separate list.
    expect(screen.getByText(/deleting one that holds state is not/i)).toBeInTheDocument();
  });

  it("explains an absent blast radius as an earlier stage having refused the document", async () => {
    mockPost.mockResolvedValue({
      findings: [
        {
          stage: "schema",
          severity: "fatal",
          code: "not_a_plan",
          message: "no resource_changes",
          resource: null,
        },
      ],
      blast_radius: null,
      verdict: "fatal",
      approval_decision: null,
    });
    renderPage(<PlanAnalysisPage />);
    await userEvent.click(screen.getByTestId("analyse-plan"));

    // `fatal` and `block` are different: one means the document was never read, the other means it was
    // read and is too wide. Rendering both as red would tell an operator to narrow a plan nobody parsed.
    expect(await screen.findByText(/a stage refused the document itself/i)).toBeInTheDocument();
    expect(screen.getByText(/there was nothing to score/i)).toBeInTheDocument();
    expect(screen.getByTestId("plan-findings")).toHaveTextContent("not_a_plan");
  });

  it("says plainly that it analyses rather than authorises", async () => {
    renderPage(<PlanAnalysisPage />);
    // An `allow` here mistaken for a green light to apply would be worse than no screen.
    expect(screen.getByText(/this analyses\. it does not authorise\./i)).toBeInTheDocument();
  });
});

// ── Part E: the onboarding path ──────────────────────────────────────────────────────────────────

describe("the onboarding path", () => {
  function serve({
    projects = PROJECTS.projects,
    devices = [] as (typeof DEVICE)[],
    indexedFiles = 0,
    // `null` is the honest default: a fresh install has published nothing, and that is the state the
    // bundle step exists to warn about.
    activeDigest = null as string | null,
  } = {}) {
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/projects?limit="))
        return Promise.resolve({ projects, next_cursor: null });
      if (path.startsWith("/agents/devices?"))
        return Promise.resolve({ devices, next_cursor: null });
      if (path.startsWith("/policies/active-bundle"))
        return Promise.resolve({ digest: activeDigest, published_at: null });
      if (path.includes("/analysis/codebase/")) {
        return Promise.resolve({
          indexed_files: indexedFiles,
          total_chunks: indexedFiles === 0 ? 0 : 9,
          languages: [],
          status: indexedFiles === 0 ? "empty" : "indexed",
          total_bytes: 0,
          resolved_dependencies: 0,
          unresolved_dependencies: 0,
          last_indexed_at: null,
        });
      }
      return Promise.resolve(null);
    });
  }

  it("lists the eight steps in the order their preconditions require", async () => {
    serve();
    renderPage(<OnboardingPage />);
    const steps = await screen.findByTestId("onboarding-steps");
    const items = steps.querySelectorAll("li");
    expect(items).toHaveLength(8);
    // Publishing the bundle is step 5, BEFORE generation — which is the ordering the SSE paint test
    // discovered the hard way: a freshly created project fails at submission with a stale-bundle error
    // four layers from its cause.
    expect(items[4]).toHaveTextContent(/publish the policy bundle/i);
    expect(items[5]).toHaveTextContent(/generate an artifact/i);
  });

  it("marks step one done when a project exists, and not before", async () => {
    serve({ projects: [] });
    renderPage(<OnboardingPage />);
    // Awaited rather than read on first paint: the step element exists immediately with its
    // not-yet-known state, and the list request settles a tick later. A synchronous read here would
    // assert on the pre-fetch render and pass for the wrong reason.
    await waitFor(() => expect(screen.getByTestId("step-1-state")).toHaveTextContent("Not yet"));

    cleanup();
    serve();
    renderPage(<OnboardingPage />);
    await waitFor(() => expect(screen.getByTestId("step-1-state")).toHaveTextContent("Done"));
  });

  it("marks pairing done only when a device is ACTIVE for the chosen project", async () => {
    serve({ devices: [{ ...DEVICE, status: "pending" }] });
    renderPage(<OnboardingPage />);
    // A pending device means a code was minted and not exchanged: step 2 is done, step 3 is not.
    await waitFor(() => expect(screen.getByTestId("step-2-state")).toHaveTextContent("Done"));
    expect(screen.getByTestId("step-3-state")).toHaveTextContent("Not yet");

    cleanup();
    serve({ devices: [DEVICE] });
    renderPage(<OnboardingPage />);
    await waitFor(() => expect(screen.getByTestId("step-3-state")).toHaveTextContent("Done"));
  });

  it("marks the scan done from the real index count", async () => {
    serve({ devices: [DEVICE], indexedFiles: 141 });
    renderPage(<OnboardingPage />);
    await waitFor(() => expect(screen.getByTestId("step-4-state")).toHaveTextContent("Done"));
    expect(screen.getByTestId("onboarding-step-4")).toHaveTextContent("141 file(s)");
  });

  it("TICKS the bundle step from a real read route rather than disclaiming it", async () => {
    // THE CHANGE. This step was permanently "Not checked" for want of an endpoint, and it is the one
    // easiest to skip and hardest to diagnose: the chokepoint refuses every submission from a device
    // pinned to a different digest. So it got the endpoint the chokepoint's own query already implied.
    serve({ devices: [DEVICE], indexedFiles: 141, activeDigest: "sha256:abc123def456abc7890" });
    renderPage(<OnboardingPage />);
    // Waited for, not read once: the badge starts at "Checking…" by design, and asserting before the
    // query settles would test the loading state while claiming to test the result.
    await waitFor(() => expect(screen.getByTestId("step-5-state")).toHaveTextContent("Done"));
    expect(screen.getByTestId("onboarding-step-5")).toHaveTextContent(
      /a device paired now is pinned/i,
    );
  });

  it("says Not yet on the bundle step when nothing is published", async () => {
    // A REAL NEGATIVE, not a shrug, and distinguishable from "nothing here reports this" — which is
    // the whole point of separating the two.
    serve({ devices: [DEVICE], indexedFiles: 141, activeDigest: null });
    renderPage(<OnboardingPage />);
    await waitFor(() => expect(screen.getByTestId("step-5-state")).toHaveTextContent("Not yet"));
  });

  it("distinguishes an action from something it has no read route for", async () => {
    // "Not checked" used to cover both, and read as "not working" in both. A user who had done
    // everything right saw four grey labels and concluded the product was broken.
    serve({ devices: [DEVICE], activeDigest: "sha256:abc123def456abc7890" });
    renderPage(<OnboardingPage />);
    // Five steps are now checked against an endpoint, the bundle step among them.
    const checked = await screen.findAllByText(/^Checked against/i);
    expect(checked).toHaveLength(5);
    // Generate and approve are things you DO; no resting state would mean "done".
    expect(screen.getAllByText("Your move")).toHaveLength(2);
    // The applied queue is observable, just not from here. A different statement, said differently.
    expect(screen.getAllByText("Not reported here")).toHaveLength(1);
    // And the retired wording is gone, so neither meaning can quietly come back.
    expect(screen.queryByText("Not checked")).not.toBeInTheDocument();
  });

  it("carries the state as a word rather than only as a colour", async () => {
    serve({ projects: [] });
    renderPage(<OnboardingPage />);
    // A tick a screen reader cannot read is not a status.
    for (const n of [1, 2, 3, 4]) {
      expect((await screen.findByTestId(`step-${n}-state`)).textContent).toMatch(
        /Done|Not yet|Checking|Your move|Not reported here/,
      );
    }
  });

  it("explains why a missing step is reported as a step", async () => {
    serve();
    renderPage(<OnboardingPage />);
    expect(
      await screen.findByText(/having no way to learn that the missing thing was step/i),
    ).toBeInTheDocument();
  });
});
