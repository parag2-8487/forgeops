// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * §2.8's panel. Two properties: there is no free-text command field, and a run reports a governance outcome
 * rather than a result — "run the tests" sounds harmless and executes code the repository controls.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DevToolsPanel, KIND_DESCRIPTIONS } from "@/features/devtools/DevToolsPanel";

const post = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: vi.fn(),
      post: (...args: unknown[]) => post(...args),
      put: vi.fn(),
      delete: vi.fn(),
      patch: vi.fn(),
    },
  };
});

function mount(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

beforeEach(() => post.mockReset());

describe("the dev tools panel", () => {
  it("offers exactly the five kinds and no command field", () => {
    mount(<DevToolsPanel projectId="p1" />);
    for (const kind of ["tests", "lint", "build", "compose", "migrations"]) {
      expect(screen.getByTestId(`devtools-run-${kind}`)).toBeInTheDocument();
    }
    // NO FREE TEXT. A panel that could type a command would need an agent that accepts one, and there is
    // no such operation.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("says plainly that these run the repository's own code on the operator's machine", () => {
    mount(<DevToolsPanel projectId="p1" />);
    const warning = screen.getByTestId("devtools-warning");
    expect(warning).toHaveTextContent("this repository defines");
    expect(warning).toHaveTextContent("mutations");
  });

  it("describes what each kind does, including that compose never removes anything", () => {
    expect(KIND_DESCRIPTIONS.build).toContain("writes artifacts");
    // The reassurance has to match the operation: the compose vector is `up -d --wait` and cannot express
    // `down -v`, and the panel's wording says so rather than leaving a reader to assume the worst.
    expect(KIND_DESCRIPTIONS.compose).toContain("never stops one or removes a volume");
    expect(KIND_DESCRIPTIONS.migrations).toContain("database migrations");
  });

  it("sends the kind and reports a governance outcome", async () => {
    post.mockResolvedValue({
      change_set_id: "cs1",
      status: "pending_approval",
      outcome: "approval-required",
    });
    mount(<DevToolsPanel projectId="p1" />);
    await userEvent.click(screen.getByTestId("devtools-run-tests"));
    expect(post).toHaveBeenCalledWith("/projects/p1/devtools/run", { kind: "tests" });
    // The wording must not imply the tests ran.
    expect(await screen.findByTestId("devtools-outcome-tests")).toHaveTextContent(
      "nothing has run yet",
    );
  });

  it("reports a blocked run against the kind that was blocked, and not against another", async () => {
    // A REFUSAL IS A RESOLVED RESPONSE, which is what the chokepoint actually returns: a blast-radius block
    // produces `outcome: "blocked"` with a change-set row, not an exception. Asserting that is both more
    // faithful than forcing a rejection and avoids plumbing that turned out to be about vitest's
    // unhandled-error reporting rather than about the panel.
    post.mockResolvedValue({ change_set_id: "cs2", status: "blocked", outcome: "blocked" });
    mount(<DevToolsPanel projectId="p1" />);
    await userEvent.click(screen.getByTestId("devtools-run-migrations"));
    expect(await screen.findByTestId("devtools-outcome-migrations")).toHaveTextContent("blocked");
    // Per-kind, so one button's outcome never appears against another.
    expect(screen.queryByTestId("devtools-outcome-tests")).not.toBeInTheDocument();
  });
});
