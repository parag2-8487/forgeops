// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * §2.2's dashboard, and the one thing it must not get wrong.
 *
 * A DEPLOYMENT HAS THREE OUTCOMES THAT LOOK LIKE TWO, and `degraded` is the one a dashboard usually
 * loses: the manifests reached the cluster and a workload did not converge. It is not a failure —
 * nothing needs retrying — and it is not a success, because the thing the operator asked for is not
 * running. A green tick on it would be the worst version of this codebase's recurring defect: a
 * plausible-looking status a human then acts on.
 *
 * So `healthy` is rendered as THREE states. `null` means nothing verified it, which happens while a
 * rollout is in flight and when an apply failed before anything could be checked; `false` means the
 * workloads were checked and were not ready. Showing those the same way would tell an operator that a
 * deployment currently mid-rollout has already failed.
 *
 * AND THE ROLLBACK PANEL OFFERS NOTHING WHEN THERE IS NOTHING. A rollback to a degraded state restores a
 * broken deployment while reporting success, so the server answers with the newest deployment whose
 * workloads actually converged — and the panel renders its absence as an absence rather than as a button.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  type Deployment,
  DeploymentDashboard,
  healthSentence,
  RollbackTargetPanel,
} from "@/features/deployments/DeploymentDashboard";

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
    },
  };
});

const PROJECT = "11111111-1111-4111-8111-111111111111";
const ENVIRONMENT = "bbbbbbbb-1111-4111-8111-111111111111";

function deployment(overrides: Partial<Deployment> = {}): Deployment {
  return {
    id: "dddddddd-1111-4111-8111-111111111111",
    environment_id: ENVIRONMENT,
    change_set_id: "cccccccc-1111-4111-8111-111111111111",
    status: "applied",
    healthy: true,
    stable: true,
    manifests: ["k8s/deployment.yaml", "k8s/service.yaml"],
    manifest_digest: "sha256:abc",
    cluster_context: "kind-forgeops",
    namespace: "default",
    report: null,
    created_at: "2026-09-21T10:00:00+00:00",
    completed_at: "2026-09-21T10:02:00+00:00",
    ...overrides,
  };
}

function renderWithQuery(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
});

describe("the three health states are three different sentences", () => {
  it("names a converged deployment", () => {
    expect(healthSentence(deployment({ healthy: true }))).toMatch(/every workload converged/i);
  });

  it("names a degraded one as applied but not converged, not as failed", () => {
    const sentence = healthSentence(deployment({ status: "degraded", healthy: false }));
    expect(sentence).toMatch(/applied/i);
    expect(sentence).toMatch(/did not converge/i);
    expect(sentence).not.toMatch(/refused/i);
  });

  it("distinguishes not-yet-verified from checked-and-unready", () => {
    const inFlight = healthSentence(deployment({ status: "applying", healthy: null }));
    const unready = healthSentence(deployment({ status: "degraded", healthy: false }));
    expect(inFlight).toMatch(/no workload has been verified/i);
    expect(inFlight).not.toEqual(unready);
  });

  it("says a failed apply checked nothing", () => {
    const sentence = healthSentence(deployment({ status: "failed", healthy: null }));
    expect(sentence).toMatch(/refused/i);
    expect(sentence).toMatch(/no workload was checked/i);
  });
});

describe("the history", () => {
  it("shows each deployment's status and health sentence", async () => {
    get.mockImplementation((path: string) => {
      if (path.includes("/environments")) return Promise.resolve({ environments: [] });
      return Promise.resolve({
        deployments: [deployment({ id: "d1", status: "degraded", healthy: false, stable: false })],
      });
    });

    renderWithQuery(<DeploymentDashboard projectId={PROJECT} />);

    await waitFor(() => expect(screen.getByTestId("deployment-history")).toBeInTheDocument());
    expect(screen.getByTestId("deployment-status-d1")).toHaveTextContent("degraded");
    expect(screen.getByTestId("deployment-health-d1")).toHaveTextContent(/did not converge/i);
    // A degraded deployment is not offered as a stable state.
    expect(screen.queryByTestId("deployment-stable-d1")).not.toBeInTheDocument();
  });

  it("marks a healthy deployment as a state a rollback can return to", async () => {
    get.mockImplementation((path: string) => {
      if (path.includes("/environments")) return Promise.resolve({ environments: [] });
      return Promise.resolve({ deployments: [deployment({ id: "d2" })] });
    });

    renderWithQuery(<DeploymentDashboard projectId={PROJECT} />);

    await waitFor(() => expect(screen.getByTestId("deployment-stable-d2")).toBeInTheDocument());
  });

  it("distinguishes a failed load from never having deployed", async () => {
    get.mockImplementation((path: string) => {
      if (path.includes("/environments")) return Promise.resolve({ environments: [] });
      return Promise.reject(new Error("network down"));
    });

    renderWithQuery(<DeploymentDashboard projectId={PROJECT} />);

    await waitFor(() => expect(screen.getByTestId("deployments-error")).toBeInTheDocument());
    expect(screen.queryByTestId("deployments-empty")).not.toBeInTheDocument();
  });
});

