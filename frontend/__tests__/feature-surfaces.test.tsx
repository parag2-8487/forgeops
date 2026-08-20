// SPDX-License-Identifier: Apache-2.0
import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ProjectList } from "../features/projects/ProjectList";
import { ReadinessRadarChart } from "../features/readiness/RadarChart";
import { GeneratorWizard } from "../features/generation/GeneratorWizard";
import { ApprovalCenter } from "../features/approvals/ApprovalCenter";
import { PolicyEditor } from "../features/policies/PolicyEditor";
import { SecretVault } from "../features/vault/SecretVault";
import { AuditViewer } from "../features/audit/AuditViewer";
import { AgentPairing } from "../features/pairing/AgentPairing";

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

  it("renders generator wizard component", () => {
    render(<GeneratorWizard />);
    expect(screen.getByText("Artifact Generator Wizard")).toBeInTheDocument();
  });

  it("renders approval center component", () => {
    const cs = [
      { id: "cs-1", summary: "Fix config", status: "PENDING", diff: "--- file\n+++ file" },
    ];
    render(<ApprovalCenter changeSets={cs} />);
    expect(screen.getByText("Fix config")).toBeInTheDocument();
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

  it("renders agent pairing component", () => {
    render(<AgentPairing />);
    expect(screen.getByText("Agent Pairing & Workload Attestation")).toBeInTheDocument();
  });
});
