// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The codebase index panel and the per-project change history — the two surfaces that make a
 * project's real state visible.
 *
 * WHY THE INDEX PANEL IS THE MOST IMPORTANT SCREEN IN THIS PASS. Three routes served it and none had
 * a caller, so there was no way for a user to learn whether a project had ever been scanned. That one
 * absence explained a readiness score of zero, an empty retrieval result and a generation with no
 * context — three different symptoms with one cause, and no screen that named it.
 *
 * The scan trigger is asserted to be ABSENT, with the command present instead. §2.2.1 confines command
 * dispatch to the governance chokepoint, so a "Scan now" button would need either an architecture
 * violation or a governed operation whose approval semantics nobody has designed — and a scan reads
 * the tree rather than mutating it, so an approval gate around it would be ceremony without a control.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: mockGet,
      post: vi.fn(),
      patch: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
      deleteWith: vi.fn(),
      stream: vi.fn(),
    },
  };
});

vi.mock("next/link", () => ({
  default: ({
    children,
    href,
    ...props
  }: {
    children: React.ReactNode;
    href: string;
  } & React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

import { CodebaseIndexPanel } from "@/features/codebase/CodebaseIndexPanel";
import {
  ChangeHistoryTimeline,
  CHANGE_SET_STATUS_MEANING,
} from "@/features/approvals/ChangeHistoryTimeline";
import type { CodebaseStatus } from "@/features/projects/types";

function renderIt(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function status(overrides: Partial<CodebaseStatus> = {}): CodebaseStatus {
  return {
    indexed_files: 0,
    total_chunks: 0,
    languages: [],
    status: "empty",
    total_bytes: 0,
    resolved_dependencies: 0,
    unresolved_dependencies: 0,
    last_indexed_at: null,
    ...overrides,
  };
}

beforeEach(() => mockGet.mockReset());
afterEach(() => cleanup());

describe("the index panel says whether a project has ever been scanned", () => {
  it("reports an empty index as never scanned, and names the consequences", async () => {
    mockGet.mockResolvedValue(status());
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);

    expect(await screen.findByTestId("index-headline")).toHaveTextContent("Never scanned");
    // The three symptoms this one fact explains, said in one place.
    expect(screen.getByText(/readiness cannot be scored/i)).toBeInTheDocument();
    expect(screen.getByText(/retrieval has nothing to search/i)).toBeInTheDocument();
    expect(screen.getByText(/generation will run without context/i)).toBeInTheDocument();
  });

  it("distinguishes indexed-without-vectors from both other states", async () => {
    mockGet.mockResolvedValue(
      status({ indexed_files: 141, total_chunks: 0, status: "indexed_without_vectors" }),
    );
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);

    expect(await screen.findByTestId("index-headline")).toHaveTextContent(
      "Indexed, without vectors",
    );
    // Not "failed": the tree, contents and dependency graph are all there. Not "indexed" either:
    // retrieval is sparse-only. Collapsing it either way leaves someone puzzled by weak retrieval.
    expect(screen.getByText(/sparse-only \(BM25\) rather than hybrid/i)).toBeInTheDocument();
    expect(screen.getByText(/symbol search is empty/i)).toBeInTheDocument();
  });

  it("reports the counts the endpoint actually returned", async () => {
    mockGet.mockResolvedValue(
      status({
        indexed_files: 141,
        total_chunks: 977,
        status: "indexed",
        total_bytes: 1_234_567,
        resolved_dependencies: 243,
        unresolved_dependencies: 1614,
        languages: ["python", "typescript"],
        last_indexed_at: "2026-08-27T00:00:00Z",
      }),
    );
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);

    expect(await screen.findByText("141")).toBeInTheDocument();
    expect(screen.getByText("977")).toBeInTheDocument();
    // Resolved out of the TOTAL, because 243 alone reads as a small number rather than as a ratio.
    expect(screen.getByText("243 of 1857")).toBeInTheDocument();
    expect(screen.getByText("python, typescript")).toBeInTheDocument();
  });

  it("says no language was detected rather than rendering an empty field", async () => {
    mockGet.mockResolvedValue(status({ indexed_files: 3, status: "indexed_without_vectors" }));
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);
    expect(await screen.findByText("none detected")).toBeInTheDocument();
    expect(screen.getByText("never")).toBeInTheDocument();
  });

  it("explains what an unresolved dependency is, so the count means something", async () => {
    mockGet.mockResolvedValue(
      status({ indexed_files: 3, status: "indexed", total_chunks: 9, unresolved_dependencies: 4 }),
    );
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);
    expect(
      await screen.findByText(/imports the graph builder could not point at a file/i),
    ).toBeInTheDocument();
  });
});

