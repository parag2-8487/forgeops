// SPDX-License-Identifier: FSL-1.1-ALv2
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockGet, mockPost } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      get: mockGet,
      post: mockPost,
    },
  };
});

import { AgentConnectPanel } from "@/features/agent/AgentConnectPanel";
import { UninstallDialog } from "@/features/agent/UninstallDialog";

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

describe("Agent Uninstall Feature & UI", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();

    mockGet.mockResolvedValue({
      backend_ws_url: "ws://localhost:18000/api/v1/ws/agent",
      agent_ws_path: "/api/v1/ws/agent",
      release_tag: "v0.1.2",
      download_base_url: "https://github.com/parag8487/ForgeOps/releases/download",
    });
  });

  it("renders the agent commands dropdown containing Help, Doctor, Wipe, and Uninstall", async () => {
    renderWithClient(<AgentConnectPanel platform="windows" />);
    await screen.findByTestId("connect-command");

    const dropdownTrigger = screen.getByTestId("agent-commands-dropdown");
    expect(dropdownTrigger).toBeInTheDocument();

    await userEvent.click(dropdownTrigger);

    expect(await screen.findByTestId("dropdown-cmd-help")).toBeInTheDocument();
    expect(await screen.findByTestId("dropdown-cmd-doctor")).toBeInTheDocument();
    expect(await screen.findByTestId("dropdown-cmd-wipe")).toBeInTheDocument();
    expect(await screen.findByTestId("dropdown-cmd-uninstall")).toBeInTheDocument();
  });

  it("renders the uninstall command preview block", async () => {
    renderWithClient(<AgentConnectPanel platform="windows" />);
    const uninstallBlock = await screen.findByTestId("uninstall-command");
    expect(uninstallBlock.textContent).toContain("forgeops-agent.exe uninstall --force");
  });

  it("displays scope and safety explanations in the confirmation dialog", async () => {
    renderWithClient(
      <UninstallDialog open={true} onOpenChange={() => {}} platform="windows" shell="powershell" />,
    );

    expect(screen.getByTestId("uninstall-dialog")).toBeInTheDocument();
    expect(screen.getByText(/Scope & Safety Notice/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Active ForgeOps agent sessions will be immediately disconnected/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Your project files, source workspaces, and repository code remain untouched/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByTestId("local-uninstall-command")).toBeInTheDocument();
  });

  it("executes the uninstall API call and renders structured summary results", async () => {
    mockPost.mockResolvedValueOnce({
      stopped_devices: ["dev-1", "dev-2"],
      removed_devices: ["dev-1", "dev-2"],
      cleaned_entries: ["revoked_tokens"],
      skipped: [],
      remaining_count: 0,
      message: "Successfully uninstalled and revoked 2 agent device(s).",
      success: true,
    });

    renderWithClient(
      <UninstallDialog open={true} onOpenChange={() => {}} platform="windows" shell="powershell" />,
    );

    const wipeCheckbox = screen.getByTestId("wipe-credentials-checkbox");
    await userEvent.click(wipeCheckbox);

    const confirmBtn = screen.getByTestId("confirm-uninstall-btn");
    await userEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/agents/uninstall", {
        project_id: undefined,
        dry_run: false,
        force: true,
        wipe_credentials: true,
      });
    });

    const results = await screen.findByTestId("uninstall-results");
    expect(results).toBeInTheDocument();
    expect(
      screen.getByText(/Successfully uninstalled and revoked 2 agent device\(s\)\./i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Next step:/i)).toBeInTheDocument();
  });
});
