// SPDX-License-Identifier: Apache-2.0
import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ProjectList } from "../features/projects/ProjectList";
import { ReadinessRadarChart } from "../features/readiness/RadarChart";
import { PolicyEditor } from "../features/policies/PolicyEditor";
import { SecretVault } from "../features/vault/SecretVault";
import { AuditViewer } from "../features/audit/AuditViewer";

describe("Frontend Feature Surfaces", () => {
  it("renders project list component", () => {
    const projects = [{ id: "p1", name: "Demo App", repository: "org/repo", readinessScore: 85 }];
    render(<ProjectList projects={projects} />);
    expect(screen.getByText("Demo App")).toBeInTheDocument();
  });

  it("renders readiness radar chart component", () => {
    const scores = [{ category: "Security", score: 90 }];
    render(<ReadinessRadarChart scores={scores} />);
    expect(screen.getByText("Security")).toBeInTheDocument();
  });

  it("renders policy editor component", () => {
    render(<PolicyEditor />);
    expect(screen.getByText("OPA Policy Editor")).toBeInTheDocument();
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
    render(<SecretVault secrets={secrets} />);
    expect(screen.getByText("Secret references")).toBeInTheDocument();
    expect(screen.getByText("STRIPE_API_TOKEN")).toBeInTheDocument();
    expect(screen.getByText("Infisical")).toBeInTheDocument();
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