describe("the scan trigger", () => {
  it("offers the exact command instead of a button, and says why", async () => {
    mockGet.mockResolvedValue(status());
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/checkout" />);

    // No trigger. §2.2.1 confines command dispatch to the chokepoint, and a scan is not a mutation of
    // the user's tree — so putting it through an approval gate would add ceremony without a control.
    expect(screen.queryByRole("button", { name: /scan now/i })).not.toBeInTheDocument();
    expect(await screen.findByTestId("scan-command")).toHaveTextContent(
      "forgeops-agent scan --project p-1 --path /srv/checkout",
    );
    expect(screen.getByText(/the backend cannot tell an agent to scan/i)).toBeInTheDocument();
  });

  it("mentions watch mode, because it makes scanning continuous", async () => {
    mockGet.mockResolvedValue(status());
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);
    expect(await screen.findByText(/forgeops-agent watch --project p-1/)).toBeInTheDocument();
    expect(
      screen.getByText(/re-indexes a changed file together with the files that import it/i),
    ).toBeInTheDocument();
  });

  it("survives a clipboard that is not available", async () => {
    mockGet.mockResolvedValue(status());
    // Absent in a non-secure context and in jsdom. A copy button that throws is worse than one that
    // quietly fails to confirm.
    const original = navigator.clipboard;
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);
    await userEvent.click(await screen.findByRole("button", { name: /copy scan command/i }));
    expect(screen.queryByText("Copied.")).not.toBeInTheDocument();
    Object.defineProperty(navigator, "clipboard", { value: original, configurable: true });
  });

  it("confirms a successful copy", async () => {
    mockGet.mockResolvedValue(status());
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);
    await userEvent.click(await screen.findByRole("button", { name: /copy scan command/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(await screen.findByText("Copied.")).toBeInTheDocument();
  });
});

describe("symbol search", () => {
  const SYMBOL = {
    name: "PolicyBundleService",
    kind: "class",
    file_path: "src/policies/bundle.py",
    line_number: 31,
    parent_symbol: null,
    signature: "class PolicyBundleService:",
    chunk_id: "c-1",
  };

  function serveIndexed(symbols: unknown[] = [SYMBOL], chunk?: unknown) {
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.endsWith("/status")) {
        return Promise.resolve(
          status({ indexed_files: 141, total_chunks: 977, status: "indexed" }),
        );
      }
      if (path.includes("/symbols")) return Promise.resolve(symbols);
      if (path.includes("/chunks/")) return Promise.resolve(chunk);
      // Resolved rather than rejected. An unmatched path here is a request the test did not
      // anticipate, and rejecting it produces an unhandled rejection during teardown that reports as a
      // failure of whichever test happened to be running -- which hides the real assertion. The
      // requests that matter are asserted positively below.
      return Promise.resolve(null);
    });
  }

  it("is not offered at all when the index has no chunks", async () => {
    mockGet.mockResolvedValue(
      status({ indexed_files: 3, total_chunks: 0, status: "indexed_without_vectors" }),
    );
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);
    await screen.findByTestId("index-headline");
    // Symbol metadata lives on the embedding rows, so this search could only ever return an empty list
    // — which is indistinguishable from "this project has no functions". Not offering it is honest.
    expect(screen.queryByLabelText(/symbol name contains/i)).not.toBeInTheDocument();
  });

  it("requests nothing until a term is submitted", async () => {
    serveIndexed();
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);
    await screen.findByLabelText(/symbol name contains/i);
    expect(mockGet.mock.calls.every((c) => !String(c[0]).includes("/symbols"))).toBe(true);
  });

  it("sends the term url-encoded, and says a percent sign is literal", async () => {
    serveIndexed();
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);

    await userEvent.type(await screen.findByLabelText(/symbol name contains/i), "Bundle Service");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith(
        expect.stringContaining("/symbols?query=Bundle%20Service"),
      ),
    );
  });

  it("renders the declaration the scanner recorded", async () => {
    serveIndexed();
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);
    await userEvent.type(await screen.findByLabelText(/symbol name contains/i), "Policy");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("PolicyBundleService")).toBeInTheDocument();
    expect(screen.getByText("src/policies/bundle.py:31")).toBeInTheDocument();
    expect(screen.getByText("class PolicyBundleService:")).toBeInTheDocument();
  });

  it("says an empty result means the index holds nothing matching, not that search is broken", async () => {
    serveIndexed([]);
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);
    await userEvent.type(await screen.findByLabelText(/symbol name contains/i), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(
      await screen.findByText(/the index holds only what the last scan recorded/i),
    ).toBeInTheDocument();
  });

  it("expands into the stored chunk, and says the content is redacted", async () => {
    serveIndexed([SYMBOL], {
      chunk_id: "c-1",
      file_path: "src/policies/bundle.py",
      content: "class PolicyBundleService:\n    ...",
      start_line: 31,
      end_line: 78,
      language: "python",
      symbol: "PolicyBundleService",
      parent_symbol: null,
      kind: "class",
      token_count: 210,
      model_id: "bge-m3:567m",
    });
    renderIt(<CodebaseIndexPanel projectId="p-1" projectPath="/srv/x" />);
    await userEvent.type(await screen.findByLabelText(/symbol name contains/i), "Policy");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    const toggle = await screen.findByTestId("chunk-toggle-c-1");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);

    expect(await screen.findByText(/lines 31–78/i)).toBeInTheDocument();
    expect(screen.getByText("bge-m3:567m")).toBeInTheDocument();
    // The agent redacts BEFORE transmitting, so there is no unredacted copy to show. Saying so stops a
    // reader assuming the platform is withholding something it has.
    expect(screen.getByText(/there is no unredacted copy here to show/i)).toBeInTheDocument();
  });
});

