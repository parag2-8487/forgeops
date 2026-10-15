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

let mockStreamOverrides: Record<string, unknown> = {};

vi.mock("@/features/deployments/useAutonomousDeployStream", async () => {
  const actual = await vi.importActual<
    typeof import("@/features/deployments/useAutonomousDeployStream")
  >("@/features/deployments/useAutonomousDeployStream");
  return {
    ...actual,
    useAutonomousDeployStream: (options: unknown) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const res = actual.useAutonomousDeployStream(options as any);
      return {
        ...res,
        ...mockStreamOverrides,
      };
    },
  };
});

import {
  JenkinsPipelineDashboard,
  formatElapsedTime,
  getStrategyLabel,
  getStageDisplayInfo,
  getTargetVerificationStatus,
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
    mockStreamOverrides = {};
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
    expect(post).toHaveBeenCalledWith(`/projects/${projectId}/autonomous-deploy/${runId}/start`);
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
      <JenkinsPipelineDashboard projectId={projectId} projectName={projectName} run={runningRun} />,
    );

    const cancelBtn = screen.getByTestId("btn-cancel");
    expect(cancelBtn).toBeInTheDocument();
    expect(cancelBtn).not.toBeDisabled();

    await user.click(cancelBtn);
    expect(post).toHaveBeenCalledWith(`/projects/${projectId}/autonomous-deploy/${runId}/cancel`);
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
      <JenkinsPipelineDashboard projectId={projectId} projectName={projectName} run={prRun} />,
    );

    // Click github_release node
    const githubNode = screen.getByTestId("stage-node-github_release");
    await user.click(githubNode);

    // Verify PR Card components
    expect(screen.getByTestId("github-pr-card")).toBeInTheDocument();
    expect(screen.getByText("Pull Request #42")).toBeInTheDocument();
    expect(screen.getByTestId("pr-status-badge")).toHaveTextContent("open");
    expect(screen.getByTestId("github-pr-url")).toHaveAttribute(
      "href",
      "https://github.com/myorg/myrepo/pull/42",
    );
    expect(screen.getByTestId("branch-flow-pill")).toHaveTextContent("forgeops/deploy-11111111");
    expect(screen.getByTestId("branch-flow-pill")).toHaveTextContent("main");
    expect(screen.getByTestId("pr-commit-sha")).toHaveTextContent("commit12");
  });

  it("renders stages with waiting, cancelling, cancelled, rolled_back, and skipped statuses", () => {
    const multiStatusRun: AutonomousRunPublicResponse = {
      id: "55555555-5555-5555-8555-555555555555",
      project_id: projectId,
      attempt_number: 1,
      status: "cancelling",
      strategy: "docker_github_vercel",
      progress_pct: 50,
      agent_connected: true,
      stages: [
        {
          id: "ms-s1",
          run_id: "55555555-5555-5555-8555-555555555555",
          stage_name: "G1_blueprint",
          gate_id: "G1",
          position: 1,
          status: "rolled_back",
          progress_pct: 100,
        },
        {
          id: "ms-s2",
          run_id: "55555555-5555-5555-8555-555555555555",
          stage_name: "G2_existing_artifacts",
          gate_id: "G2",
          position: 2,
          status: "waiting",
          progress_pct: 0,
        },
        {
          id: "ms-s3",
          run_id: "55555555-5555-5555-8555-555555555555",
          stage_name: "G3_consistency",
          gate_id: "G3",
          position: 3,
          status: "cancelling",
          progress_pct: 30,
        },
        {
          id: "ms-s4",
          run_id: "55555555-5555-5555-8555-555555555555",
          stage_name: "G4_build",
          gate_id: "G4",
          position: 4,
          status: "cancelled",
          progress_pct: 0,
        },
        {
          id: "ms-s5",
          run_id: "55555555-5555-5555-8555-555555555555",
          stage_name: "G5_apply",
          gate_id: "G5",
          position: 5,
          status: "skipped",
          progress_pct: 0,
        },
      ],
    };

    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={multiStatusRun}
      />,
    );

    expect(screen.getByTestId("stage-node-G1_blueprint")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G2_existing_artifacts")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G3_consistency")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G4_build")).toBeInTheDocument();
    expect(screen.getByTestId("stage-node-G5_apply")).toBeInTheDocument();
  });

  it("renders Docker container, Vercel deployment URL, and direct push cards when metadata is present", async () => {
    const user = userEvent.setup();
    const artifactsRun: AutonomousRunPublicResponse = {
      id: "66666666-6666-6666-8666-666666666666",
      project_id: projectId,
      attempt_number: 1,
      status: "succeeded",
      strategy: "docker_github_vercel",
      progress_pct: 100,
      agent_connected: true,
      stages: [
        {
          id: "art-s1",
          run_id: "66666666-6666-6666-8666-666666666666",
          stage_name: "docker_deploy",
          gate_id: null,
          position: 1,
          status: "succeeded",
          progress_pct: 100,
          stage_metadata: {
            container_id: "c1234567890abcdef",
            image: "my-docker-image:latest",
          },
        },
        {
          id: "art-s2",
          run_id: "66666666-6666-6666-8666-666666666666",
          stage_name: "vercel_deploy",
          gate_id: null,
          position: 2,
          status: "succeeded",
          progress_pct: 100,
          stage_metadata: {
            deployment_url: "https://my-vercel-deployment.vercel.app",
          },
        },
        {
          id: "art-s3",
          run_id: "66666666-6666-6666-8666-666666666666",
          stage_name: "github_release",
          gate_id: null,
          position: 3,
          status: "succeeded",
          progress_pct: 100,
          stage_metadata: {
            publishing_mode: "direct_push",
            target_branch: "release-v1",
            commit_sha: "fedcba9876543210",
          },
        },
      ],
    };

    renderWithClient(
      <JenkinsPipelineDashboard
        projectId={projectId}
        projectName={projectName}
        run={artifactsRun}
      />,
    );

    // Click docker_deploy
    await user.click(screen.getByTestId("stage-node-docker_deploy"));
    expect(screen.getByTestId("container-id")).toHaveTextContent("c1234567890abcdef");

    // Click vercel_deploy
    await user.click(screen.getByTestId("stage-node-vercel_deploy"));
    expect(screen.getByTestId("deployment-url")).toHaveAttribute(
      "href",
      "https://my-vercel-deployment.vercel.app",
    );

    // Click github_release
    await user.click(screen.getByTestId("stage-node-github_release"));
    expect(screen.getByTestId("commit-sha")).toHaveTextContent("fedcba9876543210");
  });

  describe("helper functions", () => {
    it("formatElapsedTime returns expected formatted strings across edge cases", () => {
      expect(formatElapsedTime(null)).toBe("--");
      expect(formatElapsedTime(undefined)).toBe("--");
      expect(formatElapsedTime("invalid-date")).toBe("--");
      expect(formatElapsedTime("2026-10-10T10:00:00Z", "2026-10-10T10:00:30Z")).toBe("30s");
      expect(formatElapsedTime("2026-10-10T10:00:00Z", "2026-10-10T10:02:15Z")).toBe("2m 15s");
      expect(formatElapsedTime("2026-10-10T10:00:00.000Z", "2026-10-10T10:00:00.015Z")).toBe("<1s");
      expect(formatElapsedTime("2026-10-10T10:00:00.000Z", "2026-10-10T10:00:00.329Z")).toBe("<1s");
      expect(formatElapsedTime("2026-10-10T10:00:00.000Z", "2026-10-10T10:00:00.000Z")).toBe("<1s");
    });

    it("getStrategyLabel formats strategies and falls back to input string", () => {
      expect(getStrategyLabel("docker_github_vercel")).toBe("Docker + GitHub + Vercel");
      expect(getStrategyLabel("docker_github")).toBe("Docker + GitHub");
      expect(getStrategyLabel("github_only")).toBe("GitHub Only");
      expect(getStrategyLabel("vercel_only")).toBe("Vercel Only");
      expect(getStrategyLabel("custom_strategy")).toBe("custom_strategy");
    });

    it("getStageDisplayInfo handles custom stage names without gates", () => {
      const customStage = {
        id: "cust-1",
        run_id: "r1",
        stage_name: "custom_cleanup",
        gate_id: null,
        position: 10,
        status: "succeeded" as const,
        progress_pct: 100,
      };
      const info = getStageDisplayInfo(customStage);
      expect(info.title).toBe("custom_cleanup");
      expect(info.badge).toBe("Step");
      expect(info.isGate).toBe(false);
    });

    it("getTargetVerificationStatus handles failed G7 targets and fallback branches", () => {
      const stages = [
        {
          id: "s1",
          run_id: "r1",
          stage_name: "docker_apply",
          gate_id: "G5",
          position: 1,
          status: "failed" as const,
          progress_pct: 50,
        },
        {
          id: "s2",
          run_id: "r1",
          stage_name: "github_release",
          gate_id: null,
          position: 2,
          status: "failed" as const,
          progress_pct: 50,
        },
        {
          id: "s3",
          run_id: "r1",
          stage_name: "vercel_deploy",
          gate_id: null,
          position: 3,
          status: "failed" as const,
          progress_pct: 50,
        },
        {
          id: "s4",
          run_id: "r1",
          stage_name: "G7_verification",
          gate_id: "G7",
          position: 4,
          status: "failed" as const,
          progress_pct: 0,
        },
      ];
      expect(getTargetVerificationStatus("docker", "docker_github", stages)).toBe("Failed");
      expect(getTargetVerificationStatus("github", "github_only", stages)).toBe("Failed");
      expect(getTargetVerificationStatus("vercel", "vercel_only", stages)).toBe("Failed");
      expect(getTargetVerificationStatus("vercel", "docker_github", stages)).toBe("Not Applicable");

      // Pending state
      const pendingStages = [
        {
          id: "s5",
          run_id: "r1",
          stage_name: "G7_verification",
          gate_id: "G7",
          position: 1,
          status: "pending" as const,
          progress_pct: 0,
        },
      ];
      expect(getTargetVerificationStatus("docker", "docker_github", pendingStages)).toBe("Pending");

      // Metadata target_results with various values
      const metaStages = [
        {
          id: "s6",
          run_id: "r1",
          stage_name: "G7_verification",
          gate_id: "G7",
          position: 1,
          status: "succeeded" as const,
          progress_pct: 100,
          stage_metadata: {
            target_results: {
              docker: "passed",
              github: "error",
              vercel: "skipped",
            },
          },
        },
      ];
      expect(getTargetVerificationStatus("docker", "docker_github_vercel", metaStages)).toBe(
        "Verified",
      );
      expect(getTargetVerificationStatus("github", "docker_github_vercel", metaStages)).toBe(
        "Failed",
      );
      expect(getTargetVerificationStatus("vercel", "docker_github_vercel", metaStages)).toBe(
        "Not Applicable",
      );
    });

    it("triggers stage selection from log console and fetches run via runQuery queryFn", async () => {
      const user = userEvent.setup();
      get.mockResolvedValueOnce(mockGithubOnlyRun);

      renderWithClient(
        <JenkinsPipelineDashboard
          projectId={projectId}
          runId={mockGithubOnlyRun.id}
          projectName={projectName}
        />,
      );

      // Verify query was called
      await waitFor(() => {
        expect(get).toHaveBeenCalledWith(
          `/projects/${projectId}/autonomous-deploy/${mockGithubOnlyRun.id}`,
        );
      });

      // Filter select in console triggers onSelectStage callback
      await waitFor(() => {
        expect(screen.getByTestId("stage-filter-select")).toBeInTheDocument();
      });
      const stageSelect = screen.getByTestId("stage-filter-select");
      await user.selectOptions(stageSelect, "G1_blueprint");

      // Verify selected stage updates
      await waitFor(() => {
        expect(
          screen.getByRole("heading", { name: "G1 — Blueprint Validation" }),
        ).toBeInTheDocument();
      });
    });

    it("renders R0 baseline readiness card, R1 indicator, and G8 recovery panel", () => {
      const runWithReadiness: AutonomousRunPublicResponse = {
        ...mockFullStackRun,
        configuration: {
          recovery_attempts: 2,
        },
        initial_readiness: {
          id: "cp-r0",
          run_id: runId,
          checkpoint_type: "initial_r0",
          assessment_version: 1,
          status: "ready",
          score: 85,
          max_score: 100,
          selected_strategy: "docker_github_vercel",
          detected_stack: "nodejs",
          dimension_scores: {
            configuration: 25,
            artifacts: 20,
            environment: 20,
            workload_integrity: 20,
          },
          explanation: "Initial readiness score: 85/100 (READY)",
        },
        post_generation_readiness: {
          id: "cp-r1",
          run_id: runId,
          checkpoint_type: "post_generation_r1",
          assessment_version: 1,
          status: "ready",
          score: 95,
          max_score: 100,
          score_delta: 10,
          selected_strategy: "docker_github_vercel",
          explanation: "Post-generation readiness score: 95/100 (READY)",
        },
        readiness_checkpoints: [
          {
            id: "cp-r0",
            run_id: runId,
            checkpoint_type: "initial_r0",
            assessment_version: 1,
            status: "ready",
            score: 85,
            max_score: 100,
            selected_strategy: "docker_github_vercel",
          },
          {
            id: "cp-r1",
            run_id: runId,
            checkpoint_type: "post_generation_r1",
            assessment_version: 1,
            status: "ready",
            score: 95,
            max_score: 100,
            score_delta: 10,
            selected_strategy: "docker_github_vercel",
          },
          {
            id: "cp-rec2",
            run_id: runId,
            checkpoint_type: "recovery_recheck",
            assessment_version: 2,
            status: "completed",
            score: 90,
            max_score: 100,
            selected_strategy: "docker_github_vercel",
            explanation: "G8 AI recovery recheck passed after artifact correction",
          },
        ],
      };

      renderWithClient(
        <JenkinsPipelineDashboard
          projectId={projectId}
          run={runWithReadiness}
          projectName={projectName}
        />,
      );

      // Verify R0 baseline card
      expect(screen.getByTestId("r0-baseline-card")).toBeInTheDocument();
      expect(screen.getByText("85/100")).toBeInTheDocument();
      expect(screen.getByText("R0 Baseline")).toBeInTheDocument();

      // Verify R1 post-generation indicator between G2 and G3
      expect(screen.getByTestId("r1-post-generation-indicator")).toBeInTheDocument();
      expect(screen.getByText("95/100")).toBeInTheDocument();
      expect(screen.getByText("+10 vs R0")).toBeInTheDocument();

      // Verify G8 recovery panel
      expect(screen.getByTestId("g8-recovery-panel")).toBeInTheDocument();
      expect(screen.getByText("Iteration 2/3")).toBeInTheDocument();
      expect(screen.getByText("90/100")).toBeInTheDocument();
      expect(screen.getByText("G8 AI recovery recheck passed after artifact correction")).toBeInTheDocument();
    });

    it("renders progress bar human-readable activity text and status-aware styles across run states", () => {
      // 1. Succeeded run
      const succeededRun: AutonomousRunPublicResponse = {
        ...mockFullStackRun,
        status: "succeeded",
        progress_pct: 100,
      };
      const { unmount: unmount1 } = renderWithClient(
        <JenkinsPipelineDashboard
          projectId={projectId}
          run={succeededRun}
          projectName={projectName}
        />,
      );
      const progressBar1 = screen.getByRole("progressbar");
      expect(progressBar1).toHaveAttribute("aria-valuenow", "100");
      expect(progressBar1).toHaveAttribute("aria-valuetext", "100 percent - Deployment succeeded and verified");
      expect(screen.getByText("Completed — All deployment gates G1–G7 verified & signed")).toBeInTheDocument();
      unmount1();

      // 2. Failed run
      const failedRun: AutonomousRunPublicResponse = {
        ...mockFullStackRun,
        status: "failed",
        progress_pct: 45,
        stages: mockFullStackRun.stages.map((s) =>
          s.gate_id === "G4" ? { ...s, status: "failed" as const } : s,
        ),
      };
      const { unmount: unmount2 } = renderWithClient(
        <JenkinsPipelineDashboard
          projectId={projectId}
          run={failedRun}
          projectName={projectName}
        />,
      );
      const progressBar2 = screen.getByRole("progressbar");
      expect(progressBar2).toHaveAttribute("aria-valuenow", "45");
      expect(screen.getByText(/Failed at G4 — Build & Compile \(45%\)/)).toBeInTheDocument();
      unmount2();

      // 3. Rolled back run
      const rolledBackRun: AutonomousRunPublicResponse = {
        ...mockFullStackRun,
        status: "rolled_back",
        progress_pct: 45,
      };
      const { unmount: unmount3 } = renderWithClient(
        <JenkinsPipelineDashboard
          projectId={projectId}
          run={rolledBackRun}
          projectName={projectName}
        />,
      );
      expect(screen.getByText(/Rolled Back — Compensation cleanup complete \(45%\)/)).toBeInTheDocument();
      unmount3();

      // 4. Cancelled run
      const cancelledRun: AutonomousRunPublicResponse = {
        ...mockFullStackRun,
        status: "cancelled",
        progress_pct: 45,
      };
      const { unmount: unmount4 } = renderWithClient(
        <JenkinsPipelineDashboard
          projectId={projectId}
          run={cancelledRun}
          projectName={projectName}
        />,
      );
      expect(screen.getByText(/Cancelled — Stopped at 45%/)).toBeInTheDocument();
      unmount4();
    });

    it("renders negative score delta when R1 drops below R0", () => {
      const runWithDrop: AutonomousRunPublicResponse = {
        ...mockFullStackRun,
        initial_readiness: {
          id: "cp-r0",
          run_id: runId,
          checkpoint_type: "initial_r0",
          assessment_version: 1,
          status: "ready",
          score: 85,
          max_score: 100,
          selected_strategy: "docker_github_vercel",
        },
        post_generation_readiness: {
          id: "cp-r1",
          run_id: runId,
          checkpoint_type: "post_generation_r1",
          assessment_version: 1,
          status: "ready",
          score: 75,
          max_score: 100,
          selected_strategy: "docker_github_vercel",
        },
      };

      renderWithClient(
        <JenkinsPipelineDashboard
          projectId={projectId}
          run={runWithDrop}
          projectName={projectName}
        />,
      );

      expect(screen.getByTestId("r1-post-generation-indicator")).toBeInTheDocument();
      expect(screen.getByText("75/100")).toBeInTheDocument();
      expect(screen.getByText("-10 vs R0")).toBeInTheDocument();
    });

    it("renders stream reconnection and error banners with authoritative state notification", () => {
      // 1. Reconnecting state
      mockStreamOverrides = {
        isReconnecting: true,
        error: null,
      };

      const { unmount } = renderWithClient(
        <JenkinsPipelineDashboard
          projectId={projectId}
          run={mockFullStackRun}
          projectName={projectName}
        />,
      );

      expect(screen.getByTestId("reconnection-banner")).toBeInTheDocument();
      expect(
        screen.getByText(
          "Live stream disconnected. Reconnecting to deployment event stream... Displaying authoritative database state.",
        ),
      ).toBeInTheDocument();
      unmount();

      // 2. Stream error state
      mockStreamOverrides = {
        isReconnecting: false,
        error: "SSE stream connection lost",
      };

      renderWithClient(
        <JenkinsPipelineDashboard
          projectId={projectId}
          run={mockFullStackRun}
          projectName={projectName}
        />,
      );

      expect(screen.getByTestId("stream-error-banner")).toBeInTheDocument();
      expect(
        screen.getByText(/Stream connection error: SSE stream connection lost/),
      ).toBeInTheDocument();
    });

    it("supports arrow key keyboard navigation across stage columns", async () => {
      const user = userEvent.setup();

      renderWithClient(
        <JenkinsPipelineDashboard
          projectId={projectId}
          run={mockFullStackRun}
          projectName={projectName}
        />,
      );

      const consoleRoot = screen.getByLabelText(
        "Autonomous deployment pipeline console. Use left and right arrow keys to navigate stages.",
      );

      // Focus the dashboard container
      consoleRoot.focus();
      expect(consoleRoot).toHaveFocus();

      // Initially G4 (running stage) is selected
      expect(screen.getByRole("heading", { name: "G4 — Build & Compile" })).toBeInTheDocument();

      // Press ArrowRight to move to G5
      await user.keyboard("{ArrowRight}");
      expect(screen.getByRole("heading", { name: "G5 — Apply & Startup" })).toBeInTheDocument();

      // Press ArrowLeft to navigate back to G4
      await user.keyboard("{ArrowLeft}");
      expect(screen.getByRole("heading", { name: "G4 — Build & Compile" })).toBeInTheDocument();
    });
  });
});
