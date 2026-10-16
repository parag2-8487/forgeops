/**
 * Learning history and preference panels. Phase 2 §2.13.
 *
 * THE ASSERTIONS THAT MATTER ARE ABOUT CORRECTABILITY. A preference store that cannot be corrected makes the
 * system quietly worse with every wrong inference, and a screen that shows preferences without offering a
 * change is only marginally better than one that hides them: the user learns the system is wrong and can do
 * nothing. So these tests check that the three actions exist, that they are described as doing different
 * things, and above all that an EXCLUDED preference is visible -- because that is the answer to "why did it
 * ignore what I told it", and it is invisible from output alone.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  LearningHistoryViewer,
  PreferenceDisplay,
  SkillFilePanel,
  type FeedbackRow,
  type PreferenceRow,
} from "@/features/learning/LearningPanels";

const get = vi.fn();
const patch = vi.fn();
const del = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    get: (...args: unknown[]) => get(...args),
    patch: (...args: unknown[]) => patch(...args),
    delete: (...args: unknown[]) => del(...args),
  },
}));

function mount(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

function preference(overrides: Partial<PreferenceRow> = {}): PreferenceRow {
  return {
    id: "p1",
    scope: "dockerfile",
    statement: "Pin every base image to an exact version.",
    source: "reflected",
    evidence_count: 4,
    active: true,
    created_at: "2026-09-23T00:00:00Z",
    updated_at: "2026-09-23T00:00:00Z",
    ...overrides,
  };
}

function feedback(overrides: Partial<FeedbackRow> = {}): FeedbackRow {
  return {
    id: "f1",
    generation_run_id: null,
    artifact_path: "Dockerfile",
    verdict: "rejected",
    comment: "",
    created_at: "2026-09-23T00:00:00Z",
    final_content_bytes: 0,
    ...overrides,
  };
}

beforeEach(() => {
  get.mockReset();
  patch.mockReset();
  del.mockReset();
  patch.mockResolvedValue({ explanation: "ok" });
  del.mockResolvedValue({ explanation: "ok" });
});

describe("the preference display is the correction mechanism", () => {
  it("offers correct, switch off and forget as three different things", async () => {
    get.mockResolvedValue({
      preferences: [preference()],
      scopes: ["dockerfile"],
      explanation: "1 active preference(s) of 1 recorded.",
    });
    mount(<PreferenceDisplay projectId="proj" />);
    await waitFor(() => expect(screen.getByTestId("preference-p1")).toBeInTheDocument());
    expect(screen.getByText("Correct")).toBeInTheDocument();
    expect(screen.getByTestId("preference-toggle-p1").textContent).toBe("Stop using this");
    expect(screen.getByTestId("preference-forget-p1").textContent).toBe("Forget entirely");
  });

  it("says a correction will not be undone by the next reflection", async () => {
    get.mockResolvedValue({
      preferences: [preference()],
      scopes: ["dockerfile"],
      explanation: "1 of 1.",
    });
    mount(<PreferenceDisplay projectId="proj" />);
    await waitFor(() => expect(screen.getByText("Correct")).toBeInTheDocument());
    await userEvent.click(screen.getByText("Correct"));
    expect(screen.getByText(/cannot overwrite the correction/)).toBeInTheDocument();
  });

  it("sends the corrected statement", async () => {
    get.mockResolvedValue({
      preferences: [preference()],
      scopes: ["dockerfile"],
      explanation: "1 of 1.",
    });
    mount(<PreferenceDisplay projectId="proj" />);
    await waitFor(() => expect(screen.getByText("Correct")).toBeInTheDocument());
    await userEvent.click(screen.getByText("Correct"));
    const box = screen.getByTestId("preference-draft-p1");
    await userEvent.clear(box);
    await userEvent.type(box, "Use a digest, not a tag.");
    await userEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0][1]).toEqual({ statement: "Use a digest, not a tag." });
  });

  it("switches a preference off without deleting it", async () => {
    get.mockResolvedValue({
      preferences: [preference()],
      scopes: ["dockerfile"],
      explanation: "1 of 1.",
    });
    mount(<PreferenceDisplay projectId="proj" />);
    await waitFor(() => expect(screen.getByTestId("preference-toggle-p1")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("preference-toggle-p1"));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0][1]).toEqual({ active: false });
    expect(del).not.toHaveBeenCalled();
  });

  it("lists an inactive preference rather than hiding it", async () => {
    get.mockResolvedValue({
      preferences: [preference({ active: false })],
      scopes: ["dockerfile"],
      explanation: "0 active preference(s) of 1 recorded.",
    });
    mount(<PreferenceDisplay projectId="proj" />);
    const state = await screen.findByTestId("preference-active-p1");
    expect(state.textContent).toContain("Switched off");
    expect(state.textContent).toContain("Kept so you can see it was inferred");
    expect(screen.getByTestId("preference-toggle-p1").textContent).toBe("Start using this again");
  });

  it("warns that a single-event inference is a guess", async () => {
    get.mockResolvedValue({
      preferences: [preference({ evidence_count: 1 })],
      scopes: ["dockerfile"],
      explanation: "1 of 1.",
    });
    mount(<PreferenceDisplay projectId="proj" />);
    const provenance = await screen.findByTestId("preference-provenance-p1");
    expect(provenance.textContent).toContain("treat it as a guess");
  });

  it("distinguishes a stated preference from an inferred one", async () => {
    get.mockResolvedValue({
      preferences: [preference({ source: "stated", evidence_count: 0 })],
      scopes: ["dockerfile"],
      explanation: "1 of 1.",
    });
    mount(<PreferenceDisplay projectId="proj" />);
    const provenance = await screen.findByTestId("preference-provenance-p1");
    expect(provenance.textContent).toContain("You stated this");
    expect(provenance.textContent).toContain("will not rewrite it");
  });

  it("says nothing has been learned rather than showing an empty list", async () => {
    get.mockResolvedValue({
      preferences: [],
      scopes: [],
      explanation:
        "Nothing has been learned about this project yet. Prompts for it are identical to those for a project with no history -- which is the honest state before any feedback exists.",
    });
    mount(<PreferenceDisplay projectId="proj" />);
    const explanation = await screen.findByTestId("preferences-explanation");
    expect(explanation.textContent).toContain("honest state before any feedback exists");
    expect(screen.queryByRole("list")).toBeNull();
  });
});

describe("the skill-file panel answers 'why did it ignore my preference'", () => {
  it("shows excluded preferences as an alert", async () => {
    get.mockResolvedValue({
      content: "## What this project's maintainers prefer\n\n- [dockerfile] Pin base images.\n",
      included_preference_ids: ["p1"],
      excluded_preferences: [
        {
          id: "p2",
          scope: "kubernetes",
          statement: "Always set resource limits.",
          evidence_count: 3,
        },
      ],
      explanation:
        "1 preference(s) injected; 1 did not fit the 2000-character budget and were left out.",
    });
    mount(<SkillFilePanel projectId="proj" />);
    const excluded = await screen.findByTestId("skill-file-excluded");
    expect(excluded.getAttribute("role")).toBe("alert");
    expect(excluded.textContent).toContain("why did it ignore my preference");
    expect(screen.getByTestId("excluded-p2").textContent).toContain("Always set resource limits");
  });

  it("shows the exact text that would be injected", async () => {
    get.mockResolvedValue({
      content: "## What this project's maintainers prefer\n\n- [dockerfile] Pin base images.\n",
      included_preference_ids: ["p1"],
      excluded_preferences: [],
      explanation: "1 preference(s) injected.",
    });
    mount(<SkillFilePanel projectId="proj" />);
    const content = await screen.findByTestId("skill-file-content");
    expect(content.textContent).toContain("Pin base images.");
    expect(screen.queryByTestId("skill-file-excluded")).toBeNull();
  });

  it("says an empty skill file is the honest state, not a load failure", async () => {
    get.mockResolvedValue({
      content: "",
      included_preference_ids: [],
      excluded_preferences: [],
      explanation: "No active preferences, so nothing is injected.",
    });
    mount(<SkillFilePanel projectId="proj" />);
    const empty = await screen.findByTestId("skill-file-empty");
    expect(empty.textContent).toContain("not a failure to load");
  });
});

describe("the history viewer", () => {
  it("shows an edit as the most informative verdict, with its size", async () => {
    get.mockResolvedValue({
      feedback: [feedback({ verdict: "edited", final_content_bytes: 412 })],
      injections: [],
      explanation: "1 feedback event(s) and 0 recorded injection(s).",
    });
    mount(<LearningHistoryViewer projectId="proj" />);
    const verdict = await screen.findByTestId("feedback-verdict-f1");
    expect(verdict.textContent).toContain("kept it with changes (412 bytes)");
    expect(screen.getByText(/preference stated by demonstration/)).toBeInTheDocument();
  });

  it("renders an absent reason as words rather than a blank", async () => {
    get.mockResolvedValue({
      feedback: [feedback()],
      injections: [],
      explanation: "1 feedback event(s) and 0 recorded injection(s).",
    });
    mount(<LearningHistoryViewer projectId="proj" />);
    const none = await screen.findByTestId("feedback-no-comment-f1");
    expect(none.textContent).toBe("no reason given");
  });

  it("distinguishes never-told from told-and-ignored", async () => {
    get.mockResolvedValue({
      feedback: [],
      injections: [],
      explanation:
        "No feedback has been recorded and nothing has been injected. The system has not been told anything about this project, which is different from having been told and ignoring it.",
    });
    mount(<LearningHistoryViewer projectId="proj" />);
    const explanation = await screen.findByTestId("history-explanation");
    expect(explanation.textContent).toContain("different from having been told and ignoring it");
  });
});
