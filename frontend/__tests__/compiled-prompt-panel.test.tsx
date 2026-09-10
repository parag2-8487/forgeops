/**
 * The instruction a run gave the model must be readable from the run.
 *
 * A run could be judged only by its output. The record held the tier, the endpoint, the token counts and
 * the outcome — everything except what the model was asked to do. So when a run wrote a file to the wrong
 * path there was no way to tell whether the model had disobeyed a correct instruction or obeyed a bad
 * one, and those two faults have opposite fixes.
 *
 * The backend column was written by the compiler and read by nothing. These tests are what stops it
 * becoming a dead column again: they fail if the panel stops reading it.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CompiledPromptPanel } from "@/features/generation/CompiledPromptPanel";

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, get: mockGet } };
});

const RUN = "11111111-1111-1111-1111-111111111111";

const PROMPT = [
  "## 1. ESTABLISHED FACTS ABOUT THIS REPOSITORY",
  "- language: python (declared in requirements.txt)",
  "",
  "## 2. WHAT TO PRODUCE",
  "### MODIFY `Dockerfile`",
  "- line 3: the image runs as root",
].join("\n");

function record(overrides: Record<string, unknown> = {}) {
  return {
    id: RUN,
    status: "accepted",
    compiled_prompt: PROMPT,
    prompt_token_estimate: 900,
    prompt_token_budget: 24000,
    addressed_checks: ["dockerfile_runs_as_non_root"],
    deferred_checks: [],
    ...overrides,
  };
}

async function reveal() {
  render(<CompiledPromptPanel runId={RUN} />);
  await userEvent.click(screen.getByTestId("show-compiled-prompt"));
}

beforeEach(() => {
  mockGet.mockReset();
});

describe("the instruction is not shown until it is asked for", () => {
  it("renders nothing but the control at first", () => {
    render(<CompiledPromptPanel runId={RUN} />);
    // The prompt runs to thousands of tokens; unfurling it over the artifacts a user came to review
    // would bury them.
    expect(screen.queryByTestId("compiled-prompt")).toBeNull();
    expect(screen.getByTestId("show-compiled-prompt")).toBeInTheDocument();
  });

  it("does not spend a request until the control is used", () => {
    render(<CompiledPromptPanel runId={RUN} />);
    expect(mockGet).not.toHaveBeenCalled();
  });
});

describe("what the panel shows", () => {
  it("shows the prompt exactly as it was sent", async () => {
    mockGet.mockResolvedValue(record());
    await reveal();
    // Byte-for-byte. A paraphrase cannot be used to diagnose a bad instruction.
    await waitFor(() => expect(screen.getByTestId("compiled-prompt")).toHaveTextContent(/root/));
    expect(screen.getByTestId("compiled-prompt").textContent).toBe(PROMPT);
  });

  it("reads the run it was given", async () => {
    mockGet.mockResolvedValue(record());
    await reveal();
    // THE PATH IS BARE, and this assertion is why the defect shipped: it agreed with the component
    // rather than with the client. `api.get` prepends `NEXT_PUBLIC_API_BASE_URL`, which ends in
    // `/api/v1`, so the old `/api/v1/generation/runs/...` became `/api/v1/api/v1/...` and answered 404.
    // Mocking `api.get` hid it, because a mock has no base URL to double up.
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(`/generation/runs/${RUN}`));
  });

  it("reports the estimate beside the budget", async () => {
    mockGet.mockResolvedValue(record());
    await reveal();
    // A prompt that dropped a section is only explicable next to the limit that forced the drop.
    await waitFor(() =>
      expect(screen.getByTestId("prompt-token-summary")).toHaveTextContent(/900.*24000/),
    );
  });

  it("names the checks the run set out to fix", async () => {
    mockGet.mockResolvedValue(record());
    await reveal();
    await waitFor(() =>
      expect(screen.getByTestId("addressed-checks")).toHaveTextContent(
        /dockerfile_runs_as_non_root/,
      ),
    );
  });

  it("distinguishes a deferred check from an attempted one", async () => {
    mockGet.mockResolvedValue(
      record({ deferred_checks: ["kubernetes_probes_declared", "helm_chart_present"] }),
    );
    await reveal();
    // A user looking at a score that did not move must learn the check was never attempted. Only that
    // case is fixed by narrowing the request; the other needs a different artifact or a better model.
    await waitFor(() =>
      expect(screen.getByTestId("deferred-checks")).toHaveTextContent(
        /kubernetes_probes_declared, helm_chart_present/,
      ),
    );
  });

  it("hides the checks rows when there are none rather than showing empty labels", async () => {
    mockGet.mockResolvedValue(record({ addressed_checks: [], deferred_checks: [] }));
    await reveal();
    await waitFor(() => expect(screen.getByTestId("compiled-prompt")).toBeInTheDocument());
    expect(screen.queryByTestId("addressed-checks")).toBeNull();
    expect(screen.queryByTestId("deferred-checks")).toBeNull();
  });
});

describe("a run with no recorded instruction", () => {
  it("says nothing was recorded rather than showing an empty box", async () => {
    mockGet.mockResolvedValue(record({ compiled_prompt: null }));
    await reveal();
    // "Not recorded" and "the model was sent nothing" are different claims, and only the first is true.
    await waitFor(() =>
      expect(screen.getByTestId("compiled-prompt-absent")).toHaveTextContent(/free-text prompt/i),
    );
    expect(screen.queryByTestId("compiled-prompt")).toBeNull();
  });
});

describe("when the run cannot be read", () => {
  it("prefers the server's own explanation to a guess", async () => {
    const { ApiProblemError } = await import("@/lib/api");
    mockGet.mockRejectedValue(
      new ApiProblemError({
        type: "https://forgeops.dev/problems/forbidden",
        title: "Forbidden",
        status: 403,
        detail: "You do not have access to this resource.",
      }),
    );
    await reveal();
    // The route answers 403 for another tenant's run AND for one that does not exist, deliberately, so
    // the panel cannot say which it was and must not invent a reason.
    await waitFor(() =>
      expect(screen.getByTestId("compiled-prompt-error")).toHaveTextContent(
        /do not have access to this resource/i,
      ),
    );
  });

  it("reports a transport failure as a transport failure", async () => {
    mockGet.mockRejectedValue(new TypeError("network down"));
    await reveal();
    await waitFor(() =>
      expect(screen.getByTestId("compiled-prompt-error")).toHaveTextContent(/could not reach/i),
    );
  });
});

describe("re-reading", () => {
  it("toggles without spending a second request", async () => {
    mockGet.mockResolvedValue(record());
    await reveal();
    await waitFor(() => expect(screen.getByTestId("compiled-prompt")).toBeInTheDocument());

    await userEvent.click(screen.getByTestId("show-compiled-prompt"));
    expect(screen.queryByTestId("compiled-prompt")).toBeNull();

    await userEvent.click(screen.getByTestId("show-compiled-prompt"));
    await waitFor(() => expect(screen.getByTestId("compiled-prompt")).toBeInTheDocument());
    // A run's prompt is written before the stream and never updated, so re-reading it would spend a
    // request to redisplay text that cannot have changed.
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});
