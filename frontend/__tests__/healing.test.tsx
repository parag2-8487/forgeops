/**
 * Self-healing panels. Phase 2 2.12.
 *
 * Self-healing is the only feature acting on production without a human, so the operator-facing question is
 * what it DECLINED to do and what it will no longer try. A log showing only successes would make a system
 * that declined three times look like one never engaged. These tests assert the declines are visible.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  HealingActivityLog,
  HealingLogUnavailable,
  PostmortemDisplay,
  type HealingActionRow,
  type PostmortemRow,
} from "@/features/incidents/HealingPanels";

const get = vi.fn();

vi.mock("@/lib/api/client", () => ({ api: { get: (...args: unknown[]) => get(...args) } }));

function mount(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

function action(overrides: Partial<HealingActionRow> = {}): HealingActionRow {
  return {
    id: "a1",
    remedy: "restart_container",
    operation: "docker.container_action",
    arguments: { action: "restart", container: "api" },
    auto: true,
    state: "succeeded",
    change_set_id: "cs1",
    note: "Container api exited 137.",
    created_at: "2026-09-23T00:00:00Z",
    completed_at: "2026-09-23T00:00:05Z",
    ...overrides,
  };
}

beforeEach(() => get.mockReset());

describe("the activity log makes declines visible", () => {
  it("distinguishes nothing attempted from something declined", async () => {
    get.mockResolvedValue({
      actions: [],
      attempts_used: 0,
      attempts_remaining: 3,
      disqualified_remedies: [],
      explanation:
        "No healing action has been attempted for this incident. That is not the same as one having been attempted and declined -- a declined action would be listed here with its reason.",
    });
    mount(<HealingActivityLog incidentId="i1" />);
    const budget = await screen.findByTestId("healing-budget");
    expect(budget.textContent).toContain("not the same as one having been attempted and declined");
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("shows a declined action as a decision rather than a failure", async () => {
    get.mockResolvedValue({
      actions: [action({ id: "a2", state: "refused", note: "inside the 300-second cooldown" })],
      attempts_used: 0,
      attempts_remaining: 3,
      disqualified_remedies: [],
      explanation: "0 of 3 automated attempts used.",
    });
    mount(<HealingActivityLog incidentId="i1" />);
    const state = await screen.findByTestId("healing-state-a2");
    expect(state.textContent).toContain("Declined by a guard rail before anything was sent");
    expect(state.textContent).not.toContain("Failed");
  });

  it("says an auto action still passed governance", async () => {
    get.mockResolvedValue({
      actions: [action()],
      attempts_used: 1,
      attempts_remaining: 2,
      disqualified_remedies: [],
      explanation: "1 of 3 automated attempts used.",
    });
    mount(<HealingActivityLog incidentId="i1" />);
    const auto = await screen.findByTestId("healing-auto-a1");
    expect(auto.textContent).toContain("without waiting for a human");
    expect(auto.textContent).toContain("Policy, blast radius and audit still applied");
  });

  it("names a risky action as having required approval", async () => {
    get.mockResolvedValue({
      actions: [
        action({ id: "a3", remedy: "rollback_deployment", auto: false, state: "proposed" }),
      ],
      attempts_used: 0,
      attempts_remaining: 3,
      disqualified_remedies: [],
      explanation: "0 of 3 automated attempts used.",
    });
    mount(<HealingActivityLog incidentId="i1" />);
    const auto = await screen.findByTestId("healing-auto-a3");
    expect(auto.textContent).toBe("Required an approval.");
  });

  it("alerts when the budget is exhausted", async () => {
    get.mockResolvedValue({
      actions: [action()],
      attempts_used: 3,
      attempts_remaining: 0,
      disqualified_remedies: [],
      explanation:
        "3 of 3 automated attempts used. The automated budget is exhausted; this incident is now a human's.",
    });
    mount(<HealingActivityLog incidentId="i1" />);
    const budget = await screen.findByTestId("healing-budget");
    expect(budget.getAttribute("role")).toBe("alert");
    expect(budget.textContent).toContain("now a human's");
  });

  it("states a disqualified remedy and why it will not be retried", async () => {
    get.mockResolvedValue({
      actions: [action({ state: "failed" })],
      attempts_used: 1,
      attempts_remaining: 2,
      disqualified_remedies: ["restart_container"],
      explanation:
        "1 of 3 automated attempts used. restart_container failed and will not be retried.",
    });
    mount(<HealingActivityLog incidentId="i1" />);
    const note = await screen.findByTestId("healing-disqualified");
    expect(note.textContent).toContain("evidence it is not the answer");
    expect(note.getAttribute("role")).toBe("alert");
  });

  it("shows the derived target so an operator can confirm what was touched", async () => {
    get.mockResolvedValue({
      actions: [action({ arguments: { action: "restart", container: "checkout-api" } })],
      attempts_used: 1,
      attempts_remaining: 2,
      disqualified_remedies: [],
      explanation: "1 of 3.",
    });
    mount(<HealingActivityLog incidentId="i1" />);
    await waitFor(() =>
      expect(screen.getByTestId("healing-action-a1").textContent).toContain("checkout-api"),
    );
  });

  it("does not show an empty log when the log itself could not be read", () => {
    // Rendered directly. The branch is a pure render decision, and driving React Query into an error state
    // to reach it fought the library rather than the code -- see HealingLogUnavailable's own comment.
    render(<HealingLogUnavailable />);
    const error = screen.getByTestId("healing-log-error");
    expect(error.textContent).toContain("empty log is not being shown");
    expect(error.getAttribute("role")).toBe("alert");
  });
});

describe("the post-incident display", () => {
  function postmortem(overrides: Partial<PostmortemRow> = {}): PostmortemRow {
    return {
      id: "p1",
      state: "generated",
      summary: "The container ran out of memory, was restarted automatically, and recovered.",
      recommendations: ["Raise the memory limit to 512Mi."],
      model: "ollama-primary",
      actions_considered: 1,
      created_at: "2026-09-23T00:10:00Z",
      ...overrides,
    };
  }

  it("renders no empty summary block when nothing was generated", async () => {
    get.mockResolvedValue({
      postmortems: [postmortem({ state: "unavailable", summary: "", recommendations: [] })],
      explanation: "1 post-incident record(s).",
    });
    mount(<PostmortemDisplay incidentId="i1" />);
    const note = await screen.findByTestId("postmortem-unavailable");
    expect(note.textContent).toContain("missing is the narrative, not the facts");
    expect(screen.queryByTestId("postmortem-summary")).toBeNull();
  });

  it("distinguishes no record from a record recommending nothing", async () => {
    get.mockResolvedValue({
      postmortems: [postmortem({ recommendations: [] })],
      explanation: "1 post-incident record(s).",
    });
    mount(<PostmortemDisplay incidentId="i1" />);
    const none = await screen.findByTestId("postmortem-no-recommendations");
    expect(none.textContent).toContain("a conclusion rather than a gap");
    expect(screen.queryByTestId("postmortem-recommendations")).toBeNull();
  });

  it("says nothing was written when there is no record at all", async () => {
    get.mockResolvedValue({
      postmortems: [],
      explanation:
        "No post-incident record has been written. The incident, its evidence and its healing actions are all readable directly; what is absent is the narrative, not the facts.",
    });
    mount(<PostmortemDisplay incidentId="i1" />);
    const none = await screen.findByTestId("postmortem-none");
    expect(none.textContent).toContain("absent is the narrative, not the facts");
  });

  it("states that recommendations cannot execute themselves", async () => {
    get.mockResolvedValue({ postmortems: [postmortem()], explanation: "1." });
    mount(<PostmortemDisplay incidentId="i1" />);
    const provenance = await screen.findByTestId("postmortem-provenance");
    expect(provenance.textContent).toContain("nothing here can execute itself");
  });

  it("renders the recommendations it has", async () => {
    get.mockResolvedValue({
      postmortems: [postmortem({ recommendations: ["Raise the limit.", "Add an alert."] })],
      explanation: "1.",
    });
    mount(<PostmortemDisplay incidentId="i1" />);
    const list = await screen.findByTestId("postmortem-recommendations");
    expect(list.querySelectorAll("li")).toHaveLength(2);
  });
});