describe("the change history timeline", () => {
  function changeSet(overrides: Record<string, unknown> = {}) {
    return {
      id: "cs-1",
      project_id: "p-1",
      status: "applied",
      origin: "generation",
      blast_radius_score: 12,
      blast_radius_verdict: "moderate",
      version: 3,
      generation_run_id: "run-1",
      created_at: "2026-08-21T04:00:00Z",
      applied_at: "2026-08-21T04:05:00Z",
      ...overrides,
    };
  }

  it("requests the project's own change sets, not the tenant's pending queue", async () => {
    mockGet.mockResolvedValue({ change_sets: [changeSet()], next_cursor: null });
    renderIt(<ChangeHistoryTimeline projectId="p-1" />);
    // Scoped by project and unfiltered by status: "what has happened to this project" needs terminal
    // states included, which the approvals screen's pending-only list cannot answer.
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith(expect.stringContaining("/approvals?project_id=p-1")),
    );
    expect(String(mockGet.mock.calls[0][0])).not.toContain("status=");
  });

  it("explains what each status means rather than colour-coding it", async () => {
    mockGet.mockResolvedValue({ change_sets: [changeSet()], next_cursor: null });
    renderIt(<ChangeHistoryTimeline projectId="p-1" />);
    expect(await screen.findByTestId("status-cs-1")).toHaveTextContent("applied");
    expect(screen.getByText(/written to the working tree/i)).toBeInTheDocument();
  });

  it("distinguishes rolled_back from reverted, which a red badge could not", async () => {
    mockGet.mockResolvedValue({
      change_sets: [
        changeSet({ status: "rolled_back" }),
        changeSet({ id: "cs-2", status: "reverted" }),
      ],
      next_cursor: null,
    });
    renderIt(<ChangeHistoryTimeline projectId="p-1" />);
    // One is an apply that failed midway and undid itself; the other is a deliberate reversal that went
    // through the chokepoint with its own fresh authority. The difference matters.
    expect(
      await screen.findByText(/NOT the same as reverted — nobody chose this/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/its own authority/i)).toBeInTheDocument();
  });

  it("covers every status the backend's CHECK constraint permits", () => {
    /**
     * §3.6's thirteen states, which revision `0010` put a CHECK constraint on. Mirrored here so a state
     * the database allows and this component cannot describe is caught by a test rather than by a user
     * seeing an unexplained word.
     */
    const CHANGE_SET_STATUSES = [
      "draft",
      "validating",
      "validation_failed",
      "pending_approval",
      "approved",
      "rejected",
      "applying",
      "applied",
      "apply_failed",
      "rolled_back",
      "reverted",
      "expired",
      "superseded",
    ];
    expect(Object.keys(CHANGE_SET_STATUS_MEANING).sort()).toEqual([...CHANGE_SET_STATUSES].sort());
  });

  it("renders an unknown status without hiding it", async () => {
    mockGet.mockResolvedValue({
      change_sets: [changeSet({ status: "quantum" })],
      next_cursor: null,
    });
    renderIt(<ChangeHistoryTimeline projectId="p-1" />);
    // A fourteenth state added to the backend shows up as an unexplained status rather than vanishing
    // from the timeline, which would make the history silently incomplete.
    expect(await screen.findByTestId("status-cs-1")).toHaveTextContent("quantum");
    expect(screen.getByText(/no description in the UI/i)).toBeInTheDocument();
  });

  it("says nothing has been submitted rather than showing an error", async () => {
    mockGet.mockResolvedValue({ change_sets: [], next_cursor: null });
    renderIt(<ChangeHistoryTimeline projectId="p-1" />);
    expect(await screen.findByText(/generation is what creates one/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("makes no request without a project", () => {
    renderIt(<ChangeHistoryTimeline projectId="" />);
    expect(mockGet).not.toHaveBeenCalled();
  });
});
