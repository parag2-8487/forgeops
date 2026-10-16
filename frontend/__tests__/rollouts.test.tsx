/**
 * The progressive rollout panel. Phase 2 §2.7a.
 *
 * EVERY TEST HERE IS ABOUT A PANEL THAT COULD LIE. A canary weight is a number an operator acts on — they
 * promote, they abort, they wait — so "not reported" and "0%" must be different sentences, and an empty
 * analysis list must not read as "all checks passed".
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RolloutPanel, type RolloutDetail } from "@/features/rollouts/RolloutPanel";

const get = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    get: (...args: unknown[]) => get(...args),
  },
}));

function mount(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

function detail(overrides: Partial<RolloutDetail> = {}): RolloutDetail {
  return {
    rollout: "api",
    namespace: "default",
    strategy: "canary",
    phase: "Progressing",
    message: "",
    canary_weight: 20,
    current_step: 0,
    total_steps: 3,
    replicas: 3,
    updated_replicas: 1,
    ready_replicas: 3,
    available_replicas: 3,
    stable_revision: "abc123",
    canary_revision: "def456",
    analysis_runs: [],
    observed_at: new Date().toISOString(),
    ...overrides,
  };
}

beforeEach(() => {
  get.mockReset();
});

describe("a weight the cluster did not report", () => {
  it("says so in words instead of showing a percentage", async () => {
    get.mockResolvedValue(detail({ canary_weight: -1 }));
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);

    const weight = await screen.findByTestId("rollout-weight");
    expect(weight).toHaveTextContent("not reported");
    // AND CRUCIALLY NOT A NUMBER. Showing 0% here would say the canary had not started when it might be
    // at 80%, and an operator would abort or promote on that.
    expect(weight.textContent).not.toMatch(/\d+%/);
  });

  it("distinguishes a real zero from an unreported weight", async () => {
    get.mockResolvedValue(detail({ canary_weight: 0 }));
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);

    const weight = await screen.findByTestId("rollout-weight");
    expect(weight).toHaveTextContent("0%");
    expect(weight).not.toHaveTextContent("not reported");
  });

  it("says when the position in the plan is unknown", async () => {
    get.mockResolvedValue(detail({ current_step: -1, total_steps: -1 }));
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);
    expect(await screen.findByTestId("rollout-step")).toHaveTextContent("was not reported");
  });

  it("renders the step 1-based, because step 0 of 3 reads as nothing having happened", async () => {
    get.mockResolvedValue(detail({ current_step: 0, total_steps: 3 }));
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);
    expect(await screen.findByTestId("rollout-step")).toHaveTextContent("step 1 of 3");
  });

  it("reports a completed rollout as complete", async () => {
    get.mockResolvedValue(detail({ current_step: 3, total_steps: 3 }));
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);
    expect(await screen.findByTestId("rollout-step")).toHaveTextContent("all 3 step(s) complete");
  });
});

describe("the analysis verdicts", () => {
  it("states that nothing is gating a rollout with no analysis runs", async () => {
    get.mockResolvedValue(detail({ analysis_runs: [] }));
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);

    const none = await screen.findByTestId("rollout-no-analysis");
    // An empty table would read as "all checks passed". The words have to say the opposite.
    expect(none).toHaveTextContent("Nothing is gating this rollout");
    expect(none).toHaveTextContent("not the same as the checks having passed");
    expect(screen.queryByTestId("rollout-analysis")).not.toBeInTheDocument();
  });

  it("reports Inconclusive as neither a pass nor a failure", async () => {
    get.mockResolvedValue(
      detail({
        analysis_runs: [
          {
            name: "api-gate-1",
            phase: "Inconclusive",
            started_at: new Date().toISOString(),
            metrics: [
              {
                name: "error-rate",
                phase: "Inconclusive",
                successful: 0,
                failed: 0,
                inconclusive: 2,
                error: 0,
                latest_value: "NaN",
              },
            ],
          },
        ],
      }),
    );
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);

    const run = await screen.findByTestId("rollout-run-api-gate-1");
    // AN IDLE SERVICE PRODUCES THIS. Treating it as a pass would promote a release nothing measured.
    expect(run).toHaveTextContent("inconclusive");
    expect(run).toHaveTextContent("this gate has not been satisfied");
    expect(run.textContent).not.toMatch(/passed/);

    const metric = screen.getByTestId("rollout-metric-error-rate");
    expect(metric).toHaveTextContent("2 inconclusive measurement(s)");
    expect(metric).toHaveTextContent("neither passes nor failures");
  });

  it("says a failed run aborts the rollout and returns traffic", async () => {
    get.mockResolvedValue(
      detail({
        analysis_runs: [
          {
            name: "api-gate-2",
            phase: "Failed",
            started_at: new Date().toISOString(),
            metrics: [
              {
                name: "p95-latency-seconds",
                phase: "Failed",
                successful: 1,
                failed: 1,
                inconclusive: 0,
                error: 0,
                latest_value: "1.8",
              },
            ],
          },
        ],
      }),
    );
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);

    const run = await screen.findByTestId("rollout-run-api-gate-2");
    expect(run).toHaveTextContent("FAILED");
    // The automatic rollback is the controller's, and the panel says so rather than implying the product
    // will do something.
    expect(run).toHaveTextContent("traffic returns to the stable version");
    expect(screen.getByTestId("rollout-metric-p95-latency-seconds")).toHaveTextContent(
      "latest 1.8",
    );
  });

  it("distinguishes an errored provider from a failed metric", async () => {
    get.mockResolvedValue(
      detail({
        analysis_runs: [
          {
            name: "api-gate-3",
            phase: "Error",
            started_at: new Date().toISOString(),
            metrics: [],
          },
        ],
      }),
    );
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);
    const run = await screen.findByTestId("rollout-run-api-gate-3");
    expect(run).toHaveTextContent("ERRORED");
    expect(run).toHaveTextContent("nothing was measured");
  });

  it("says when a metric has no measurement rather than showing a blank", async () => {
    get.mockResolvedValue(
      detail({
        analysis_runs: [
          {
            name: "api-gate-4",
            phase: "Running",
            started_at: new Date().toISOString(),
            metrics: [
              {
                name: "error-rate",
                phase: "Running",
                successful: 0,
                failed: 0,
                inconclusive: 0,
                error: 0,
                latest_value: "",
              },
            ],
          },
        ],
      }),
    );
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);
    expect(await screen.findByTestId("rollout-metric-error-rate")).toHaveTextContent(
      "no measurement recorded",
    );
  });
});

describe("the freshness and failure rules the other dashboards use", () => {
  it("shows loading as loading, not as a rollout with no progress", async () => {
    get.mockReturnValue(new Promise(() => {}));
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);
    expect(await screen.findByTestId("rollout-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("rollout-weight")).not.toBeInTheDocument();
  });

  it("says an unread rollout is not the same as a healthy or absent one", async () => {
    get.mockRejectedValue({ message: "the agent did not answer" });
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);
    const error = await screen.findByTestId("rollout-error");
    expect(error).toHaveTextContent("did not report");
    expect(error).toHaveTextContent("not the same as the rollout being healthy or absent");
    expect(screen.queryByTestId("rollout-weight")).not.toBeInTheDocument();
  });

  it("marks a stale reading as stale", async () => {
    get.mockResolvedValue(detail({ observed_at: new Date(Date.now() - 5 * 60_000).toISOString() }));
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);
    const stale = await screen.findByTestId("rollout-stale");
    expect(stale).toHaveTextContent("more than a minute old");
    expect(stale).toHaveTextContent("may have moved since");
  });

  it("carries the controller's message, which is the most useful field on the report", async () => {
    get.mockResolvedValue(
      detail({ phase: "Degraded", message: "ReplicaSet has timed out progressing" }),
    );
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);
    const phase = await screen.findByTestId("rollout-phase");
    // "Degraded" alone sends an operator to read logs.
    expect(phase).toHaveTextContent("Degraded");
    expect(phase).toHaveTextContent("ReplicaSet has timed out progressing");
  });

  it("reports replica tallies separately from the weight", async () => {
    get.mockResolvedValue(
      detail({
        canary_weight: 80,
        replicas: 5,
        ready_replicas: 2,
        updated_replicas: 4,
        available_replicas: 2,
      }),
    );
    mount(<RolloutPanel projectId="p1" namespace="default" rollout="api" />);
    // The weight is what the mesh was TOLD; the tallies are what is RUNNING. A panel showing only the
    // weight hides a rollout whose pods never became ready.
    expect(await screen.findByTestId("rollout-replicas")).toHaveTextContent("2 of 5 ready");
    expect(screen.getByTestId("rollout-weight")).toHaveTextContent("80%");
  });
});
