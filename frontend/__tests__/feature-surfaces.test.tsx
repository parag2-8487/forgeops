// SPDX-License-Identifier: Apache-2.0
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ProjectList } from "../features/projects/ProjectList";
import { ReadinessRadarChart } from "../features/readiness/RadarChart";
import { PolicyEditor } from "../features/policies/PolicyEditor";
import { SecretVault } from "../features/vault/SecretVault";
import { AuditViewer } from "../features/audit/AuditViewer";
import type { ProjectResponse } from "../features/projects/types";

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

/**
 * Several of these components now issue mutations, so they need a query client in the tree. Retries
 * off, because a test asserting a failure path should not wait through three back-offs.
 */
function withQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

function project(overrides: Partial<ProjectResponse> = {}): ProjectResponse {
  return {
    id: "p1",
    name: "Demo App",
    path: "/srv/demo",
    repo_url: "https://github.com/org/repo",
    settings: {},
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    archived_at: null,
    tags: [],
    favourite: false,
    indexed_file_count: 0,
    ...overrides,
  };
}

describe("Frontend Feature Surfaces", () => {
  /**
   * `ProjectList` now takes the real `ProjectResponse`, not `{ id, name, repository, readinessScore }`.
   *
   * The old shape is why the projects page mapped every project to `readinessScore: 0`: the component
   * demanded a score, the list endpoint could not cheaply produce one, and the page supplied a literal
   * rather than changing the contract. The contract changed.
   */
  it("renders a project row from a real project response", () => {
    render(withQuery(<ProjectList projects={[project()]} />));
    expect(screen.getByText("Demo App")).toBeInTheDocument();
    expect(screen.getByText("https://github.com/org/repo")).toBeInTheDocument();
  });

  it("says an unscanned project is unscanned rather than showing it a score", () => {
    render(withQuery(<ProjectList projects={[project({ indexed_file_count: 0 })]} />));
    // The literal that used to be here was `0`. Zero files is a fact; a readiness score of zero for a
    // project nobody has scanned is not.
    expect(screen.getByTestId("index-p1")).toHaveTextContent(/not scanned/i);
    expect(screen.getByTestId("index-p1")).not.toHaveTextContent("0/100");
  });

  it("reports a scanned project's file count", () => {
    render(withQuery(<ProjectList projects={[project({ indexed_file_count: 141 })]} />));
    expect(screen.getByTestId("index-p1")).toHaveTextContent("141 files indexed");
  });

  it("renders a project's tags, and says so when there are none", () => {
    render(withQuery(<ProjectList projects={[project({ tags: ["prod", "eu"] })]} />));
    expect(screen.getByTestId("tags-p1")).toHaveTextContent("prod");
    expect(screen.getByTestId("tags-p1")).toHaveTextContent("eu");

    render(withQuery(<ProjectList projects={[project({ id: "p2", tags: [] })]} />));
    expect(screen.getByTestId("tags-p2")).toHaveTextContent("none");
  });

  it("exposes the favourite toggle as a two-state control rather than two labels", () => {
    render(withQuery(<ProjectList projects={[project({ favourite: true })]} />));
    // `aria-pressed` so a screen reader hears the state instead of inferring it from a changing name.
    expect(screen.getByTestId("favourite-p1")).toHaveAttribute("aria-pressed", "true");
  });

  it("renders readiness radar chart component", () => {
    const scores = [{ category: "Security", score: 90 }];
    render(<ReadinessRadarChart scores={scores} />);
    expect(screen.getByText("Security")).toBeInTheDocument();
  });

  /**
   * `PolicyEditor` took no props and its save button was wired to nothing. It now requires the policy
   * it edits, the templates it can start from, and both callbacks — so a caller cannot mount an editor
   * that has nowhere to save to, which is what the previous version was.
   */
  it("renders the policy editor for a new policy, with a starting Rego skeleton", () => {
    render(
      withQuery(<PolicyEditor policy={null} templates={[]} onSaved={vi.fn()} onCancel={vi.fn()} />),
    );
    expect(screen.getByText("New policy")).toBeInTheDocument();
    // The skeleton names the rule the chokepoint actually evaluates, so a first-time author does not
    // write a policy in a package nothing reads.
    expect((screen.getByTestId("policy-rego") as HTMLTextAreaElement).value).toContain(
      "package forgeops.governance",
    );
  });

  it("does not offer a dry run before the policy is stored", () => {
    render(
      withQuery(<PolicyEditor policy={null} templates={[]} onSaved={vi.fn()} onCancel={vi.fn()} />),
    );
    // The dry-run evaluates the STORED Rego, so testing an unsaved draft would report on a policy that
    // does not exist.
    expect(screen.queryByTestId("run-dryrun")).not.toBeInTheDocument();
    expect(screen.getByText(/Save the policy first/i)).toBeInTheDocument();
  });

  // These two components took no props until they were converted to render real data. Both used
  // to hardcode their contents, so the old assertions could only check a title. Now they are
  // given records and asserted on the records, which is the difference between testing that a
  // component renders and testing that it renders what it was handed.
  it("renders secret vault component from supplied references", () => {
    const secrets = [
      {
        id: "s1",
        key: "STRIPE_API_TOKEN",
        environment: "staging",
        infisical_path: "/apps/web",
        is_local: false,
      },
    ];
    render(withQuery(<SecretVault secrets={secrets} readOnly />));
    expect(screen.getByText("Secret references")).toBeInTheDocument();
    expect(screen.getByText("STRIPE_API_TOKEN")).toBeInTheDocument();
    expect(screen.getByText("Infisical")).toBeInTheDocument();
  });

  it("offers no write controls in read-only mode, and no value field in either mode", () => {
    const secrets = [
      { id: "s1", key: "K", environment: "staging", infisical_path: null, is_local: true },
    ];
    render(withQuery(<SecretVault secrets={secrets} readOnly />));
    expect(screen.queryByTestId("rotate-s1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("delete-secret-s1")).not.toBeInTheDocument();
    // The list can never show a value: the response shape carries none, so there is nothing to render.
    expect(screen.queryByLabelText(/^value$/i)).not.toBeInTheDocument();
  });

  it("renders audit viewer component from supplied events", () => {
    const events = [
      {
        seq: 42,
        id: "e1",
        action: "policy_evaluated",
        actor_kind: "user",
        resource_kind: "changeset",
        resource_id: "cs-9",
        outcome: "allow",
        reason: "matched the staging rule",
      },
    ];
    render(<AuditViewer events={events} />);
    expect(screen.getByText("Audit event log")).toBeInTheDocument();
    expect(screen.getByText("policy_evaluated")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("changeset/cs-9")).toBeInTheDocument();
  });

  // `pairing/AgentPairing.tsx` IS DELETED, and its smoke test with it.
  //
  // It rendered `SPIFFE Trust Domain: spiffe://cluster.local` and `Status: Connected & Attested`
  // from no props and no fetch -- a security control reported as passing by a component that could
  // not observe it. Nothing rendered it once `/pairing` was rewritten against
  // `GET /api/v1/agents/devices`, so it was an unused component whose only remaining effect was to
  // make a fabricated attestation claim available to the next person who imported it.
  //
  // The old assertion here is worth remembering as a category: it checked that the component
  // rendered its own title. A test like that passes for a component that has never been correct,
  // which is why the replacement asserts observed device state in `route-pages.test.tsx` instead.
});
