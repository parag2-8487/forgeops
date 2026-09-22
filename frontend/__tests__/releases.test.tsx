// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * §2.3's timeline and comparison. Two properties: health is three states in words, and a rollback button
 * exists only for a deployment the server called stable — offering one for a degraded deployment would
 * restore a broken state while reporting success.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  healthWord,
  ReleaseTimeline,
  type TimelineDeployment,
} from "@/features/releases/ReleaseTimeline";

const get = vi.fn();
const post = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
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

function deployment(overrides: Partial<TimelineDeployment> = {}): TimelineDeployment {
  return {
    id: "d1",
    environment_id: "e1",
    environment: "staging",
    change_set_id: "cs1",
    status: "applied",
    healthy: true,
    stable: true,
    manifests: ["k8s/deployment.yaml"],
    manifest_digest: "sha256:aaa",
    cluster_context: "kind-forgeops",
    namespace: "default",
    created_at: "2026-09-22T08:00:00Z",
    completed_at: "2026-09-22T08:02:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
});

describe("the health wording", () => {
  it("has a distinct word for each of the three states", () => {
    // The point of the tri-state: "not verified" must not read as a failure.
    expect(healthWord(null)).toBe("not verified");
    expect(healthWord(false)).toBe("did not converge");
    expect(healthWord(true)).toBe("converged");
  });
});

describe("the timeline", () => {
  it("shows loading as loading rather than as never having deployed", async () => {
    get.mockReturnValue(new Promise(() => {}));
    mount(<ReleaseTimeline projectId="p1" />);
    expect(await screen.findByTestId("timeline-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("timeline-empty")).not.toBeInTheDocument();
  });

  it("distinguishes a read failure from an empty history", async () => {
    get.mockRejectedValue(new Error("the database did not answer"));
    mount(<ReleaseTimeline projectId="p1" />);
    expect(await screen.findByTestId("timeline-error")).toHaveTextContent("not the same as never");
  });

  it("offers a rollback only for a stable deployment", async () => {
    get.mockResolvedValue({
      deployments: [
        deployment({ id: "good", stable: true, healthy: true }),
        deployment({ id: "bad", stable: false, healthy: false, status: "degraded" }),
      ],
      count: 2,
      limit: 50,
    });
    mount(<ReleaseTimeline projectId="p1" />);
    expect(await screen.findByTestId("timeline-rollback-good")).toBeInTheDocument();
    // NO BUTTON for the degraded one, and the reason is stated where the button would have been.
    expect(screen.queryByTestId("timeline-rollback-bad")).not.toBeInTheDocument();
    expect(screen.getByTestId("timeline-unstable-bad")).toHaveTextContent("never converged");
  });

  it("renders an in-flight deployment as not verified rather than failed", async () => {
    get.mockResolvedValue({
      deployments: [deployment({ id: "flight", healthy: null, stable: false, status: "applying" })],
      count: 1,
      limit: 50,
    });
    mount(<ReleaseTimeline projectId="p1" />);
    expect(await screen.findByTestId("timeline-health-flight")).toHaveTextContent("not verified");
  });

  it("reports a governance outcome for a rollback rather than a result", async () => {
    get.mockResolvedValue({ deployments: [deployment({ id: "good" })], count: 1, limit: 50 });
    post.mockResolvedValue({ outcome: "approval-required" });
    mount(<ReleaseTimeline projectId="p1" />);
    await userEvent.click(await screen.findByTestId("timeline-rollback-good"));
    expect(post).toHaveBeenCalledWith("/projects/p1/releases/rollback", {
      environment_id: "e1",
      deployment_id: "good",
    });
    expect(await screen.findByTestId("timeline-outcome")).toHaveTextContent("Waiting for a human");
  });

  it("promotes an environment through the promote route", async () => {
    get.mockResolvedValue({ deployments: [deployment({ id: "good" })], count: 1, limit: 50 });
    post.mockResolvedValue({ outcome: "applying" });
    mount(<ReleaseTimeline projectId="p1" />);
    await userEvent.click(await screen.findByTestId("timeline-promote-e1"));
    expect(post).toHaveBeenCalledWith("/projects/p1/releases/promote", {
      source_environment_id: "e1",
    });
  });
});

describe("the comparison", () => {
  it("asks for nothing until two different deployments are chosen", async () => {
    get.mockResolvedValue({
      deployments: [deployment({ id: "a" }), deployment({ id: "b" })],
      count: 2,
      limit: 50,
    });
    mount(<ReleaseTimeline projectId="p1" />);
    expect(await screen.findByTestId("diff-unselected")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("timeline-left-a"));
    // One side chosen is still not a comparison, and no request has gone out for one.
    expect(screen.getByTestId("diff-unselected")).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("says so rather than comparing a deployment with itself", async () => {
    get.mockResolvedValue({ deployments: [deployment({ id: "a" })], count: 1, limit: 50 });
    mount(<ReleaseTimeline projectId="p1" />);
    await userEvent.click(await screen.findByTestId("timeline-left-a"));
    await userEvent.click(screen.getByTestId("timeline-right-a"));
    expect(screen.getByTestId("diff-same")).toBeInTheDocument();
  });

  it("shows what was added, removed and how health changed", async () => {
    get.mockImplementation((path: string) => {
      if (path.includes("/diff")) {
        return Promise.resolve({
          left: deployment({ id: "a", healthy: true }),
          right: deployment({ id: "b", healthy: false }),
          manifests_added: ["k8s/ingress.yaml"],
          manifests_removed: [],
          manifests_unchanged: ["k8s/deployment.yaml"],
          identical_manifests: false,
          cluster_context_changed: false,
          namespace_changed: false,
          health_changed: true,
        });
      }
      return Promise.resolve({
        deployments: [deployment({ id: "a" }), deployment({ id: "b" })],
        count: 2,
        limit: 50,
      });
    });
    mount(<ReleaseTimeline projectId="p1" />);
    await userEvent.click(await screen.findByTestId("timeline-left-a"));
    await userEvent.click(screen.getByTestId("timeline-right-b"));
    await waitFor(() => expect(screen.getByTestId("diff-result")).toBeInTheDocument());
    expect(screen.getByTestId("diff-added")).toHaveTextContent("added k8s/ingress.yaml");
    expect(screen.getByTestId("diff-removed")).toHaveTextContent("Nothing removed");
    // The fact an operator comparing two releases is usually after.
    expect(screen.getByTestId("diff-health")).toHaveTextContent("converged → did not converge");
  });
});
