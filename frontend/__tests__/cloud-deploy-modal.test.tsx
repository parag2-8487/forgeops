// SPDX-License-Identifier: FSL-1.1-ALv2
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CloudDeployModal } from "@/features/integrations/CloudDeployModal";
import { ApiProblemError } from "@/lib/api";

const get = vi.fn();
const post = vi.fn();
const put = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
      put: (...args: unknown[]) => put(...args),
      delete: vi.fn(),
    },
  };
});

describe("CloudDeployModal", () => {
  const projectId = "11111111-1111-4111-8111-111111111111";
  const projectName = "My Awesome App";

  function renderWithClient(ui: React.ReactElement) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
  }

  beforeEach(() => {
    vi.clearAllMocks();
    get.mockImplementation((url: string) => {
      if (url === "/integrations/github") {
        return Promise.resolve({ connected: true, login: "test-user" });
      }
      if (url.includes("/integrations/github/repositories")) {
        return Promise.resolve({
          items: [
            {
              full_name: "test-user/my-existing-repo",
              owner: "test-user",
              name: "my-existing-repo",
              private: false,
              default_branch: "main",
              language: "TypeScript",
              pushed_at: "2026-10-01T00:00:00Z",
              clone_url: "https://github.com/test-user/my-existing-repo.git",
              html_url: "https://github.com/test-user/my-existing-repo",
              size_kb: 100,
              archived: false,
            },
          ],
          total: 1,
          page: 1,
          per_page: 10,
          truncated: false,
        });
      }
      if (url.includes("/vercel/config-check")) {
        return Promise.resolve({
          framework: "vite",
          framework_display: "Vite (SPA)",
          has_vercel_json: false,
          needs_vercel_json: true,
          reason: "SPA requires client-side routing rewrites",
          suggested_vercel_json: { rewrites: [{ source: "/(.*)", destination: "/index.html" }] },
        });
      }
      return Promise.resolve({});
    });
  });

  it("does not render when closed", () => {
    renderWithClient(
      <CloudDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={false}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders when open and displays GitHub tab by default", async () => {
    renderWithClient(
      <CloudDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /export and deploy codebase/i }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/@test-user/i)).toBeInTheDocument();
      expect(screen.getByText(/ready to push/i)).toBeInTheDocument();
    });
  });

  it("handles unlinked GitHub token quick-connect", async () => {
    const user = userEvent.setup();
    get.mockImplementation((url: string) => {
      if (url === "/integrations/github") {
        return Promise.resolve({ connected: false });
      }
      return Promise.resolve({});
    });
    put.mockResolvedValue({});

    renderWithClient(
      <CloudDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/github not linked/i)).toBeInTheDocument();
    });

    const tokenPrefix = ["gh", "p_"].join("");
    const sampleToken = `${tokenPrefix}1234567890abcdef`;
    const tokenInput = screen.getByPlaceholderText(new RegExp(tokenPrefix, "i"));
    await user.type(tokenInput, sampleToken);
    await user.click(screen.getByRole("button", { name: /link github/i }));

    expect(put).toHaveBeenCalledWith("/integrations/github/token", {
      token: sampleToken,
    });
  });

  it("pushes a new GitHub repository with custom parameters and visibility", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({
      status: "pushed",
      repo_full_name: "test-user/custom-app",
      repo_url: "https://github.com/test-user/custom-app",
      branch: "release",
      commit_sha: "abcdef1234567890",
      files_count: 5,
    });

    renderWithClient(
      <CloudDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/@test-user/i)).toBeInTheDocument();
    });

    // Toggle to public
    await user.click(screen.getByRole("button", { name: /^public$/i }));

    // Modify name, description, branch, commit message
    const repoInput = screen.getByPlaceholderText(/e\.g\. portfolio/i);
    await user.clear(repoInput);
    await user.type(repoInput, "custom-app");

    const descInput = screen.getByPlaceholderText(/repository description/i);
    await user.clear(descInput);
    await user.type(descInput, "Custom description");

    const branchInput = screen.getByPlaceholderText(/main/i);
    await user.clear(branchInput);
    await user.type(branchInput, "release");

    const commitInput = screen.getByPlaceholderText(/deploy commit message/i);
    await user.clear(commitInput);
    await user.type(commitInput, "Initial push");

    const pushButton = screen.getByRole("button", { name: /push to github/i });
    await user.click(pushButton);

    expect(post).toHaveBeenCalledWith(
      `/projects/${projectId}/github/push`,
      expect.objectContaining({
        mode: "new",
        new_repo_name: "custom-app",
        new_repo_description: "Custom description",
        new_repo_private: false,
        branch: "release",
        commit_message: "Initial push",
      }),
    );

    await waitFor(() => {
      expect(screen.getByText(/successfully pushed to github/i)).toBeInTheDocument();
      expect(screen.getByText(/open repository on github/i)).toBeInTheDocument();
    });
  });

  it("pushes to an existing GitHub repository", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({
      status: "pushed",
      repo_full_name: "test-user/my-existing-repo",
      repo_url: "https://github.com/test-user/my-existing-repo",
      branch: "main",
      commit_sha: "abcdef1234567890",
      files_count: 3,
    });

    renderWithClient(
      <CloudDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/@test-user/i)).toBeInTheDocument();
    });

    // Select existing repo mode
    await user.click(screen.getByRole("button", { name: /push to existing repository/i }));

    // Click repository from picker
    await waitFor(() => {
      expect(screen.getByText(/my-existing-repo/i)).toBeInTheDocument();
    });
    await user.click(screen.getByText(/my-existing-repo/i));

    const pushButton = screen.getByRole("button", { name: /push to github/i });
    await user.click(pushButton);

    expect(post).toHaveBeenCalledWith(
      `/projects/${projectId}/github/push`,
      expect.objectContaining({
        mode: "existing",
        repo_full_name: "test-user/my-existing-repo",
      }),
    );
  });

  it("handles GitHub push errors", async () => {
    const user = userEvent.setup();
    post.mockRejectedValue(
      new ApiProblemError({
        type: "https://forgeops.dev/problems/push-error",
        title: "Push Error",
        detail: "Repository already exists",
        status: 409,
      }),
    );

    renderWithClient(
      <CloudDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /push to github/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /push to github/i }));

    await waitFor(() => {
      expect(screen.getByText(/repository already exists/i)).toBeInTheDocument();
    });
  });

  it("switches to Vercel tab and deploys with autoConfigSpa toggle", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({
      status: "deployed",
      deployment_id: "dpl_123",
      url: "https://my-app.vercel.app",
      inspector_url: "https://vercel.com/inspect",
      ready_state: "READY",
      framework: "vite",
      configured_vercel_json: true,
    });

    renderWithClient(
      <CloudDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /vercel deployment/i }));

    await waitFor(() => {
      expect(screen.getByText(/auto-generate spa rewrite rules/i)).toBeInTheDocument();
    });

    // Check SPA checkbox toggle
    const spaCheckbox = screen.getByRole("checkbox");
    expect(spaCheckbox).toBeChecked();
    await user.click(spaCheckbox);
    expect(spaCheckbox).not.toBeChecked();

    const tokenInput = screen.getByPlaceholderText(/paste vercel token/i);
    await user.type(tokenInput, "vck_token123");

    const projectNameInput = screen.getByPlaceholderText(/e\.g\. my-project/i);
    await user.clear(projectNameInput);
    await user.type(projectNameInput, "custom-vercel-app");

    const deployButton = screen.getByRole("button", { name: /deploy to vercel/i });
    await user.click(deployButton);

    expect(post).toHaveBeenCalledWith(
      `/projects/${projectId}/vercel/deploy`,
      expect.objectContaining({
        vercel_token: "vck_token123",
        project_name: "custom-vercel-app",
        auto_configure_spa: false,
      }),
    );

    await waitFor(() => {
      expect(screen.getByText(/live deployment created!/i)).toBeInTheDocument();
      expect(screen.getByText(/open live deployment/i)).toBeInTheDocument();
    });
  });

  it("handles Vercel deploy errors", async () => {
    const user = userEvent.setup();
    post.mockRejectedValue(
      new ApiProblemError({
        type: "https://forgeops.dev/problems/unauthorized",
        title: "Unauthorized",
        detail: "Invalid Vercel token",
        status: 401,
      }),
    );

    renderWithClient(
      <CloudDeployModal
        projectId={projectId}
        projectName={projectName}
        isOpen={true}
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /vercel deployment/i }));
    const tokenInput = screen.getByPlaceholderText(/paste vercel token/i);
    await user.type(tokenInput, "bad_token");

    await user.click(screen.getByRole("button", { name: /deploy to vercel/i }));

    await waitFor(() => {
      expect(screen.getByText(/invalid vercel token/i)).toBeInTheDocument();
    });
  });
});
