// SPDX-License-Identifier: FSL-1.1-ALv2
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

    const helpItem = await screen.findByTestId("dropdown-cmd-help");
    const doctorItem = await screen.findByTestId("dropdown-cmd-doctor");
    const wipeItem = await screen.findByTestId("dropdown-cmd-wipe");
    const uninstallItem = await screen.findByTestId("dropdown-cmd-uninstall");

    expect(helpItem).toBeInTheDocument();
    expect(doctorItem).toBeInTheDocument();
    expect(wipeItem).toBeInTheDocument();
    expect(uninstallItem).toBeInTheDocument();

    await userEvent.click(uninstallItem);

    // Verify dialog opened
    expect(await screen.findByTestId("uninstall-dialog")).toBeInTheDocument();
  });

  it("allows clicking help item from the commands dropdown", async () => {
    renderWithClient(<AgentConnectPanel platform="windows" />);
    await screen.findByTestId("connect-command");

    const dropdownTrigger = screen.getByTestId("agent-commands-dropdown");
    await userEvent.click(dropdownTrigger);
    const helpItem = await screen.findByTestId("dropdown-cmd-help");
    await userEvent.click(helpItem);
  });

  it("allows switching shell in picker and updating command block", async () => {
    renderWithClient(<AgentConnectPanel platform="windows" />);
    await screen.findByTestId("connect-command");

    const shellPicker = screen.getByTestId("shell-picker");
    expect(shellPicker).toBeInTheDocument();
    fireEvent.change(shellPicker, { target: { value: "cmd" } });
    expect(shellPicker).toHaveValue("cmd");
  });

  it("renders the uninstall command preview block", async () => {
    renderWithClient(<AgentConnectPanel platform="windows" />);
    const uninstallBlock = await screen.findByTestId("uninstall-command");
    expect(uninstallBlock.textContent).toContain("forgeops-agent.exe uninstall --force");
  });

  it("displays scope and safety explanations in the confirmation dialog and handles cancel", async () => {
    const handleOpenChange = vi.fn();
    renderWithClient(
      <UninstallDialog
        open={true}
        onOpenChange={handleOpenChange}
        platform="windows"
        shell="powershell"
      />,
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

    const cancelBtn = screen.getByRole("button", { name: /cancel/i });
    await userEvent.click(cancelBtn);
    expect(handleOpenChange).toHaveBeenCalledWith(false);
  });

  it("executes the uninstall API call with project ID, handles skipped entries, and allows dismissing results", async () => {
    mockPost.mockResolvedValueOnce({
      stopped_devices: ["dev-1", "dev-2"],
      removed_devices: ["dev-1", "dev-2"],
      cleaned_entries: ["revoked_tokens"],
      skipped: [{ device_id: "dev-3", reason: "permission denied" }],
      remaining_count: 1,
      message: "Successfully uninstalled and revoked 2 agent device(s).",
      success: false,
    });

    const handleOpenChange = vi.fn();
    renderWithClient(
      <UninstallDialog
        open={true}
        onOpenChange={handleOpenChange}
        platform="windows"
        shell="powershell"
        projectId="proj-456"
      />,
    );

    const wipeCheckbox = screen.getByTestId("wipe-credentials-checkbox");
    await userEvent.click(wipeCheckbox);

    const confirmBtn = screen.getByTestId("confirm-uninstall-btn");
    await userEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/agents/uninstall", {
        project_id: "proj-456",
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
    expect(screen.getByText(/Partially removed/i)).toBeInTheDocument();
    expect(screen.getByText(/Skipped \/ Remaining: 1/i)).toBeInTheDocument();
    expect(screen.getByText(/revoked_tokens/i)).toBeInTheDocument();
    expect(screen.getByText(/Next step:/i)).toBeInTheDocument();

    // Click Done to exercise handleClose
    const doneBtn = screen.getByTestId("uninstall-dismiss-btn");
    await userEvent.click(doneBtn);
    expect(handleOpenChange).toHaveBeenCalledWith(false);
  });

  it("handles successful clean uninstall without credentials wipe or skipped items", async () => {
    mockPost.mockResolvedValueOnce({
      stopped_devices: ["dev-1"],
      removed_devices: ["dev-1"],
      cleaned_entries: [],
      skipped: [],
      remaining_count: 0,
      message: "Clean uninstall complete.",
      success: true,
    });

    renderWithClient(
      <UninstallDialog open={true} onOpenChange={() => {}} platform="windows" shell="powershell" />,
    );

    const confirmBtn = screen.getByTestId("confirm-uninstall-btn");
    await userEvent.click(confirmBtn);

    expect(await screen.findByText(/Clean uninstall complete\./i)).toBeInTheDocument();
    expect(screen.getByText(/Status: Removed successfully/i)).toBeInTheDocument();
    expect(screen.queryByText(/Skipped/i)).toBeNull();
  });

  it("handles uninstall mutation error state gracefully", async () => {
    mockPost.mockRejectedValueOnce(new Error("Network connection dropped"));

    renderWithClient(
      <UninstallDialog open={true} onOpenChange={() => {}} platform="linux" shell="bash" />,
    );

    const confirmBtn = screen.getByTestId("confirm-uninstall-btn");
    await userEvent.click(confirmBtn);

    expect(
      await screen.findByText(/Error during uninstall: Network connection dropped/i),
    ).toBeInTheDocument();
  });

  it("displays loading indicator and disabled button while mutation is pending", async () => {
    let resolver: (data: unknown) => void = () => {};
    mockPost.mockReturnValueOnce(
      new Promise((resolve) => {
        resolver = resolve;
      }),
    );

    renderWithClient(
      <UninstallDialog open={true} onOpenChange={() => {}} platform="windows" shell="powershell" />,
    );

    const confirmBtn = screen.getByTestId("confirm-uninstall-btn");
    await userEvent.click(confirmBtn);

    expect(screen.getByText(/Uninstalling…/i)).toBeInTheDocument();
    expect(confirmBtn).toBeDisabled();

    resolver({
      stopped_devices: [],
      removed_devices: [],
      cleaned_entries: [],
      skipped: [],
      remaining_count: 0,
      message: "Pending test completed successfully",
      success: true,
    });

    expect(await screen.findByText(/Pending test completed successfully/i)).toBeInTheDocument();
  });
});
