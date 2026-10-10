// SPDX-License-Identifier: FSL-1.1-ALv2
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockPush = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
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

import { AutonomousDeployModal } from "@/features/deployments/AutonomousDeployModal";

describe("AutonomousDeployModal", () => {
  const projectId = "11111111-1111-4111-8111-111111111111";
  const projectName = "SuperApp";

  function renderWithClient(ui: React.ReactElement) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
  }

  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue({
      devices: [
        {
          id: "device-1",
          project_id: projectId,
          status: "active",
          agent_version: "1.4.0",
          platform: "linux-x86_64",
          last_seen: new Date().toISOString(),
          seconds_since_last_seen: 5,
          heartbeat_fresh: true,
        },
      ],
      next_cursor: null,
    });
  });

  it("does not render when isOpen is false", () => {
    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={false}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders all 4 strategy cards when open", async () => {
    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByTestId("strategy-docker_github_vercel")).toBeInTheDocument();
    expect(screen.getByTestId("strategy-docker_github")).toBeInTheDocument();
    expect(screen.getByTestId("strategy-github_only")).toBeInTheDocument();
    expect(screen.getByTestId("strategy-vercel_only")).toBeInTheDocument();

    expect(screen.getByText("Full Stack (Docker + GitHub + Vercel)")).toBeInTheDocument();
    expect(screen.getByText("Docker + GitHub")).toBeInTheDocument();
    expect(screen.getByText("GitHub Only")).toBeInTheDocument();
    expect(screen.getByText("Vercel Only")).toBeInTheDocument();
  });

  it("shows and hides dynamic form fields when switching strategies", async () => {
    const user = userEvent.setup();
    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    // Default strategy: docker_github_vercel (shows GitHub, Vercel, Docker)
    expect(screen.getByTestId("github-config-section")).toBeInTheDocument();
    expect(screen.getByTestId("vercel-config-section")).toBeInTheDocument();
    expect(screen.getByTestId("docker-config-section")).toBeInTheDocument();

    // Switch to docker_github
    await user.click(screen.getByTestId("strategy-docker_github"));
    expect(screen.getByTestId("github-config-section")).toBeInTheDocument();
    expect(screen.getByTestId("docker-config-section")).toBeInTheDocument();
    expect(screen.queryByTestId("vercel-config-section")).not.toBeInTheDocument();

    // Switch to github_only
    await user.click(screen.getByTestId("strategy-github_only"));
    expect(screen.getByTestId("github-config-section")).toBeInTheDocument();
    expect(screen.queryByTestId("docker-config-section")).not.toBeInTheDocument();
    expect(screen.queryByTestId("vercel-config-section")).not.toBeInTheDocument();

    // Switch to vercel_only
    await user.click(screen.getByTestId("strategy-vercel_only"));
    expect(screen.getByTestId("vercel-config-section")).toBeInTheDocument();
    expect(screen.queryByTestId("github-config-section")).not.toBeInTheDocument();
    expect(screen.queryByTestId("docker-config-section")).not.toBeInTheDocument();
  });

  it("displays agent pairing banner when connected agent is detected", async () => {
    get.mockResolvedValue({
      devices: [
        {
          id: "device-123",
          project_id: projectId,
          status: "active",
          agent_version: "2.1.0",
          platform: "darwin-arm64",
          last_seen: new Date().toISOString(),
          seconds_since_last_seen: 4,
          heartbeat_fresh: true,
        },
      ],
      next_cursor: null,
    });

    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Agent Connected")).toBeInTheDocument();
    });
    expect(
      screen.getByText(/Local project workspace is accessible for execution/i),
    ).toBeInTheDocument();
  });

  it("displays agent pairing banner when agent is disconnected and clarifies requirement", async () => {
    get.mockResolvedValue({
      devices: [],
      next_cursor: null,
    });

    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Agent Disconnected")).toBeInTheDocument();
    });
    expect(
      screen.getByText(
        /An active connected agent is required to access local project files during execution/i,
      ),
    ).toBeInTheDocument();
  });

  it("submits run creation request and navigates to the dedicated pipeline page on success", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const runId = "22222222-2222-4222-8222-222222222222";

    post.mockResolvedValue({
      id: runId,
      project_id: projectId,
      status: "pending",
      strategy: "docker_github_vercel",
      attempt_number: 1,
    });

    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={onClose}
      />,
    );

    const submitBtn = screen.getByTestId("create-run-button");
    expect(submitBtn).toBeInTheDocument();
    expect(submitBtn).not.toBeDisabled();

    await user.click(submitBtn);

    await waitFor(() => {
      expect(post).toHaveBeenCalledTimes(1);
    });

    const [calledPath, calledPayload] = post.mock.calls[0];
    expect(calledPath).toBe(`/projects/${projectId}/autonomous-deploy`);
    expect(calledPayload).toMatchObject({
      strategy: "docker_github_vercel",
      github_config: {
        repository_mode: "new_private",
        repository_name: "superapp",
        target_branch: "main",
        commit_message: "Automated deployment by ForgeOps",
      },
      vercel_config: {
        project_name: "superapp",
        production_deploy: true,
      },
      docker_config: {
        port_bindings: { "8080": 8080 },
      },
    });
    expect(calledPayload.idempotency_key).toBeDefined();

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(mockPush).toHaveBeenCalledWith(`/projects/${projectId}/autonomous-deploy/${runId}`);
  });

  it("handles idempotent 200 response by routing to dedicated pipeline page", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const existingRunId = "33333333-3333-4333-8333-333333333333";

    post.mockResolvedValue({
      id: existingRunId,
      project_id: projectId,
      status: "pending",
      strategy: "github_only",
      attempt_number: 1,
    });

    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={onClose}
      />,
    );

    await user.click(screen.getByTestId("strategy-github_only"));
    await user.click(screen.getByTestId("create-run-button"));

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith(
        `/projects/${projectId}/autonomous-deploy/${existingRunId}`,
      );
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("disables submit button and shows loading indicator while creation is in flight", async () => {
    const user = userEvent.setup();
    let resolvePost: (value: unknown) => void;
    post.mockReturnValue(
      new Promise((res) => {
        resolvePost = res;
      }),
    );

    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    const submitBtn = screen.getByTestId("create-run-button");
    await user.click(submitBtn);

    expect(screen.getByText(/Creating Run\.\.\./i)).toBeInTheDocument();
    expect(submitBtn).toBeDisabled();

    // Resolve promise to clean up
    resolvePost!({
      id: "run-async-test",
      project_id: projectId,
      status: "pending",
    });
  });

  it("displays error banner when submission fails", async () => {
    const user = userEvent.setup();
    post.mockRejectedValue(new Error("Database connection timed out"));

    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByTestId("create-run-button"));

    await waitFor(() => {
      expect(screen.getByTestId("autonomous-deploy-error")).toBeInTheDocument();
    });
    expect(screen.getByText("Database connection timed out")).toBeInTheDocument();
  });

  it("submits run creation request with pull_request publishing mode", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const runId = "44444444-4444-4444-8444-444444444444";

    post.mockResolvedValue({
      id: runId,
      project_id: projectId,
      status: "pending",
      strategy: "docker_github_vercel",
      attempt_number: 1,
    });

    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={onClose}
      />,
    );

    // Click Pull Request publishing mode button
    const prModeBtn = screen.getByTestId("publishing-mode-pull-request");
    await user.click(prModeBtn);

    const submitBtn = screen.getByTestId("create-run-button");
    await user.click(submitBtn);

    await waitFor(() => {
      expect(post).toHaveBeenCalledTimes(1);
    });

    const [, calledPayload] = post.mock.calls[0];
    expect(calledPayload).toMatchObject({
      strategy: "docker_github_vercel",
      github_config: {
        publishing_mode: "pull_request",
      },
    });
  });

  it("discovers branches and highlights default branch with badge and truncation banner", async () => {
    const user = userEvent.setup();
    get.mockImplementation((path: string) => {
      if (path.includes("/integrations/github/repositories/")) {
        return Promise.resolve({
          owner: "testorg",
          repo: "testrepo",
          default_branch: "production",
          branches: ["production", "staging", "dev"],
          can_push: true,
          is_private: false,
          truncated: true,
        });
      }
      return Promise.resolve({ devices: [], next_cursor: null });
    });

    renderWithClient(
      <AutonomousDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    // Switch repo to testorg/testrepo
    const repoInput = screen.getByLabelText("Repository Name");
    await user.clear(repoInput);
    await user.type(repoInput, "testorg/testrepo");

    await waitFor(() => {
      expect(screen.getByText("Default Branch")).toBeInTheDocument();
    });

    expect(screen.getByText(/Repository contains >1,000 branches; listing capped at 1,000/i)).toBeInTheDocument();
  });
});
