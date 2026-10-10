// SPDX-License-Identifier: FSL-1.1-ALv2
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockPush = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
  useParams: () => ({ projectId: "proj-123", runId: "run-456" }),
}));

const get = vi.fn();
const post = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
      put: vi.fn(),
      delete: vi.fn(),
    },
  };
});

import {
  JenkinsPipelineDashboard,
  type AutonomousRunPublicResponse,
} from "@/features/deployments/JenkinsPipelineDashboard";

describe("JenkinsPipelineDashboard", () => {
  const projectId = "11111111-1111-4111-8111-111111111111";
  const runId = "22222222-2222-4222-8222-222222222222";
  const projectName = "SuperApp";

  function renderWithClient(ui: React.ReactElement) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
  }

  const mockFullStackRun: AutonomousRunPublicResponse = {
    id: runId,
    project_id: projectId,
    attempt_number: 1,
    status: "pending",
    strategy: "docker_github_vercel",
    progress_pct: 25,
    agent_connected: true,
    started_at: "2026-10-10T10:00:00Z",
    completed_at: null,
    stages: [
      {
        id: "s1",
        run_id: runId,
        stage_name: "G1_blueprint",
        gate_id: "G1",
        position: 1,
        status: "succeeded",
        progress_pct: 100,
        started_at: "2026-10-10T10:00:00Z",
        completed_at: "2026-10-10T10:00:05Z",
      },
      {
        id: "s2",
        run_id: runId,
        stage_name: "G2_artifact",
        gate_id: "G2",
        position: 2,
        status: "succeeded",
        progress_pct: 100,
        started_at: "2026-10-10T10:00:05Z",
        completed_at: "2026-10-10T10:00:10Z",
      },
      {
        id: "s3",
        run_id: runId,
        stage_name: "G3_consistency",
        gate_id: "G3",
        position: 3,
        status: "succeeded",
        progress_pct: 100,
        started_at: "2026-10-10T10:00:10Z",
        completed_at: "2026-10-10T10:00:15Z",
      },
      {
        id: "s4",
        run_id: runId,
        stage_name: "G4_build",
        gate_id: "G4",
        position: 4,
        status: "running",
        progress_pct: 50,
        started_at: "2026-10-10T10:00:15Z",
        completed_at: null,
      },
      {
        id: "s5",
        run_id: runId,
        stage_name: "G5_apply",
        gate_id: "G5",
        position: 5,
        status: "pending",
        progress_pct: 0,
      },
      {
        id: "s6",
        run_id: runId,
        stage_name: "G6_workload",
        gate_id: "G6",
        position: 6,
        status: "pending",
        progress_pct: 0,
      },
      {
        id: "s7",
        run_id: runId,
        stage_name: "github_release",
        gate_id: null,
        position: 7,
        status: "pending",
        progress_pct: 0,
      },
      {
        id: "s8",
        run_id: runId,
        stage_name: "vercel_deploy",
        gate_id: null,
        position: 8,
        status: "pending",
        progress_pct: 0,
      },
      {
        id: "s9",
        run_id: runId,
        stage_name: "G7_verification",
        gate_id: "G7",
        position: 9,
        status: "pending",
        progress_pct: 0,
      },
    ],
  };

  const mockGithubOnlyRun: AutonomousRunPublicResponse = {
    id: "33333333-3333-4333-8333-333333333333",
    project_id: projectId,
    attempt_number: 1,
    status: "succeeded",
    strategy: "github_only",
    progress_pct: 100,
    agent_connected: true,
    started_at: "2026-10-10T10:00:00Z",
    completed_at: "2026-10-10T10:01:20Z",
    stages: [
      {
        id: "gh-s1",
        run_id: "33333333-3333-4333-8333-333333333333",
        stage_name: "G1_blueprint",
        gate_id: "G1",
        position: 1,
        status: "succeeded",
        progress_pct: 100,
        started_at: "2026-10-10T10:00:00Z",
        completed_at: "2026-10-10T10:00:05Z",
      },
      {
        id: "gh-s2",
        run_id: "33333333-3333-4333-8333-333333333333",
        stage_name: "G2_artifact",
        gate_id: "G2",
        position: 2,
        status: "succeeded",
        progress_pct: 100,
        started_at: "2026-10-10T10:00:05Z",
        completed_at: "2026-10-10T10:00:10Z",
      },
      {
        id: "gh-s3",
        run_id: "33333333-3333-4333-8333-333333333333",
        stage_name: "G3_consistency",
        gate_id: "G3",
        position: 3,
        status: "succeeded",
        progress_pct: 100,
        started_at: "2026-10-10T10:00:10Z",
        completed_at: "2026-10-10T10:00:15Z",
      },
      {
        id: "gh-s4",
        run_id: "33333333-3333-4333-8333-333333333333",
        stage_name: "github_release",
        gate_id: null,
        position: 4,
        status: "succeeded",
        progress_pct: 100,
        started_at: "2026-10-10T10:00:15Z",
        completed_at: "2026-10-10T10:00:55Z",
        stage_metadata: {
          commit_sha: "abc123def456",
          message: "GitHub release published to forgeops-team/superapp",
        },
      },
      {
        id: "gh-s5",
        run_id: "33333333-3333-4333-8333-333333333333",
        stage_name: "G7_verification",
        gate_id: "G7",
        position: 5,
        status: "succeeded",
        progress_pct: 100,
        started_at: "2026-10-10T10:00:55Z",
        completed_at: "2026-10-10T10:01:20Z",
        stage_metadata: {
          target_results: {
            docker: "not_applicable",
            github: "verified",
            vercel: "not_applicable",
          },
        },
      },
    ],
  };

  const mockFailedRun: AutonomousRunPublicResponse = {
    id: "44444444-4444-4444-8444-444444444444",
    project_id: projectId,
    attempt_number: 2,
    status: "failed",
    strategy: "docker_github",
    progress_pct: 45,
    agent_connected: true,
    started_at: "2026-10-10T10:00:00Z",
    completed_at: "2026-10-10T10:00:30Z",
    stages: [
      {
        id: "fail-s1",
        run_id: "44444444-4444-4444-8444-444444444444",
        stage_name: "G1_blueprint",
        gate_id: "G1",
        position: 1,
        status: "succeeded",
        progress_pct: 100,
      },
      {
        id: "fail-s2",
        run_id: "44444444-4444-4444-8444-444444444444",
        stage_name: "G4_build",
        gate_id: "G4",
        position: 2,
        status: "failed",
        progress_pct: 50,
        error_message: "Compilation error in Dockerfile: syntax error line 12",
      },
      {
        id: "fail-s3",
        run_id: "44444444-4444-4444-8444-444444444444",
        stage_name: "G5_apply",
        gate_id: "G5",
        position: 3,
        status: "cancelled",
        progress_pct: 0,
      },
      {
        id: "fail-s4",
        run_id: "44444444-4444-4444-8444-444444444444",
        stage_name: "G7_verification",
        gate_id: "G7",
        position: 4,
        status: "failed",
        progress_pct: 0,
        stage_metadata: {
          target_results: {
            docker: "failed",
            github: "not_applicable",
            vercel: "not_applicable",
          },
        },
      },
    ],
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders header summary bar, breadcrumb, badges, and progress bar", () => {
    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={mockFullStackRun}
      />,
    );

    // Breadcrumb
    const breadcrumb = screen.getByTestId("breadcrumb");
    expect(breadcrumb).toBeInTheDocument();
    expect(screen.getByText("Projects")).toBeInTheDocument();
    expect(screen.getByText("SuperApp")).toBeInTheDocument();
    expect(screen.getByText("Run #1")).toBeInTheDocument();

    // Badges
    expect(screen.getByTestId("run-id-badge")).toHaveTextContent(runId);
    expect(screen.getByTestId("strategy-badge")).toHaveTextContent("Docker + GitHub + Vercel");
    expect(screen.getByTestId("status-badge")).toHaveTextContent("pending");

    // Monotonic progress bar
    const progressBar = screen.getByTestId("progress-bar");
    expect(progressBar).toHaveAttribute("aria-valuenow", "25");
    expect(screen.getByText("25%")).toBeInTheDocument();

    // Elapsed timer
    expect(screen.getByTestId("elapsed-timer")).toBeInTheDocument();

    // Agent status banner
    expect(screen.getByTestId("agent-status-banner")).toHaveTextContent("Paired Agent Connected");
  });

  it("renders all distinct G1-G7 gates and operational stages in the Blue Ocean graph", () => {
    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={mockFullStackRun}
      />,
    );

    const graph = screen.getByTestId("blue-ocean-graph");
    expect(graph).toBeInTheDocument();

    // Distinct nodes for full stack:
    expect(screen.getByTestId("stage-node-G1_blueprint")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G2_artifact")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G3_consistency")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G4_build")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G5_apply")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G6_workload")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-github_release")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-vercel_deploy")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G7_verification")).toBeInTheDocument();
  });

  it("omits inapplicable stages when strategy does not include them", () => {
    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={mockGithubOnlyRun}
      />,
    );

    // Docker stages must be completely omitted
    expect(screen.queryByTestId("stage-node-G4_build")).not.toBeInTheDocument();
    expect(screen.queryByTestId("stage-node-G5_apply")).not.toBeInTheDocument();
    expect(screen.queryByTestId("stage-node-G6_workload")).not.toBeInTheDocument();
    // Vercel stage must be omitted
    expect(screen.queryByTestId("stage-node-vercel_deploy")).not.toBeInTheDocument();

    // GitHub and gates must be present
    expect(screen.getByTestId("stage-node-G1_blueprint")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-github_release")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G7_verification")).toBeInTheDocument();
  });

  it("supports interactive stage selection and displays active stage details", async () => {
    const user = userEvent.setup();
    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={mockGithubOnlyRun}
      />,
    );

    const detailsCard = screen.getByTestId("stage-details-card");
    expect(detailsCard).toBeInTheDocument();

    // Click on github_release node
    const githubNode = screen.getByTestId("stage-node-github_release");
    await user.click(githubNode);

    // Details card updates with selected stage info
    expect(screen.getByRole("heading", { name: "GitHub Release" })).toBeInTheDocument();
    expect(screen.getByTestId("gate-verdict")).toHaveTextContent("Passed");
    expect(screen.getByTestId("commit-sha")).toHaveTextContent("abc123def456");
  });

  it("displays visual status styles for pending, running, succeeded, failed, cancelled, and skipped", () => {
    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={mockFailedRun}
      />,
    );

    // Succeeded stage G1
    const g1Node = screen.getByTestId("stage-node-G1_blueprint");
    expect(g1Node).toHaveTextContent("succeeded");

    // Failed stage G4
    const g4Node = screen.getByTestId("stage-node-G4_build");
    expect(g4Node).toHaveTextContent("failed");

    // Cancelled stage G5
    const g5Node = screen.getByTestId("stage-node-G5_apply");
    expect(g5Node).toHaveTextContent("cancelled");
  });

  it("disables Start button when agent is disconnected and enables when connected", async () => {
    const user = userEvent.setup();
    const disconnectedRun: AutonomousRunPublicResponse = {
      ...mockFullStackRun,
      agent_connected: false,
    };

    const { rerender } = renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={disconnectedRun}
      />,
    );

    const startBtn = screen.getByTestId("btn-start");
    expect(startBtn).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Local Agent Disconnected");

    // Rerender with agent connected
    rerender(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <JenkinsPipelineDashboard
          projectId={projectId}
          projectName={projectName}
          run={mockFullStackRun}
        />
      </QueryClientProvider>,
    );

    const enabledStartBtn = screen.getByTestId("btn-start");
    expect(enabledStartBtn).not.toBeDisabled();

    // Clicking Start calls API endpoint
    post.mockResolvedValueOnce({
      ...mockFullStackRun,
      status: "running",
    });

    await user.click(enabledStartBtn);
    expect(post).toHaveBeenCalledWith(
      `/projects/${projectId}/autonomous-deploy/${runId}/start`,
    );
  });

  it("handles Cancel button interactions correctly", async () => {
    const user = userEvent.setup();
    const runningRun: AutonomousRunPublicResponse = {
      ...mockFullStackRun,
      status: "running",
    };

    post.mockResolvedValueOnce({
      ...runningRun,
      status: "cancelling",
    });

    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={runningRun}
      />,
    );

    const cancelBtn = screen.getByTestId("btn-cancel");
    expect(cancelBtn).toBeInTheDocument();
    expect(cancelBtn).not.toBeDisabled();

    await user.click(cancelBtn);
    expect(post).toHaveBeenCalledWith(
      `/projects/${projectId}/autonomous-deploy/${runId}/cancel`,
    );
  });

  it("shows Cancelling... when status is cancelling", () => {
    const cancellingRun: AutonomousRunPublicResponse = {
      ...mockFullStackRun,
      status: "cancelling",
    };

    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={cancellingRun}
      />,
    );

    const cancelBtn = screen.getByTestId("btn-cancel");
    expect(cancelBtn).toHaveTextContent("Cancelling...");
    expect(cancelBtn).toBeDisabled();
  });

  it("renders Retry button on failed runs and navigates to new attempt when clicked", async () => {
    const user = userEvent.setup();
    const newAttemptId = "55555555-5555-4555-8555-555555555555";
    post.mockResolvedValueOnce({
      id: newAttemptId,
      project_id: projectId,
      attempt_number: 3,
      status: "pending",
      stages: [],
    });

    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={mockFailedRun}
      />,
    );

    const retryBtn = screen.getByTestId("btn-retry");
    expect(retryBtn).toBeInTheDocument();
    expect(retryBtn).not.toBeDisabled();

    await user.click(retryBtn);

    expect(post).toHaveBeenCalledWith(
      `/projects/${projectId}/autonomous-deploy/${mockFailedRun.id}/retry`,
    );
    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith(
        `/projects/${projectId}/autonomous-deploy/${newAttemptId}`,
      );
    });
  });

  it("evaluates strategy-aware G7 multi-target verification summary breakdown", () => {
    // For github_only run
    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={mockGithubOnlyRun}
      />,
    );

    const summary = screen.getByTestId("g7-verification-summary");
    expect(summary).toBeInTheDocument();

    const dockerTarget = screen.getByTestId("g7-target-docker");
    const githubTarget = screen.getByTestId("g7-target-github");
    const vercelTarget = screen.getByTestId("g7-target-vercel");

    expect(dockerTarget).toHaveTextContent("Not Applicable");
    expect(githubTarget).toHaveTextContent("Verified");
    expect(vercelTarget).toHaveTextContent("Not Applicable");
  });

  it("renders Failed for failing target in G7 verification breakdown", () => {
    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={mockFailedRun}
      />,
    );

    const dockerTarget = screen.getByTestId("g7-target-docker");
    const vercelTarget = screen.getByTestId("g7-target-vercel");

    expect(dockerTarget).toHaveTextContent("Failed");
    expect(vercelTarget).toHaveTextContent("Not Applicable");
  });

  it("renders reload-safe Pull Request card with flow pill, badge, and URL", async () => {
    const user = userEvent.setup();
    const prRun: AutonomousRunPublicResponse = {
      ...mockFullStackRun,
      stages: mockFullStackRun.stages.map((s) =>
        s.stage_name === "github_release"
          ? {
              ...s,
              status: "succeeded",
              stage_metadata: {
                publishing_mode: "pull_request",
                pr_number: 42,
                pr_url: "https://github.com/myorg/myrepo/pull/42",
                pr_state: "open",
                pr_merged: false,
                source_branch: "forgeops/deploy-11111111",
                base_branch: "main",
                commit_sha: "commit123456789",
              },
            }
          : s,
      ),
    };

    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={prRun}
      />,
    );

    // Click github_release node
    const githubNode = screen.getByTestId("stage-node-github_release");
    await user.click(githubNode);

    // Verify PR Card components
    expect(screen.getByTestId("github-pr-card")).toBeInTheDocument();
    expect(screen.getByText("Pull Request #42")).toBeInTheDocument();
    expect(screen.getByTestId("pr-status-badge")).toHaveTextContent("open");
    expect(screen.getByTestId("github-pr-url")).toHaveAttribute("href", "https://github.com/myorg/myrepo/pull/42");
    expect(screen.getByTestId("branch-flow-pill")).toHaveTextContent("forgeops/deploy-11111111");
    expect(screen.getByTestId("branch-flow-pill")).toHaveTextContent("main");
    expect(screen.getByTestId("pr-commit-sha")).toHaveTextContent("commit12");
  });
});
