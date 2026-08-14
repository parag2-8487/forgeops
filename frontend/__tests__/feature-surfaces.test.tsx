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

  it("renders secret vault component", () => {
    render(<SecretVault />);
    expect(screen.getByText("Secret Vault Management")).toBeInTheDocument();
  });

  it("renders audit viewer component", () => {
    render(<AuditViewer />);
    expect(screen.getByText("Audit Event Log Viewer")).toBeInTheDocument();
  });

  it("renders agent pairing component", () => {
    render(<AgentPairing />);
    expect(screen.getByText("Agent Pairing & Workload Attestation")).toBeInTheDocument();
  });
});
