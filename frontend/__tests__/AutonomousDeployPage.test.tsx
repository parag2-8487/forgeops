// SPDX-License-Identifier: FSL-1.1-ALv2
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockPush = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
  useParams: () => ({ projectId: "11111111-1111-4111-8111-111111111111" }),
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

import AutonomousDeployPage from "@/app/(shell)/projects/[projectId]/autonomous-deploy/page";
import ProjectDetailPage from "@/app/(shell)/projects/[projectId]/page";
import { AutonomousDeployForm } from "@/features/deployments/AutonomousDeployForm";
import { validateGitBranchName } from "@/lib/git-validation";

describe("Autonomous Deploy UX Improvements", () => {
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
    get.mockImplementation((url: string) => {
      if (url === `/projects/${projectId}`) {
        return Promise.resolve({
          id: projectId,
          name: projectName,
          created_at: new Date().toISOString(),
        });
      }
      if (url.startsWith("/agents/devices")) {
        return Promise.resolve({
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
      }
      if (url.startsWith("/integrations/github/repositories?")) {
        return Promise.resolve({
          items: [
            {
              full_name: "parag2-8487/test",
              owner: "parag2-8487",
              name: "test",
              private: true,
              default_branch: "main",
              language: "TypeScript",
              pushed_at: new Date().toISOString(),
              clone_url: "https://github.com/parag2-8487/test.git",
              html_url: "https://github.com/parag2-8487/test",
              size_kb: 1024,
              archived: false,
            },
            {
              full_name: "parag2-8487/infra",
              owner: "parag2-8487",
              name: "infra",
              private: false,
              default_branch: "master",
              language: "Go",
              pushed_at: new Date().toISOString(),
              clone_url: "https://github.com/parag2-8487/infra.git",
              html_url: "https://github.com/parag2-8487/infra",
              size_kb: 512,
              archived: false,
            },
          ],
          total: 2,
          page: 1,
          per_page: 50,
          truncated: false,
        });
      }
      if (url.includes("/branches")) {
        return Promise.resolve({
          owner: "parag2-8487",
          repo: "test",
          default_branch: "main",
          branches: ["main", "staging", "feat/autonomous-deploy"],
          can_push: true,
          is_private: true,
          truncated: false,
        });
      }
      if (url.endsWith("/readiness")) {
        return Promise.resolve({ score: 90, level: "green" });
      }
      if (url.includes("/analysis/codebase/")) {
        return Promise.resolve({ status: "indexed" });
      }
      if (url.startsWith("/approvals")) {
        return Promise.resolve({ change_sets: [] });
      }
      if (url.startsWith("/secrets")) {
        return Promise.resolve([]);
      }
      if (url.endsWith("/activity")) {
        return Promise.resolve([]);
      }
      if (url === "/integrations/github") {
        return Promise.resolve({
          configured: true,
          connected: true,
          login: "parag2-8487",
        });
      }
      if (url.startsWith("/integrations/github/repositories/check-availability")) {
        const u = new URL("http://localhost" + url);
        const name = u.searchParams.get("name") || "superapp";
        const owner = u.searchParams.get("owner") || "parag2-8487";
        return Promise.resolve({
          available: true,
          owner,
          name,
          full_name: `${owner}/${name}`,
          status: "available",
          message: `Repository name '${name}' is available.`,
        });
      }
      return Promise.resolve({});
    });

    post.mockResolvedValue({
      id: "run-99999999-9999-4999-8999-999999999999",
      project_id: projectId,
      status: "running",
      strategy: "docker_github_vercel",
      attempt_number: 1,
    });
  });

  it("project detail page has an Autonomous Deploy link pointing to the dedicated route", async () => {
    renderWithClient(<ProjectDetailPage />);
    const deployLink = await screen.findByRole("link", { name: "Autonomous Deploy" });
    expect(deployLink).toBeInTheDocument();
    expect(deployLink).toHaveAttribute("href", `/projects/${projectId}/autonomous-deploy`);
  });

  it("renders dedicated page with breadcrumb, header, and strategy sections", async () => {
    renderWithClient(<AutonomousDeployPage />);

    // Header and breadcrumb
    expect(
      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Projects")).toBeInTheDocument();
    expect(screen.getAllByText(projectName).length).toBeGreaterThanOrEqual(1);

    // Sections
    expect(screen.getByTestId("agent-pairing-banner")).toBeInTheDocument();
    expect(screen.getByTestId("strategy-docker_github_vercel")).toBeInTheDocument();
    expect(screen.getByTestId("strategy-docker_github")).toBeInTheDocument();
    expect(screen.getByTestId("strategy-github_only")).toBeInTheDocument();
    expect(screen.getByTestId("strategy-vercel_only")).toBeInTheDocument();
    expect(screen.getByTestId("github-config-section")).toBeInTheDocument();
    expect(screen.getByTestId("vercel-config-section")).toBeInTheDocument();
    expect(screen.getByTestId("docker-config-section")).toBeInTheDocument();
  });

  it("fetches and displays repositories automatically when selecting Existing Repository mode", async () => {
    const user = userEvent.setup();
    renderWithClient(<AutonomousDeployPage />);

    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    // Switch to Existing Repository mode
    const existingModeBtn = screen.getByTestId("repo-mode-existing");
    await user.click(existingModeBtn);

    // Should render the repository combobox container
    const comboboxContainer = await screen.findByTestId("repo-combobox-container");
    expect(comboboxContainer).toBeInTheDocument();

    // Verify GET /integrations/github/repositories was queried
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith(
        expect.stringContaining("/integrations/github/repositories"),
      );
    });

    // Toggle dropdown open
    const toggleBtn = screen.getByLabelText("Toggle repository dropdown");
    await user.click(toggleBtn);

    // List items should be rendered
    const testRepoOption = await screen.findByTestId("repo-select-parag2-8487/test");
    expect(testRepoOption).toBeInTheDocument();
    expect(screen.getByText("parag2-8487/infra")).toBeInTheDocument();

    // Select repository
    await user.click(testRepoOption);

    // Branch query should have been triggered for parag2-8487/test
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith(
        expect.stringContaining("/integrations/github/repositories/parag2-8487/test/branches"),
      );
    });

    // Default branch "main" should be automatically selected with badge
    const branchInput = screen.getByTestId("github-branch-input") as HTMLInputElement;
    await waitFor(() => {
      expect(branchInput.value).toBe("main");
    });
    expect(screen.getByText("Default Branch: main")).toBeInTheDocument();
  });

  it("handles repository change by clearing branch selection and querying new branches", async () => {
    const user = userEvent.setup();
    renderWithClient(<AutonomousDeployPage />);

    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    // Switch to Existing Repository mode
    await user.click(screen.getByTestId("repo-mode-existing"));
    await user.click(screen.getByLabelText("Toggle repository dropdown"));

    // Select first repo
    await user.click(await screen.findByTestId("repo-select-parag2-8487/test"));

    // Now mock second repo branches with different default branch
    get.mockImplementation((url: string) => {
      if (url.includes("/infra/branches")) {
        return Promise.resolve({
          owner: "parag2-8487",
          repo: "infra",
          default_branch: "master",
          branches: ["master", "dev"],
          can_push: true,
          is_private: false,
          truncated: false,
        });
      }
      return Promise.resolve({ items: [] });
    });

    // Open dropdown again and select infra repo
    await user.click(screen.getByLabelText("Toggle repository dropdown"));
    await user.click(await screen.findByTestId("repo-select-parag2-8487/infra"));

    // Verify branch changed to "master"
    const branchInput = screen.getByTestId("github-branch-input") as HTMLInputElement;
    await waitFor(() => {
      expect(branchInput.value).toBe("master");
    });
    expect(screen.getByText("Default Branch: master")).toBeInTheDocument();
  });

  it("allows toggling manual repository entry as explicit fallback", async () => {
    const user = userEvent.setup();
    renderWithClient(<AutonomousDeployPage />);

    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });
    await user.click(screen.getByTestId("repo-mode-existing"));

    // Click "Enter repository manually"
    const manualBtn = screen.getByRole("button", { name: "Enter repository manually" });
    await user.click(manualBtn);

    // Now plain text input is shown
    const repoInput = screen.getByTestId("github-repo-input") as HTMLInputElement;
    expect(repoInput).toBeInTheDocument();

    await user.clear(repoInput);
    await user.type(repoInput, "custom-org/custom-repo");
    expect(repoInput.value).toBe("custom-org/custom-repo");

    // Can toggle back to picker
    const pickBtn = screen.getByRole("button", { name: "Pick from account list" });
    await user.click(pickBtn);
    expect(screen.getByTestId("repo-combobox-container")).toBeInTheDocument();
  });

  it("supports Direct Push versus Create Pull Request mode distinction in existing repo mode", async () => {
    const user = userEvent.setup();
    renderWithClient(<AutonomousDeployPage />);

    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    // Switch to Existing Repository mode where PR is available
    await user.click(screen.getByTestId("repo-mode-existing"));

    // Initial default is Direct Push
    expect(screen.getByText("Target Branch (Direct Push)")).toBeInTheDocument();

    // Toggle to Create Pull Request
    const prBtn = screen.getByTestId("publishing-mode-pull-request");
    await user.click(prBtn);

    // Label changes to Base Branch
    expect(screen.getByText("Base Branch (Destination for PR)")).toBeInTheDocument();
    expect(screen.getByText(/forgeops\/deploy-<run_id>/)).toBeInTheDocument();

    // Toggle back
    const dpBtn = screen.getByTestId("publishing-mode-direct-push");
    await user.click(dpBtn);
    expect(screen.getByText("Target Branch (Direct Push)")).toBeInTheDocument();
  });

  it("disables Create Pull Request when New Repository is selected and shows explanation", async () => {
    const user = userEvent.setup();
    renderWithClient(<AutonomousDeployPage />);

    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    // By default in New Repository mode, PR button is disabled
    const prBtn = screen.getByTestId("publishing-mode-pull-request");
    expect(prBtn).toBeDisabled();
    expect(
      screen.getAllByText(
        "Pull requests require an existing repository and an existing base branch.",
      ).length,
    ).toBeGreaterThanOrEqual(1);

    // Clicking disabled button does not change mode
    await user.click(prBtn);
    expect(screen.getByTestId("publishing-mode-direct-push")).toHaveClass("border-primary");
  });

  it("disables Create Pull Request when New Branch is selected and automatically falls back to Direct Push", async () => {
    const user = userEvent.setup();
    renderWithClient(<AutonomousDeployPage />);

    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    // Switch to Existing Repository mode
    await user.click(screen.getByTestId("repo-mode-existing"));

    const prBtn = screen.getByTestId("publishing-mode-pull-request");
    expect(prBtn).toBeEnabled();

    // Select Pull Request mode
    await user.click(prBtn);
    expect(screen.getByText("Base Branch (Destination for PR)")).toBeInTheDocument();

    // Now switch branch mode to New Branch
    const newBranchBtn = screen.getByTestId("branch-mode-new");
    await user.click(newBranchBtn);

    // PR button must now be disabled and auto-fallback to Direct Push occurred
    expect(prBtn).toBeDisabled();
    expect(screen.getByTestId("publishing-mode-direct-push")).toHaveClass("border-primary");
    expect(
      screen.getAllByText(
        "Pull requests require an existing repository and an existing base branch.",
      ).length,
    ).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("New Target Branch Name")).toBeInTheDocument();
  });

  it("auto-falls back to Direct Push when switching from PR mode to New Repository", async () => {
    const user = userEvent.setup();
    renderWithClient(<AutonomousDeployPage />);

    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    // Switch to Existing Repository mode and select PR
    await user.click(screen.getByTestId("repo-mode-existing"));
    const prBtn = screen.getByTestId("publishing-mode-pull-request");
    await user.click(prBtn);
    expect(prBtn).toHaveClass("border-primary");

    // Now switch to New Repository mode
    await user.click(screen.getByTestId("repo-mode-new-private"));

    // Must be disabled and fallen back to Direct Push
    expect(prBtn).toBeDisabled();
    expect(screen.getByTestId("publishing-mode-direct-push")).toHaveClass("border-primary");
  });

  it("supports searchable branch combobox, selecting branches with slashes, and keyboard navigation", async () => {
    const user = userEvent.setup();
    // Return branches including slash-containing branch
    get.mockImplementation((url: string) => {
      if (url === `/projects/${projectId}`) {
        return Promise.resolve({
          id: projectId,
          name: projectName,
          created_at: new Date().toISOString(),
        });
      }
      if (url.includes("/branches")) {
        return Promise.resolve({
          owner: "parag2-8487",
          repo: "test",
          default_branch: "main",
          branches: ["main", "release/2.x", "feat/auth-v2", "fix/crash"],
          can_push: true,
          is_private: true,
          truncated: false,
        });
      }
      if (url.startsWith("/integrations/github/repositories?")) {
        return Promise.resolve({
          items: [
            {
              full_name: "parag2-8487/test",
              owner: "parag2-8487",
              name: "test",
              private: true,
              default_branch: "main",
            },
          ],
        });
      }
      return Promise.resolve({});
    });

    renderWithClient(<AutonomousDeployPage />);
    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    // Switch to Existing Repository mode and select repo
    await user.click(screen.getByTestId("repo-mode-existing"));
    await user.click(screen.getByLabelText("Toggle repository dropdown"));
    await user.click(await screen.findByTestId("repo-select-parag2-8487/test"));

    // Combobox input should be present and auto-selected default branch "main"
    const branchInput = (await screen.findByTestId("github-branch-input")) as HTMLInputElement;
    await waitFor(() => {
      expect(branchInput.value).toBe("main");
    });
    expect(screen.getByTestId("default-branch-badge")).toHaveTextContent("Default Branch: main");

    // Open branch dropdown via chevron button
    const toggleBtn = screen.getByLabelText("Toggle branch dropdown");
    await user.click(toggleBtn);

    // Dropdown list should be visible with branches including slashes
    expect(await screen.findByTestId("branch-dropdown-menu")).toBeInTheDocument();
    const slashBranchOption = screen.getByTestId("branch-select-release/2.x");
    expect(slashBranchOption).toBeInTheDocument();
    expect(screen.getByText("release/2.x")).toBeInTheDocument();

    // Select the branch with slashes
    await user.click(slashBranchOption);
    expect(branchInput.value).toBe("release/2.x");
    expect(screen.queryByTestId("branch-dropdown-menu")).not.toBeInTheDocument();

    // Test keyboard navigation: open and navigate with ArrowDown and Enter
    await user.click(toggleBtn);
    expect(await screen.findByTestId("branch-dropdown-menu")).toBeInTheDocument();
    fireEvent.keyDown(branchInput, { key: "ArrowDown" });
    fireEvent.keyDown(branchInput, { key: "Enter" });
    expect(branchInput.value).toBe("main");

    // Pressing Escape closes dropdown
    await user.click(toggleBtn);
    expect(await screen.findByTestId("branch-dropdown-menu")).toBeInTheDocument();
    fireEvent.keyDown(branchInput, { key: "Escape" });
    expect(screen.queryByTestId("branch-dropdown-menu")).not.toBeInTheDocument();
  });

  it("handles branch truncation warning for repositories with >1,000 branches", async () => {
    const user = userEvent.setup();
    get.mockImplementation((url: string) => {
      if (url === `/projects/${projectId}`) {
        return Promise.resolve({
          id: projectId,
          name: projectName,
          created_at: new Date().toISOString(),
        });
      }
      if (url.includes("/branches")) {
        return Promise.resolve({
          owner: "parag2-8487",
          repo: "huge-repo",
          default_branch: "main",
          branches: ["main", "branch-1"],
          can_push: true,
          is_private: true,
          truncated: true,
        });
      }
      if (url.startsWith("/integrations/github/repositories?")) {
        return Promise.resolve({
          items: [
            {
              full_name: "parag2-8487/huge-repo",
              owner: "parag2-8487",
              name: "huge-repo",
              private: true,
              default_branch: "main",
            },
          ],
        });
      }
      return Promise.resolve({});
    });

    renderWithClient(<AutonomousDeployPage />);
    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    await user.click(screen.getByTestId("repo-mode-existing"));
    await user.click(screen.getByLabelText("Toggle repository dropdown"));
    await user.click(await screen.findByTestId("repo-select-parag2-8487/huge-repo"));

    // Open branch dropdown
    await user.click(screen.getByLabelText("Toggle branch dropdown"));

    // Truncation notice should be displayed
    const truncatedNotice = await screen.findByTestId("branches-truncated-notice");
    expect(truncatedNotice).toHaveTextContent(
      "Repository contains >1,000 branches; listing capped at 1,000.",
    );
  });

  it("enforces strict Git branch validation and disables submission when invalid", async () => {
    const user = userEvent.setup();
    renderWithClient(<AutonomousDeployPage />);

    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    const branchInput = screen.getByTestId("github-branch-input");
    const submitBtn = screen.getByTestId("create-run-button");

    // Type branch with whitespace
    await user.clear(branchInput);
    await user.type(branchInput, "feature branch");

    const errorMsg = await screen.findByTestId("branch-validation-error");
    expect(errorMsg).toHaveTextContent("whitespace is forbidden");
    expect(submitBtn).toBeDisabled();

    // Type branch starting with hyphen
    await user.clear(branchInput);
    await user.type(branchInput, "-invalid");
    expect(await screen.findByTestId("branch-validation-error")).toHaveTextContent(
      "cannot begin with a hyphen",
    );
    expect(submitBtn).toBeDisabled();

    // Valid branch clears error and enables submit
    await user.clear(branchInput);
    await user.type(branchInput, "feature/valid-branch");
    expect(screen.queryByTestId("branch-validation-error")).not.toBeInTheDocument();
    expect(submitBtn).toBeEnabled();
  });

  it("successfully submits deployment and navigates to the run dashboard", async () => {
    const user = userEvent.setup();
    renderWithClient(<AutonomousDeployPage />);

    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    const submitBtn = screen.getByTestId("create-run-button");
    await waitFor(() => {
      expect(screen.getByTestId("repo-availability-available")).toBeInTheDocument();
      expect(submitBtn).toBeEnabled();
    });
    await user.click(submitBtn);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        `/projects/${projectId}/autonomous-deploy`,
        expect.objectContaining({
          strategy: "docker_github_vercel",
          idempotency_key: expect.any(String),
          github_config: expect.objectContaining({
            publishing_mode: "direct_push",
            repository_name: "parag2-8487/superapp",
          }),
        }),
      );
    });

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith(
        `/projects/${projectId}/autonomous-deploy/run-99999999-9999-4999-8999-999999999999`,
      );
    });
  });

  it("displays server error banner if autonomous deploy submission fails", async () => {
    const user = userEvent.setup();
    post.mockRejectedValue(new Error("Upstream GitHub rate limit exceeded."));

    renderWithClient(<AutonomousDeployPage />);
    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

    const submitBtn = screen.getByTestId("create-run-button");
    await waitFor(() => {
      expect(screen.getByTestId("repo-availability-available")).toBeInTheDocument();
      expect(submitBtn).toBeEnabled();
    });
    await user.click(submitBtn);

    const errorAlert = await screen.findByTestId("autonomous-deploy-error");
    expect(errorAlert).toHaveTextContent("Upstream GitHub rate limit exceeded.");
  });

  it("cancel button navigates back to project detail page", async () => {
    const user = userEvent.setup();
    renderWithClient(<AutonomousDeployPage />);

    await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });
    const cancelBtn = screen.getByRole("button", { name: "Cancel" });
    await user.click(cancelBtn);

    expect(mockPush).toHaveBeenCalledWith(`/projects/${projectId}`);
  });

  describe("New GitHub Repository Creation and Availability Flow", () => {
    it("resolves connected GitHub username and displays repository owner indicator", async () => {
      renderWithClient(<AutonomousDeployPage />);

      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      const ownerIndicator = await screen.findByTestId("repo-owner-indicator");
      expect(ownerIndicator).toHaveTextContent("Repository owner:");
      expect(ownerIndicator).toHaveTextContent("@parag2-8487");
    });

    it("displays warning and disables submission when GitHub account is not connected", async () => {
      get.mockImplementation((url: string) => {
        if (url === `/projects/${projectId}`) {
          return Promise.resolve({
            id: projectId,
            name: projectName,
            created_at: new Date().toISOString(),
          });
        }
        if (url === "/integrations/github") {
          return Promise.resolve({ configured: true, connected: false, login: null });
        }
        if (url.startsWith("/agents/devices")) {
          return Promise.resolve({ devices: [], next_cursor: null });
        }
        return Promise.resolve({});
      });

      renderWithClient(<AutonomousDeployPage />);
      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      const warning = await screen.findByTestId("github-not-connected-warning");
      expect(warning).toHaveTextContent("No GitHub account connected");
      expect(warning).toHaveTextContent("Settings → Integrations");

      const submitBtn = screen.getByTestId("create-run-button");
      expect(submitBtn).toBeDisabled();
    });

    it("displays available state and canonical repository preview when name is available", async () => {
      const user = userEvent.setup();
      renderWithClient(<AutonomousDeployPage />);

      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      const input = screen.getByTestId("github-repo-input");
      await user.clear(input);
      await user.type(input, "my-fresh-app");

      await waitFor(() => {
        expect(screen.getByTestId("repo-availability-available")).toBeInTheDocument();
      });

      const preview = screen.getByTestId("canonical-repo-preview");
      expect(preview).toHaveTextContent("Repository to create:");
      expect(preview).toHaveTextContent("parag2-8487/my-fresh-app");

      const submitBtn = screen.getByTestId("create-run-button");
      expect(submitBtn).toBeEnabled();
    });

    it("displays already exists state, disables submission, and allows switching to existing mode", async () => {
      const user = userEvent.setup();
      get.mockImplementation((url: string) => {
        if (url === `/projects/${projectId}`) {
          return Promise.resolve({
            id: projectId,
            name: projectName,
            created_at: new Date().toISOString(),
          });
        }
        if (url === "/integrations/github") {
          return Promise.resolve({ configured: true, connected: true, login: "parag2-8487" });
        }
        if (url.startsWith("/integrations/github/repositories/check-availability")) {
          return Promise.resolve({
            available: false,
            owner: "parag2-8487",
            name: "existing-app",
            full_name: "parag2-8487/existing-app",
            status: "taken",
            message: "Repository 'parag2-8487/existing-app' already exists.",
          });
        }
        if (url.startsWith("/integrations/github/repositories?")) {
          return Promise.resolve({ items: [], total: 0, page: 1, per_page: 50, truncated: false });
        }
        if (url.includes("/branches")) {
          return Promise.resolve({
            owner: "parag2-8487",
            repo: "existing-app",
            default_branch: "main",
            branches: ["main"],
            can_push: true,
            is_private: true,
            truncated: false,
          });
        }
        if (url.startsWith("/agents/devices")) {
          return Promise.resolve({ devices: [], next_cursor: null });
        }
        return Promise.resolve({});
      });

      renderWithClient(<AutonomousDeployPage />);
      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      const input = screen.getByTestId("github-repo-input");
      await user.clear(input);
      await user.type(input, "existing-app");

      await waitFor(() => {
        expect(screen.getByTestId("repo-availability-taken")).toBeInTheDocument();
      });
      expect(screen.getByTestId("repo-taken-message")).toHaveTextContent(
        "already exists under your account",
      );

      const submitBtn = screen.getByTestId("create-run-button");
      expect(submitBtn).toBeDisabled();

      // Click switch to existing mode
      const switchBtn = screen.getByTestId("switch-to-existing-btn");
      await user.click(switchBtn);

      // Verify mode switched to existing
      expect(screen.getByTestId("repo-mode-existing")).toHaveClass("border-primary");
    });

    it("displays unknown state and allows submission when GitHub availability cannot be confirmed", async () => {
      const user = userEvent.setup();
      get.mockImplementation((url: string) => {
        if (url === `/projects/${projectId}`) {
          return Promise.resolve({
            id: projectId,
            name: projectName,
            created_at: new Date().toISOString(),
          });
        }
        if (url === "/integrations/github") {
          return Promise.resolve({ configured: true, connected: true, login: "parag2-8487" });
        }
        if (url.startsWith("/integrations/github/repositories/check-availability")) {
          return Promise.resolve({
            available: false,
            owner: "parag2-8487",
            name: "unconfirmed-app",
            full_name: "parag2-8487/unconfirmed-app",
            status: "unknown",
            message: "GitHub could not confirm whether 'unconfirmed-app' is available.",
            detail: "Token lacks repo scope.",
          });
        }
        if (url.startsWith("/agents/devices")) {
          return Promise.resolve({ devices: [], next_cursor: null });
        }
        return Promise.resolve({});
      });

      renderWithClient(<AutonomousDeployPage />);
      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      const input = screen.getByTestId("github-repo-input");
      await user.clear(input);
      await user.type(input, "unconfirmed-app");

      await waitFor(() => {
        expect(screen.getByTestId("repo-availability-unknown")).toBeInTheDocument();
      });
      expect(screen.getByTestId("repo-availability-unknown")).toHaveTextContent(
        "Availability unconfirmed",
      );
      expect(screen.getByTestId("canonical-repo-preview")).toHaveTextContent(
        "parag2-8487/unconfirmed-app",
      );

      const submitBtn = screen.getByTestId("create-run-button");
      expect(submitBtn).toBeEnabled();
      await user.click(submitBtn);

      await waitFor(() => {
        expect(post).toHaveBeenCalledWith(
          `/projects/${projectId}/autonomous-deploy`,
          expect.objectContaining({
            github_config: expect.objectContaining({
              repository_mode: "new_private",
              repository_name: "parag2-8487/unconfirmed-app",
            }),
          }),
        );
      });
    });

    it("displays invalid state and inline validation error when repository name is invalid", async () => {
      const user = userEvent.setup();
      renderWithClient(<AutonomousDeployPage />);
      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      const input = screen.getByTestId("github-repo-input");
      await user.clear(input);
      await user.type(input, "invalid name with space");

      await waitFor(() => {
        expect(screen.getByTestId("repo-availability-invalid")).toBeInTheDocument();
      });
      expect(screen.getByTestId("repo-name-validation-error")).toHaveTextContent(
        "Repository name cannot contain whitespace.",
      );

      const submitBtn = screen.getByTestId("create-run-button");
      expect(submitBtn).toBeDisabled();
    });

    it("displays error and retry button when availability check encounters network failure", async () => {
      const user = userEvent.setup();
      let attempt = 0;
      get.mockImplementation((url: string) => {
        if (url === `/projects/${projectId}`) {
          return Promise.resolve({
            id: projectId,
            name: projectName,
            created_at: new Date().toISOString(),
          });
        }
        if (url === "/integrations/github") {
          return Promise.resolve({ configured: true, connected: true, login: "parag2-8487" });
        }
        if (url.startsWith("/integrations/github/repositories/check-availability")) {
          attempt++;
          if (attempt === 1) {
            return Promise.reject(new Error("Network timeout contacting GitHub API"));
          }
          return Promise.resolve({
            available: true,
            owner: "parag2-8487",
            name: "retry-repo",
            full_name: "parag2-8487/retry-repo",
            status: "available",
            message: "Repository name is available.",
          });
        }
        if (url.startsWith("/agents/devices")) {
          return Promise.resolve({ devices: [], next_cursor: null });
        }
        return Promise.resolve({});
      });

      renderWithClient(<AutonomousDeployPage />);
      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      const input = screen.getByTestId("github-repo-input");
      await user.clear(input);
      await user.type(input, "retry-repo");

      await waitFor(() => {
        expect(screen.getByTestId("repo-availability-error")).toBeInTheDocument();
      });

      const retryBtn = screen.getByTestId("retry-availability-button");
      await user.click(retryBtn);

      await waitFor(() => {
        expect(screen.getByTestId("repo-availability-available")).toBeInTheDocument();
      });
    });

    it("submits canonical name without duplicate owner when user enters owner/repo", async () => {
      const user = userEvent.setup();
      renderWithClient(<AutonomousDeployPage />);
      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      const input = screen.getByTestId("github-repo-input");
      await user.clear(input);
      await user.type(input, "parag2-8487/explicit-repo");

      await waitFor(() => {
        expect(screen.getByTestId("repo-availability-available")).toBeInTheDocument();
      });

      const submitBtn = screen.getByTestId("create-run-button");
      expect(submitBtn).toBeEnabled();
      await user.click(submitBtn);

      await waitFor(() => {
        expect(post).toHaveBeenCalledWith(
          `/projects/${projectId}/autonomous-deploy`,
          expect.objectContaining({
            github_config: expect.objectContaining({
              repository_mode: "new_private",
              repository_name: "parag2-8487/explicit-repo",
            }),
          }),
        );
      });
    });

    it("handles strategy selection and dynamically updates visible configuration sections", async () => {
      const user = userEvent.setup();
      renderWithClient(<AutonomousDeployPage />);
      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      // Default: full stack has GitHub, Vercel, and Docker
      expect(screen.getByTestId("github-config-section")).toBeInTheDocument();
      expect(screen.getByTestId("vercel-config-section")).toBeInTheDocument();
      expect(screen.getByTestId("docker-config-section")).toBeInTheDocument();

      // Click Docker + GitHub
      await user.click(screen.getByTestId("strategy-docker_github"));
      expect(screen.getByTestId("github-config-section")).toBeInTheDocument();
      expect(screen.getByTestId("docker-config-section")).toBeInTheDocument();
      expect(screen.queryByTestId("vercel-config-section")).not.toBeInTheDocument();

      // Click GitHub Only
      await user.click(screen.getByTestId("strategy-github_only"));
      expect(screen.getByTestId("github-config-section")).toBeInTheDocument();
      expect(screen.queryByTestId("docker-config-section")).not.toBeInTheDocument();
      expect(screen.queryByTestId("vercel-config-section")).not.toBeInTheDocument();

      // Click Vercel Only
      await user.click(screen.getByTestId("strategy-vercel_only"));
      expect(screen.queryByTestId("github-config-section")).not.toBeInTheDocument();
      expect(screen.queryByTestId("docker-config-section")).not.toBeInTheDocument();
      expect(screen.getByTestId("vercel-config-section")).toBeInTheDocument();

      // Switch back to Full Stack
      await user.click(screen.getByTestId("strategy-docker_github_vercel"));
      expect(screen.getByTestId("github-config-section")).toBeInTheDocument();
      expect(screen.getByTestId("vercel-config-section")).toBeInTheDocument();
      expect(screen.getByTestId("docker-config-section")).toBeInTheDocument();
    });

    it("supports configuring Vercel options, Docker ports, and custom commit message", async () => {
      const user = userEvent.setup();
      renderWithClient(<AutonomousDeployPage />);
      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      // Change commit message
      const commitInput = screen.getByTestId("github-commit-message-input");
      await user.clear(commitInput);
      await user.type(commitInput, "Custom release commit");

      // Change Vercel project and toggle production checkbox
      const vercelInput = screen.getByTestId("vercel-project-input");
      await user.clear(vercelInput);
      await user.type(vercelInput, "my-custom-vercel-project");

      const vercelCheckbox = screen.getByTestId("vercel-production-checkbox");
      await user.click(vercelCheckbox);

      // Change Docker ports
      const hostPortInput = screen.getByTestId("docker-host-port-input");
      await user.clear(hostPortInput);
      await user.type(hostPortInput, "3000");

      const containerPortInput = screen.getByTestId("docker-container-port-input");
      await user.clear(containerPortInput);
      await user.type(containerPortInput, "3000");

      // Wait for repository availability
      await waitFor(() => {
        expect(screen.getByTestId("repo-availability-available")).toBeInTheDocument();
      });

      const submitBtn = screen.getByTestId("create-run-button");
      expect(submitBtn).toBeEnabled();
      await user.click(submitBtn);

      await waitFor(() => {
        expect(post).toHaveBeenCalledWith(
          `/projects/${projectId}/autonomous-deploy`,
          expect.objectContaining({
            github_config: expect.objectContaining({
              commit_message: "Custom release commit",
            }),
            vercel_config: expect.objectContaining({
              project_name: "my-custom-vercel-project",
              production_deploy: false,
            }),
            docker_config: expect.objectContaining({
              port_bindings: { "3000": 3000 },
            }),
          }),
        );
      });
    });

    it("supports onCancel and onSuccess callback props directly on AutonomousDeployForm", async () => {
      const user = userEvent.setup();
      const mockCancel = vi.fn();
      const mockSuccess = vi.fn();
      const fakeRun = {
        id: "run-custom-success-123",
        project_id: projectId,
        status: "pending",
        strategy: "docker_github_vercel" as const,
        attempt_number: 1,
      };
      post.mockResolvedValueOnce(fakeRun);

      renderWithClient(
        <AutonomousDeployForm
          projectId={projectId}
          projectName={projectName}
          onCancel={mockCancel}
          onSuccess={mockSuccess}
        />,
      );

      // Verify Cancel button triggers onCancel prop
      const cancelBtn = screen.getByRole("button", { name: "Cancel" });
      await user.click(cancelBtn);
      expect(mockCancel).toHaveBeenCalledTimes(1);

      // Wait for availability check and click submit
      await waitFor(() => {
        expect(screen.getByTestId("repo-availability-available")).toBeInTheDocument();
      });

      const submitBtn = screen.getByTestId("create-run-button");
      await user.click(submitBtn);

      await waitFor(() => {
        expect(mockSuccess).toHaveBeenCalledWith(fakeRun);
      });
    });

    it("displays retry button when remote branch query fails and allows retry", async () => {
      const user = userEvent.setup();
      let branchQueryFails = true;
      get.mockImplementation((url: string) => {
        if (url === `/projects/${projectId}`) {
          return Promise.resolve({
            id: projectId,
            name: projectName,
            created_at: new Date().toISOString(),
          });
        }
        if (url === "/integrations/github") {
          return Promise.resolve({ configured: true, connected: true, login: "parag2-8487" });
        }
        if (url.startsWith("/integrations/github/repositories?")) {
          return Promise.resolve({
            items: [
              {
                full_name: "parag2-8487/test",
                owner: "parag2-8487",
                name: "test",
                private: true,
                default_branch: "main",
                language: "TypeScript",
                pushed_at: new Date().toISOString(),
                clone_url: "https://github.com/parag2-8487/test.git",
                html_url: "https://github.com/parag2-8487/test",
                size_kb: 1024,
                archived: false,
              },
            ],
            total: 1,
            page: 1,
            per_page: 50,
            truncated: false,
          });
        }
        if (url.includes("/branches")) {
          if (branchQueryFails) {
            return Promise.reject(new Error("Failed to load branches"));
          }
          return Promise.resolve({
            owner: "parag2-8487",
            repo: "test",
            default_branch: "main",
            branches: ["main", "feature/awesome"],
            can_push: true,
            is_private: true,
            truncated: false,
          });
        }
        if (url.startsWith("/agents/devices")) {
          return Promise.resolve({ devices: [], next_cursor: null });
        }
        return Promise.resolve({});
      });

      renderWithClient(<AutonomousDeployPage />);
      await screen.findByRole("heading", { level: 1, name: "Autonomous Deployment" });

      // Switch to Existing Repository mode and select repo
      await user.click(screen.getByTestId("repo-mode-existing"));
      await user.click(screen.getByLabelText("Toggle repository dropdown"));
      await user.click(await screen.findByTestId("repo-select-parag2-8487/test"));

      // Open branch dropdown
      const toggleBtn = screen.getByLabelText("Toggle branch dropdown");
      await user.click(toggleBtn);

      // Verify retry button rendered and click it
      const retryBtn = await screen.findByRole("button", { name: /retry/i });
      branchQueryFails = false;
      await user.click(retryBtn);

      // Verify branches loaded after retry
      await waitFor(() => {
        expect(screen.getByTestId("branch-select-feature/awesome")).toBeInTheDocument();
      });
    });
  });

  describe("validateGitBranchName helper unit tests", () => {
    it("accepts valid git branch names", () => {
      expect(validateGitBranchName("main")).toBeNull();
      expect(validateGitBranchName("master")).toBeNull();
      expect(validateGitBranchName("feature/autonomous-deploy")).toBeNull();
      expect(validateGitBranchName("release-v1.6.0")).toBeNull();
      expect(validateGitBranchName("user_123/fix")).toBeNull();
    });

    it("rejects empty branch names", () => {
      expect(validateGitBranchName("")).toBe("Branch name cannot be empty.");
    });

    it("rejects branches with whitespace", () => {
      expect(validateGitBranchName("feature branch")).toContain("whitespace is forbidden");
      expect(validateGitBranchName(" main")).toContain("whitespace is forbidden");
      expect(validateGitBranchName("main\t")).toContain("whitespace is forbidden");
    });

    it("rejects branches starting with hyphen", () => {
      expect(validateGitBranchName("-main")).toContain("cannot begin with a hyphen");
    });

    it("rejects consecutive slashes or leading/trailing slashes", () => {
      expect(validateGitBranchName("/main")).toContain(
        "cannot be '@', start/end with '/', or end with '.'",
      );
      expect(validateGitBranchName("main/")).toContain(
        "cannot be '@', start/end with '/', or end with '.'",
      );
      expect(validateGitBranchName("feature//branch")).toContain(
        "consecutive slashes '//' are forbidden",
      );
    });

    it("rejects forbidden characters and sequences", () => {
      expect(validateGitBranchName("feat..branch")).toContain("forbidden sequence");
      expect(validateGitBranchName("feat@{upstream}")).toContain("forbidden sequence");
      expect(validateGitBranchName("feat~1")).toContain("forbidden character '~'");
      expect(validateGitBranchName("feat^1")).toContain("forbidden character '^'");
      expect(validateGitBranchName("feat:1")).toContain("forbidden character ':'");
      expect(validateGitBranchName("feat*")).toContain("forbidden character '*'");
    });

    it("rejects components ending in .lock or starting with .", () => {
      expect(validateGitBranchName("feat/.hidden")).toContain("cannot start with '.'");
      expect(validateGitBranchName("feat/test.lock")).toContain("cannot end with '.lock'");
    });
  });
});
