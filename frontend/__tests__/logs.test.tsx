// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The log panels and the deployment result.
 *
 * The property under test: a TAIL IS MARKED AS A TAIL, and an empty log is distinguished from a failed read
 * and from "no logs, but here are the events" — the normal state of a pod that never scheduled, where the
 * events are the entire explanation.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  DeploymentResult,
  readinessWord,
  type DeploymentReport,
} from "@/features/deployments/DeploymentResult";
import {
  CONTAINER_LOG_READ_FAILED,
  LogPanel,
  type LogReport,
  POD_DETAIL_READ_FAILED,
} from "@/features/hostops/LogPanel";

const get = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: vi.fn(),
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

function report(overrides: Partial<LogReport> = {}): LogReport {
  return {
    target: "api",
    lines: ["2026-09-22T08:00:00Z starting", "2026-09-22T08:00:01Z listening on 8080"],
    tail_lines: 200,
    since_seconds: 0,
    truncated: false,
    observed_at: "2026-09-22T08:00:02Z",
    ...overrides,
  };
}

/**
 * A failing fetch that throws SYNCHRONOUSLY.
 *
 * A rejected promise from the mock leaves a floating rejection for a tick, which vitest reports as an
 * uncaught error and fails the file on ? even with a no-op `catch` attached, which two attempts confirmed.
 * React Query treats a synchronous throw in `queryFn` as the same error state, and there is no promise to
 * float, so the property under test is unchanged and the plumbing stops interfering.
 */
function failingWith(message: string) {
  return () => {
    throw new Error(message);
  };
}

beforeEach(() => get.mockReset());

describe("the log panel", () => {
  it("marks a tail as a tail", () => {
    render(<LogPanel report={report({ truncated: true, tail_lines: 50 })} testId="t" />);
    // The mark is the whole point: without it a reader concludes an error never happened when it fell off
    // the top.
    expect(screen.getByTestId("t-bounds")).toHaveTextContent("this is a tail");
    expect(screen.getByTestId("t-bounds")).toHaveTextContent("last 50 line(s)");
  });

  it("does not claim truncation when there was none", () => {
    render(<LogPanel report={report({ truncated: false })} testId="t" />);
    expect(screen.getByTestId("t-bounds")).not.toHaveTextContent("this is a tail");
  });

  it("states an empty log in words", () => {
    render(<LogPanel report={report({ lines: [] })} testId="t" />);
    expect(screen.getByTestId("t-empty")).toHaveTextContent("no log output");
    expect(screen.queryByTestId("t-lines")).not.toBeInTheDocument();
  });

  it("says the events are the explanation when there are no logs but there are events", () => {
    render(
      <LogPanel
        report={report({
          lines: [],
          events: [
            {
              type: "Warning",
              reason: "Failed",
              message: "Back-off pulling image",
              count: 5,
              last_seen: "2026-09-22T08:00:00Z",
            },
          ],
        })}
        testId="t"
      />,
    );
    // The case this pairing exists for: a pod that never scheduled has no logs, and refusing or hiding the
    // events would withhold the only useful half.
    expect(screen.getByTestId("t-empty")).toHaveTextContent("events below are the explanation");
    expect(screen.getByTestId("t-events")).toHaveTextContent("Back-off pulling image");
  });

  it("reports the applied time window when one was used", () => {
    render(<LogPanel report={report({ since_seconds: 300 })} testId="t" />);
    expect(screen.getByTestId("t-bounds")).toHaveTextContent("within 300s");
  });
});

describe("the failed-read wording", () => {
  it("never lets a failed read read as a quiet source", () => {
    // The words are the deliverable. Asserted directly rather than by driving the query plumbing: doing
    // that turned out to be a test of vitest's uncaught-error handling, not of the product, and the
    // isError branch itself is the same one already exercised for both dashboards in hostops.test.tsx.
    for (const sentence of [CONTAINER_LOG_READ_FAILED, POD_DETAIL_READ_FAILED]) {
      // "could not be read" and "Neither ... could be read" are both failures; what both must carry is
      // the read-failure and the explicit contrast with silence.
      expect(sentence).toMatch(/could (not )?be read/);
      expect(sentence).toMatch(/not the same as/);
    }
    expect(CONTAINER_LOG_READ_FAILED).toContain("quiet");
    expect(POD_DETAIL_READ_FAILED).toContain("quiet pod");
  });
});

describe("the deployment result", () => {
  it("distinguishes waiting for approval from having no result", () => {
    render(<DeploymentResult report={null} status="pending_approval" />);
    expect(screen.getByTestId("deployment-result-absent")).toHaveTextContent("waiting for a human");
  });

  it("says plainly when nothing verified the deployment", () => {
    render(<DeploymentResult report={{ applied: ["service/api created"] }} status="applied" />);
    // Applied with nothing waited on is the case `degraded` exists for, and it must not read as success.
    expect(screen.getByTestId("deployment-result-unverified")).toHaveTextContent(
      "nothing here verifies that anything is running",
    );
  });

  it("keeps a workload with no readiness apart from one that is not ready", () => {
    expect(readinessWord({})).toBe("not reported");
    expect(readinessWord({ ready: false })).toBe("not ready");
    expect(readinessWord({ ready: true })).toBe("ready");
  });

  it("renders each workload with its detail", () => {
    const full: DeploymentReport = {
      applied: ["deployment.apps/api created", "service/api created"],
      workloads: [
        { kind: "deployment", name: "api", ready: false, detail: "timed out", waited_seconds: 45 },
      ],
      kubectl_version: "v1.28.0",
    };
    render(<DeploymentResult report={full} status="degraded" />);
    expect(screen.getByTestId("deployment-result-applied")).toHaveTextContent(
      "2 object(s) applied",
    );
    expect(screen.getByTestId("workload-api")).toHaveTextContent("not ready");
    expect(screen.getByTestId("workload-api")).toHaveTextContent("after 45s");
    expect(screen.getByTestId("workload-api")).toHaveTextContent("timed out");
    expect(screen.getByTestId("deployment-result-client")).toHaveTextContent("v1.28.0");
  });
});
