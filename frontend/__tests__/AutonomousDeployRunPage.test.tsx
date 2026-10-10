// SPDX-License-Identifier: FSL-1.1-ALv2
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockParams = vi.fn();
vi.mock("next/navigation", () => ({
  useParams: () => mockParams(),
}));

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

const mockApiGet = vi.fn();
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => mockApiGet(...args),
      post: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
    },
  };
});

vi.mock("@/features/deployments/JenkinsPipelineDashboard", () => ({
  JenkinsPipelineDashboard: ({
    projectName,
    runId,
    onRefresh,
  }: {
    projectName: string;
    runId: string;
    onRefresh: () => void;
  }) => (
    <div data-testid="mock-dashboard">
      <span>{projectName}</span>
      <span>{runId}</span>
      <button onClick={onRefresh} data-testid="refresh-btn">
        Refresh
      </button>
    </div>
  ),
}));

import AutonomousDeployRunPage from "@/app/(shell)/projects/[projectId]/autonomous-deploy/[runId]/page";

describe("AutonomousDeployRunPage", () => {
  const projectId = "11111111-1111-4111-8111-111111111111";
  const runId = "22222222-2222-4222-8222-222222222222";

  function renderWithClient() {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
    return render(
      <QueryClientProvider client={queryClient}>
        <AutonomousDeployRunPage />
      </QueryClientProvider>,
    );
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mockParams.mockReturnValue({ projectId, runId });
  });

  it("renders loading skeleton initially while run is fetching", () => {
    mockApiGet.mockImplementation(() => new Promise(() => {}));
    renderWithClient();
    expect(screen.getByTestId("pipeline-page-skeleton")).toBeInTheDocument();
  });

  it("renders error state when run query fails", async () => {
    mockApiGet.mockRejectedValue(new Error("Network connection lost"));
    renderWithClient();

    await waitFor(() => {
      expect(screen.getByTestId("pipeline-page-error")).toBeInTheDocument();
    });

    expect(screen.getByText("Failed to Load Deployment Pipeline")).toBeInTheDocument();
    expect(screen.getByText("Network connection lost")).toBeInTheDocument();
    expect(screen.getByText("Return to Project Overview")).toBeInTheDocument();
  });

  it("renders dashboard when project and run queries resolve successfully", async () => {
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes(`/projects/${projectId}/autonomous-deploy/${runId}`)) {
        return Promise.resolve({
          id: runId,
          project_id: projectId,
          status: "running",
          strategy: "docker_github_vercel",
          stages: [],
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        });
      }
      if (url.includes(`/projects/${projectId}`)) {
        return Promise.resolve({
          id: projectId,
          name: "Acme Platform",
        });
      }
      return Promise.reject(new Error("Unknown route"));
    });

    renderWithClient();

    await waitFor(() => {
      expect(screen.getByTestId("mock-dashboard")).toBeInTheDocument();
    });

    expect(screen.getByText("Acme Platform")).toBeInTheDocument();
    expect(screen.getByText(runId)).toBeInTheDocument();
  });

  it("falls back to default Project name when project detail query resolves without a name", async () => {
    mockApiGet.mockImplementation((url: string) => {
      if (url.includes(`/projects/${projectId}/autonomous-deploy/${runId}`)) {
        return Promise.resolve({
          id: runId,
          project_id: projectId,
          status: "succeeded",
          strategy: "github_only",
          stages: [],
        });
      }
      return Promise.resolve({});
    });

    renderWithClient();

    await waitFor(() => {
      expect(screen.getByTestId("mock-dashboard")).toBeInTheDocument();
    });

    expect(screen.getByText("Project")).toBeInTheDocument();
  });
});
