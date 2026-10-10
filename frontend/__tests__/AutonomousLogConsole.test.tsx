// SPDX-License-Identifier: FSL-1.1-ALv2
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  AutonomousLogConsole,
  type AutonomousLogConsoleProps,
} from "@/features/deployments/AutonomousLogConsole";
import type { AutonomousLogEntry } from "@/features/deployments/useAutonomousDeployStream";

describe("AutonomousLogConsole", () => {
  const sampleLogs: AutonomousLogEntry[] = [
    {
      log_seq: 1,
      stage_name: "G1_blueprint",
      level: "INFO",
      message: "Validating project deployment blueprint and strategy config.",
      created_at: "2026-10-10T10:00:01Z",
    },
    {
      log_seq: 2,
      stage_name: "G2_artifact",
      level: "WARN",
      message: "Detected non-standard Dockerfile location: using fallback ./deploy/Dockerfile.",
      created_at: "2026-10-10T10:00:05Z",
    },
    {
      log_seq: 3,
      stage_name: "G4_build",
      level: "ERROR",
      message: "Docker daemon returned compilation error: syntax error in layer 4.",
      created_at: "2026-10-10T10:00:10Z",
    },
    {
      log_seq: 4,
      stage_name: "github_release",
      level: "INFO",
      message: "Successfully published release tag v1.0.4 to forgeops-org/main.",
      created_at: "2026-10-10T10:00:15Z",
    },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders log lines with log_seq, timestamp, stage tag, level colors, and sanitized message", () => {
    render(<AutonomousLogConsole logs={sampleLogs} />);

    // Assert container
    expect(screen.getByTestId("autonomous-log-console")).toBeInTheDocument();

    // Check each line is rendered
    expect(screen.getByTestId("log-line-1")).toBeInTheDocument();
    expect(screen.getByTestId("log-line-2")).toBeInTheDocument();
    expect(screen.getByTestId("log-line-3")).toBeInTheDocument();
    expect(screen.getByTestId("log-line-4")).toBeInTheDocument();

    // Sequence numbers
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
    expect(screen.getByText("#3")).toBeInTheDocument();
    expect(screen.getByText("#4")).toBeInTheDocument();

    // Stage tags
    expect(screen.getByText("[G1_blueprint]")).toBeInTheDocument();
    expect(screen.getByText("[G2_artifact]")).toBeInTheDocument();
    expect(screen.getByText("[G4_build]")).toBeInTheDocument();
    expect(screen.getByText("[github_release]")).toBeInTheDocument();

    // Level badges & text
    const infoLevels = screen.getAllByText("[INFO]");
    expect(infoLevels.length).toBe(2);
    expect(infoLevels[0]).toHaveClass("text-emerald-400");

    const warnLevel = screen.getByText("[WARN]");
    expect(warnLevel).toBeInTheDocument();
    expect(warnLevel).toHaveClass("text-amber-400");

    const errorLevel = screen.getByText("[ERROR]");
    expect(errorLevel).toBeInTheDocument();
    expect(errorLevel).toHaveClass("text-rose-400");

    // Messages
    expect(
      screen.getByText("Validating project deployment blueprint and strategy config."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Docker daemon returned compilation error: syntax error in layer 4."),
    ).toBeInTheDocument();
  });

  it("filters logs by text search query across message and stage name", async () => {
    const user = userEvent.setup();
    render(<AutonomousLogConsole logs={sampleLogs} />);

    const searchInput = screen.getByTestId("log-search-input");
    expect(searchInput).toBeInTheDocument();

    // Search for "daemon"
    await user.type(searchInput, "daemon");

    // Line 3 should remain, lines 1, 2, 4 should be filtered out
    expect(screen.getByTestId("log-line-3")).toBeInTheDocument();
    expect(screen.queryByTestId("log-line-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("log-line-2")).not.toBeInTheDocument();
    expect(screen.queryByTestId("log-line-4")).not.toBeInTheDocument();

    // Clear search
    await user.clear(searchInput);

    expect(screen.getByTestId("log-line-1")).toBeInTheDocument();
    expect(screen.getByTestId("log-line-2")).toBeInTheDocument();
    expect(screen.getByTestId("log-line-3")).toBeInTheDocument();
    expect(screen.getByTestId("log-line-4")).toBeInTheDocument();
  });

  it("filters logs by stage using the stage filter dropdown", async () => {
    const user = userEvent.setup();
    render(<AutonomousLogConsole logs={sampleLogs} />);

    const stageSelect = screen.getByTestId("stage-filter-select");
    expect(stageSelect).toBeInTheDocument();

    // Filter by "G2_artifact"
    await user.selectOptions(stageSelect, "G2_artifact");

    expect(screen.getByTestId("log-line-2")).toBeInTheDocument();
    expect(screen.queryByTestId("log-line-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("log-line-3")).not.toBeInTheDocument();
    expect(screen.queryByTestId("log-line-4")).not.toBeInTheDocument();

    // Switch back to "all"
    await user.selectOptions(stageSelect, "all");

    expect(screen.getByTestId("log-line-1")).toBeInTheDocument();
    expect(screen.getByTestId("log-line-2")).toBeInTheDocument();
    expect(screen.getByTestId("log-line-3")).toBeInTheDocument();
    expect(screen.getByTestId("log-line-4")).toBeInTheDocument();
  });

  it("toggles follow logs auto-scroll lock", async () => {
    const user = userEvent.setup();
    render(<AutonomousLogConsole logs={sampleLogs} />);

    const followBtn = screen.getByTestId("btn-follow-logs");
    expect(followBtn).toHaveAttribute("title", "Auto-scroll locked (active)");

    // Toggle off
    await user.click(followBtn);
    expect(followBtn).toHaveAttribute("title", "Auto-scroll unlocked");

    // Toggle on
    await user.click(followBtn);
    expect(followBtn).toHaveAttribute("title", "Auto-scroll locked (active)");
  });

  it("copies all logs to clipboard and displays confirmation", async () => {
    const user = userEvent.setup();
    const writeTextMock = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: writeTextMock,
      },
      configurable: true,
      writable: true,
    });

    render(<AutonomousLogConsole logs={sampleLogs} />);

    const copyBtn = screen.getByTestId("btn-copy-logs");
    expect(copyBtn).toHaveTextContent("Copy All Logs");

    await user.click(copyBtn);

    expect(writeTextMock).toHaveBeenCalledTimes(1);
    const copiedText = writeTextMock.mock.calls[0][0];
    expect(copiedText).toContain("[G1_blueprint]");
    expect(copiedText).toContain("Validating project deployment blueprint and strategy config.");
    expect(copiedText).toContain("#1");

    // Verify feedback
    await waitFor(() => {
      expect(screen.getByText("Copied!")).toBeInTheDocument();
    });
  });

  it("displays sticky truncation notice when 5,000-line limit is reached", () => {
    // Normal logs < 5,000 lines should NOT display truncation notice
    const { rerender } = render(<AutonomousLogConsole logs={sampleLogs} />);
    expect(screen.queryByTestId("log-truncation-notice")).not.toBeInTheDocument();

    // Generate 5,000 synthetic log lines
    const truncatedLogs: AutonomousLogEntry[] = Array.from({ length: 5000 }, (_, i) => ({
      log_seq: i + 1,
      stage_name: "docker_build",
      level: "INFO",
      message: `Step ${i + 1} execution in container environment`,
      created_at: "2026-10-10T10:00:00Z",
    }));

    rerender(<AutonomousLogConsole logs={truncatedLogs} />);

    const notice = screen.getByTestId("log-truncation-notice");
    expect(notice).toBeInTheDocument();
    expect(notice).toHaveTextContent(
      "[WARN] Log limit reached (5,000 lines). Further output truncated.",
    );
  });

  it("displays empty state when logs list is empty", () => {
    render(<AutonomousLogConsole logs={[]} />);

    const emptyState = screen.getByTestId("log-empty-state");
    expect(emptyState).toBeInTheDocument();
    expect(emptyState).toHaveTextContent("No log output available yet.");
  });
});
