/**
 * The AI Command Center UI. Phase 2 §2.5.
 *
 * THE TEST THAT MATTERS MOST asserts that typing a mutating sentence does NOT execute it — that interpreting
 * and executing are two user actions, and the second shows the resolved operation and arguments first. The
 * whole risk of a natural-language surface is the gap between what was said and what was understood, so a UI
 * that closed that gap only after acting would be worse than no UI.
 *
 * The second-most-important asserts that the client sends an INTENT and never an operation: a request body
 * carrying `operation` would be a client able to redirect what runs.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  CommandHistory,
  CommandInput,
  type HistoryRow,
  type InterpretedPlan,
} from "@/features/commands/CommandCenter";

const get = vi.fn();
const post = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));

function mount(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

function plan(overrides: Partial<InterpretedPlan> = {}): InterpretedPlan {
  return {
    intent: "deploy",
    category: "deploy",
    operation: "deployments.create",
    arguments: { environment: "staging" },
    mutating: true,
    ready: true,
    confidence: 0.9,
    describes: "Deploy this project's current manifests to an environment.",
    explanation:
      "Understood as: Deploy this project's current manifests to an environment. This changes things, so it needs your confirmation and then passes policy, approval and audit like any other change.",
    executed: false,
    ...overrides,
  };
}

const CATALOGUE = {
  commands: [
    {
      intent: "deploy",
      category: "deploy",
      mutating: true,
      describes: "Deploy.",
      examples: ["deploy to staging"],
      required_slots: ["environment"],
    },
  ],
  explanation:
    "These are the only commands natural language can resolve to. The set is closed. No command runs a command line.",
};

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  get.mockResolvedValue(CATALOGUE);
});

describe("interpreting and executing are two separate actions", () => {
  it("typing a mutating sentence does not execute it", async () => {
    post.mockResolvedValue(plan());
    mount(<CommandInput projectId="proj" sessionKey="s1" />);
    await userEvent.type(screen.getByTestId("command-input"), "deploy to production");
    await userEvent.click(screen.getByText("Interpret"));

    await waitFor(() => expect(screen.getByTestId("command-plan")).toBeInTheDocument());
    // Exactly one call, and it was interpret.
    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0][0]).toBe("/commands/interpret");
    expect(screen.getByTestId("command-confirm").textContent).toBe("Confirm and dispatch");
  });

  it("shows the resolved operation and arguments before anything runs", async () => {
    post.mockResolvedValue(plan());
    mount(<CommandInput projectId="proj" sessionKey="s1" />);
    await userEvent.type(screen.getByTestId("command-input"), "deploy to staging");
    await userEvent.click(screen.getByText("Interpret"));

    const operation = await screen.findByTestId("command-plan-operation");
    expect(operation.textContent).toContain("deployments.create");
    expect(screen.getByTestId("command-plan-arguments").textContent).toContain("staging");
  });

  it("sends an intent and slots, never an operation", async () => {
    post.mockImplementation((path: string) =>
      path === "/commands/interpret"
        ? Promise.resolve(plan())
        : Promise.resolve({ explanation: "Dispatched.", dispatched: true }),
    );
    mount(<CommandInput projectId="proj" sessionKey="s1" />);
    await userEvent.type(screen.getByTestId("command-input"), "deploy to staging");
    await userEvent.click(screen.getByText("Interpret"));
    await waitFor(() => expect(screen.getByTestId("command-confirm")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("command-confirm"));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const body = post.mock.calls[1][1] as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["intent", "project_id", "session_key", "slots"]);
    expect(body).not.toHaveProperty("operation");
  });

  it("warns on a weak match before confirmation", async () => {
    post.mockResolvedValue(plan({ confidence: 0.3 }));
    mount(<CommandInput projectId="proj" sessionKey="s1" />);
    await userEvent.type(screen.getByTestId("command-input"), "do the thing");
    await userEvent.click(screen.getByText("Interpret"));

    const warning = await screen.findByTestId("command-low-confidence");
    expect(warning.getAttribute("role")).toBe("alert");
    expect(warning.textContent).toContain("invites confirming without looking");
  });

  it("asks for a missing value instead of offering to run", async () => {
    post.mockResolvedValue(
      plan({
        ready: false,
        arguments: {},
        explanation: "Missing: environment. Nothing has been done.",
      }),
    );
    mount(<CommandInput projectId="proj" sessionKey="s1" />);
    await userEvent.type(screen.getByTestId("command-input"), "deploy");
    await userEvent.click(screen.getByText("Interpret"));

    const incomplete = await screen.findByTestId("command-incomplete");
    expect(incomplete.textContent).toContain("Nothing has been assumed on your behalf");
    expect(screen.queryByTestId("command-confirm")).toBeNull();
  });

  it("says nothing was done when a command is refused", async () => {
    post.mockImplementation(async () => {
      throw new Error("That command is not one this system understands.");
    });
    mount(<CommandInput projectId="proj" sessionKey="s1" />);
    await userEvent.type(screen.getByTestId("command-input"), "rm -rf /");
    await userEvent.click(screen.getByText("Interpret"));

    const refusal = await screen.findByTestId("command-refusal");
    expect(refusal.textContent).toContain("Nothing was done");
    expect(screen.queryByTestId("command-plan")).toBeNull();
  });

  it("labels a read as changing nothing", async () => {
    post.mockResolvedValue(
      plan({
        intent: "show_pods",
        operation: "kubernetes.pod_list",
        arguments: {},
        mutating: false,
        explanation:
          "Understood as: List the pods in a namespace. This only reads; nothing will be changed.",
      }),
    );
    mount(<CommandInput projectId="proj" sessionKey="s1" />);
    await userEvent.type(screen.getByTestId("command-input"), "show pods");
    await userEvent.click(screen.getByText("Interpret"));

    await waitFor(() => expect(screen.getByTestId("command-confirm")).toBeInTheDocument());
    expect(screen.getByTestId("command-confirm").textContent).toBe("Run this read");
    expect(screen.getByTestId("command-plan-explanation").textContent).toContain(
      "nothing will be changed",
    );
  });
});

describe("autocomplete comes from the server", () => {
  it("suggests the catalogue's examples and states the set is closed", async () => {
    mount(<CommandInput projectId="proj" sessionKey="s1" />);
    const note = await screen.findByTestId("command-catalogue-note");
    expect(note.textContent).toContain("No command runs a command line");
    const suggestions = screen.getByTestId("command-suggestions");
    expect(suggestions.querySelectorAll("option")).toHaveLength(1);
    expect(suggestions.querySelector("option")?.getAttribute("value")).toBe("deploy to staging");
  });
});

describe("the history shows refusals", () => {
  function row(overrides: Partial<HistoryRow> = {}): HistoryRow {
    return {
      id: "h1",
      utterance: "deploy to mars",
      intent: "",
      outcome: "refused",
      detail: "'mars' is not an environment this system knows.",
      session_key: "s1",
      created_at: "2026-09-23T00:00:00Z",
      ...overrides,
    };
  }

  it("describes a refusal as received and declined", async () => {
    get.mockResolvedValue({
      history: [row()],
      explanation: "1 command(s), including any declined.",
    });
    mount(<CommandHistory sessionKey="s1" />);
    const outcome = await screen.findByTestId("command-outcome-h1");
    expect(outcome.textContent).toBe("received and declined");
    expect(screen.getByText(/not an environment/)).toBeInTheDocument();
  });

  it("says a dispatched command faces policy and audit", async () => {
    get.mockResolvedValue({
      history: [row({ outcome: "dispatched", intent: "deploy", detail: "" })],
      explanation: "1 command(s).",
    });
    mount(<CommandHistory sessionKey="s1" />);
    const outcome = await screen.findByTestId("command-outcome-h1");
    expect(outcome.textContent).toContain("faces policy and audit");
  });

  it("distinguishes an empty history from a silent failure", async () => {
    get.mockResolvedValue({
      history: [],
      explanation:
        "Nothing has been asked yet. A declined command would appear here with its reason, so an empty history means no command was received rather than that one failed silently.",
    });
    mount(<CommandHistory sessionKey="s1" />);
    const explanation = await screen.findByTestId("command-history-explanation");
    expect(explanation.textContent).toContain("rather than that one failed silently");
  });
});