describe("requesting a deployment", () => {
  it("cannot be submitted before an environment is chosen", async () => {
    get.mockImplementation((path: string) => {
      if (path.includes("/environments")) {
        return Promise.resolve({
          environments: [
            {
              id: ENVIRONMENT,
              name: "prod",
              kind: "production",
              k8s_context: null,
              requires_approval: true,
              position: 0,
            },
          ],
        });
      }
      return Promise.resolve({ deployments: [] });
    });

    renderWithQuery(<DeploymentDashboard projectId={PROJECT} />);

    await waitFor(() => expect(screen.getByTestId("environment-selector")).toBeInTheDocument());
    expect(screen.getByTestId("deploy-submit")).toBeDisabled();
  });

  it("says a human must approve when the server says so", async () => {
    get.mockImplementation((path: string) => {
      if (path.includes("/environments")) {
        return Promise.resolve({
          environments: [
            {
              id: ENVIRONMENT,
              name: "prod",
              kind: "production",
              k8s_context: "kind-forgeops",
              requires_approval: true,
              position: 0,
            },
          ],
        });
      }
      return Promise.resolve({ deployments: [] });
    });
    post.mockResolvedValue({
      deployment: deployment({ status: "pending_approval", healthy: null, stable: false }),
      change_set_id: "cs-1",
      outcome: "approval-required",
      requires_approval: true,
      environment: "prod",
      blast_radius_score: 4,
      blast_radius_verdict: "allow",
    });

    renderWithQuery(<DeploymentDashboard projectId={PROJECT} />);
    await waitFor(() => expect(screen.getByTestId("environment-selector")).toBeInTheDocument());
    await userEvent.selectOptions(screen.getByTestId("environment-selector"), ENVIRONMENT);
    await userEvent.click(screen.getByTestId("deploy-submit"));

    await waitFor(() => expect(screen.getByTestId("deployment-decision")).toBeInTheDocument());
    expect(screen.getByTestId("deployment-decision")).toHaveTextContent(
      /waiting for human approval/i,
    );
  });

  it("shows the server's refusal verbatim, because it names the bound", async () => {
    get.mockImplementation((path: string) => {
      if (path.includes("/environments")) {
        return Promise.resolve({
          environments: [
            {
              id: ENVIRONMENT,
              name: "dev",
              kind: "development",
              k8s_context: "kind-forgeops",
              requires_approval: false,
              position: 0,
            },
          ],
        });
      }
      return Promise.resolve({ deployments: [] });
    });
    post.mockRejectedValue({
      problem: {
        detail:
          "a deployment may carry at most 32 manifests and this one names 40. The agent enforces the same bound, so a larger set would produce a change set that could never be delivered.",
      },
    });

    renderWithQuery(<DeploymentDashboard projectId={PROJECT} />);
    await waitFor(() => expect(screen.getByTestId("environment-selector")).toBeInTheDocument());
    await userEvent.selectOptions(screen.getByTestId("environment-selector"), ENVIRONMENT);
    await userEvent.click(screen.getByTestId("deploy-submit"));

    await waitFor(() => expect(screen.getByTestId("deployment-problem")).toBeInTheDocument());
    expect(screen.getByTestId("deployment-problem")).toHaveTextContent(/at most 32 manifests/);
  });
});

describe("the rollback target", () => {
  it("offers nothing when the server says there is no stable state", async () => {
    get.mockResolvedValue({
      target: null,
      reason:
        "this environment has no healthy deployment on record, so there is no stable state to roll back to.",
    });

    renderWithQuery(<RollbackTargetPanel projectId={PROJECT} environmentId={ENVIRONMENT} />);

    await waitFor(() => expect(screen.getByTestId("rollback-none")).toBeInTheDocument());
    expect(screen.getByTestId("rollback-none")).toHaveTextContent(/no stable state/i);
    expect(screen.queryByTestId("rollback-target")).not.toBeInTheDocument();
  });

  it("describes what a rollback would restore", async () => {
    get.mockResolvedValue({
      target: deployment(),
      reason: "the newest deployment whose workloads converged",
    });

    renderWithQuery(<RollbackTargetPanel projectId={PROJECT} environmentId={ENVIRONMENT} />);

    await waitFor(() => expect(screen.getByTestId("rollback-target")).toBeInTheDocument());
    expect(screen.getByTestId("rollback-target")).toHaveTextContent(/2 manifest\(s\)/);
  });

  it("reports a read failure as a failure rather than as no target", async () => {
    get.mockRejectedValue(new Error("boom"));

    renderWithQuery(<RollbackTargetPanel projectId={PROJECT} environmentId={ENVIRONMENT} />);

    await waitFor(() => expect(screen.getByTestId("rollback-error")).toBeInTheDocument());
    expect(screen.queryByTestId("rollback-none")).not.toBeInTheDocument();
  });
});
