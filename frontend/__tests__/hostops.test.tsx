// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * §2.4's and §2.9's dashboards, and the single property both exist to protect.
 *
 * EVERY PANEL HERE SHOWS A NUMBER A HUMAN ACTS ON. Restart that container; the host is out of memory; that
 * workload has no replicas. So a measurement the platform could not obtain must never render as a
 * measurement of zero, and a family the cluster refused to disclose must never render as a family with no
 * members. Those are the two mistakes these tests are for; everything else here is incidental.
 *
 * The three states, and the three different sentences:
 *
 *   never reported / not measured   nothing sampled this
 *   stale                           sampled, and older than the freshness window — shown WITH ITS AGE,
 *                                   because a number from four minutes ago is still information
 *   current                         sampled inside the window
 *
 * And for the cluster specifically: "no ingresses exist" and "you may not list ingresses" are opposite
 * facts that an empty table states identically, so `partial_reasons` is asserted to surface as a warning.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  type DockerInventory,
  DockerDashboard,
  freshnessOf,
  measurement,
} from "@/features/hostops/DockerDashboard";
import {
  count,
  type K8sInventory,
  KubernetesDashboard,
} from "@/features/hostops/KubernetesDashboard";

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

function dockerInventory(overrides: Partial<DockerInventory> = {}): DockerInventory {
  return {
    containers: [],
    images: [],
    volumes: [],
    networks: [{ name: "bridge", driver: "bridge" }],
    docker_version: "27.1.1",
    observed_at: new Date().toISOString(),
    stats_sampled: false,
    ...overrides,
  };
}

function container(overrides: Partial<DockerInventory["containers"][number]> = {}) {
  return {
    id: "aaa",
    name: "api",
    image: "nginx:1.27",
    state: "running",
    status: "Up 2 minutes",
    ports: "",
    cpu_percent: null,
    memory_bytes: null,
    memory_limit_bytes: null,
    network_rx_bytes: null,
    network_tx_bytes: null,
    ...overrides,
  };
}

function k8sInventory(overrides: Partial<K8sInventory> = {}): K8sInventory {
  return {
    cluster_context: "kind-forgeops",
    server_version: "v1.28.0",
    namespaces: ["default"],
    nodes: [],
    pods: [],
    workloads: [],
    services: [],
    ingresses: [],
    config_maps: [],
    horizontal_pod_autoscalers: [],
    observed_at: new Date().toISOString(),
    partial_reasons: [],
    ...overrides,
  };
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
});

describe("the freshness rule, shared by both dashboards", () => {
  it("separates never reported from stale from current", () => {
    const now = Date.parse("2026-09-21T12:00:00Z");
    expect(freshnessOf(null, now).kind).toBe("never-reported");
    expect(freshnessOf(undefined, now).kind).toBe("never-reported");
    expect(freshnessOf("2026-09-21T11:58:00Z", now).kind).toBe("stale");
    expect(freshnessOf("2026-09-21T11:59:45Z", now).kind).toBe("current");
  });

  it("treats a timestamp it cannot read as never reported, not as now", () => {
    // THE ONE MISTAKE THAT MAKES A STALE PANEL LOOK LIVE. Falling back to `Date.now()` for an
    // unparsable value would report every broken reading as fresh.
    expect(freshnessOf("not a timestamp").kind).toBe("never-reported");
    expect(freshnessOf("").kind).toBe("never-reported");
  });

  it("reports the age of a stale reading, because an old number is still information", () => {
    const now = Date.parse("2026-09-21T12:00:00Z");
    const stale = freshnessOf("2026-09-21T11:55:00Z", now);
    expect(stale.kind).toBe("stale");
    expect(stale.kind === "stale" && stale.ageSeconds).toBe(300);
  });
});

describe("a measurement that was not taken", () => {
  it("is words, not a zero", () => {
    expect(measurement(null, (v) => `${v}%`)).toBe("not measured");
    // AND A REAL ZERO IS A ZERO. An idle container reports 0 and must be shown as idle, or the
    // distinction has been destroyed from the other direction.
    expect(measurement(0, (v) => `${v}%`)).toBe("0%");
  });

  it("applies to a cluster count as well", () => {
    expect(count(null)).toBe("not reported");
    expect(count(0)).toBe("0");
  });
});

describe("the Docker dashboard", () => {
  it("shows loading as loading, not as a host running nothing", async () => {
    get.mockReturnValue(new Promise(() => {}));
    mount(<DockerDashboard projectId="p1" />);
    expect(await screen.findByTestId("docker-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("docker-containers-empty")).not.toBeInTheDocument();
  });

  it("shows a failure as a failure, and says it is not the same as an empty host", async () => {
    get.mockRejectedValue(new Error("the agent did not answer within 60s"));
    mount(<DockerDashboard projectId="p1" />);
    const error = await screen.findByTestId("docker-error");
    expect(error).toHaveTextContent("did not report");
    expect(error).toHaveTextContent("not the same as the host running nothing");
    expect(screen.queryByTestId("docker-containers")).not.toBeInTheDocument();
  });

  it("renders an unsampled container's figures as not measured", async () => {
    get.mockResolvedValue(dockerInventory({ containers: [container()] }));
    mount(<DockerDashboard projectId="p1" />);
    expect(await screen.findByTestId("docker-cpu-api")).toHaveTextContent("not measured");
    expect(screen.getByTestId("docker-memory-api")).toHaveTextContent("not measured");
    // And it SAYS why, rather than leaving a reader to wonder.
    expect(screen.getByTestId("docker-stats-absent")).toBeInTheDocument();
  });

  it("renders a real zero as a zero", async () => {
    get.mockResolvedValue(
      dockerInventory({
        stats_sampled: true,
        containers: [
          container({
            cpu_percent: 0,
            memory_bytes: 0,
            memory_limit_bytes: 2 * 1024 * 1024 * 1024,
          }),
        ],
      }),
    );
    mount(<DockerDashboard projectId="p1" />);
    // AN IDLE CONTAINER IS IDLE. If this read "not measured", the distinction would be broken from the
    // other side and a working host would look unmonitored.
    expect(await screen.findByTestId("docker-cpu-api")).toHaveTextContent("0.00%");
    expect(screen.getByTestId("docker-memory-api")).toHaveTextContent("0 B of 2.0 GiB");
    expect(screen.queryByTestId("docker-stats-absent")).not.toBeInTheDocument();
  });

  it("marks a stale reading as stale with its age", async () => {
    get.mockResolvedValue(
      dockerInventory({ observed_at: new Date(Date.now() - 300_000).toISOString() }),
    );
    mount(<DockerDashboard projectId="p1" />);
    expect(await screen.findByTestId("docker-freshness")).toHaveTextContent("stale");
  });

  it("says never reported when the agent sent no timestamp", async () => {
    get.mockResolvedValue(dockerInventory({ observed_at: "" }));
    mount(<DockerDashboard projectId="p1" />);
    expect(await screen.findByTestId("docker-freshness")).toHaveTextContent("never reported");
  });

  it("asks for stats under a different cache key when the toggle is used", async () => {
    get.mockResolvedValue(dockerInventory({ containers: [container()] }));
    mount(<DockerDashboard projectId="p1" />);
    await screen.findByTestId("docker-containers");
    await userEvent.click(screen.getByTestId("docker-stats-toggle"));
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/projects/p1/docker/inventory?stats=true");
    });
    // THE STATLESS CALL IS STILL THE OTHER CALL. One key for both would serve every-figure-null to the
    // panel that asked to measure.
    expect(get).toHaveBeenCalledWith("/projects/p1/docker/inventory");
  });

  it("reports a governance outcome for an action rather than a result", async () => {
    get.mockResolvedValue(dockerInventory({ containers: [container()] }));
    post.mockResolvedValue({
      change_set_id: "cs-1",
      status: "pending_approval",
      outcome: "approval-required",
    });
    mount(<DockerDashboard projectId="p1" />);
    await userEvent.click(await screen.findByTestId("docker-restart-api"));
    const outcome = await screen.findByTestId("docker-last-outcome");
    // THE WORDING MATTERS: an operator must not believe the container restarted when an approver has
    // not looked at it yet.
    expect(outcome).toHaveTextContent("Waiting for a human");
    expect(outcome).toHaveTextContent("Nothing has changed on the host yet");
  });

  it("asks before the irreversible action and not before the reversible ones", async () => {
    get.mockResolvedValue(dockerInventory({ containers: [container()] }));
    post.mockResolvedValue({ change_set_id: "cs-1", status: "approved", outcome: "applying" });
    mount(<DockerDashboard projectId="p1" />);

    // A restart goes straight through: doing it twice is harmless.
    await userEvent.click(await screen.findByTestId("docker-restart-api"));
    expect(post).toHaveBeenCalledWith("/projects/p1/docker/containers/actions", {
      action: "restart",
      container: "api",
    });

    post.mockClear();
    // A removal asks first, and asking is not sending.
    await userEvent.click(screen.getByTestId("docker-remove-api"));
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByTestId("docker-remove-confirm-api")).toHaveTextContent("is lost");
    await userEvent.click(screen.getByTestId("docker-remove-yes-api"));
    expect(post).toHaveBeenCalledWith("/projects/p1/docker/containers/actions", {
      action: "remove",
      container: "api",
    });
  });

  it("distinguishes an empty host from an unreachable one in words", async () => {
    get.mockResolvedValue(dockerInventory());
    mount(<DockerDashboard projectId="p1" />);
    const empty = await screen.findByTestId("docker-containers-empty");
    // "reached the daemon" is the load-bearing phrase: it states that the absence is a fact about the
    // host rather than about the platform.
    expect(empty).toHaveTextContent("reached the daemon");
  });
});

describe("the Kubernetes dashboard", () => {
  it("renders an unreadable family as a warning and not as an empty list", async () => {
    get.mockResolvedValue(
      k8sInventory({
        partial_reasons: ['ingresses: forbidden - User "dev" cannot list resource "ingresses"'],
      }),
    );
    mount(<KubernetesDashboard projectId="p1" />);
    const warning = await screen.findByTestId("k8s-partial");
    expect(warning).toHaveTextContent("could not be read");
    expect(warning).toHaveTextContent("cannot list");
    // The word that matters: incomplete, not empty.
    expect(warning).toHaveTextContent("incomplete rather than");
  });

  it("says nothing about partial reads when everything was readable", async () => {
    get.mockResolvedValue(k8sInventory());
    mount(<KubernetesDashboard projectId="p1" />);
    // Awaited on a DATA-DEPENDENT element. `k8s-dashboard` is present during loading too, so waiting for
    // it proves nothing about whether the payload arrived — the first version of this test passed while
    // still showing the spinner, which would have made the assertion below vacuous.
    expect(await screen.findByTestId("k8s-ingresses")).toHaveTextContent("No ingresses");
    expect(screen.queryByTestId("k8s-partial")).not.toBeInTheDocument();
  });

  it("renders a node with no Ready condition as not having reported", async () => {
    get.mockResolvedValue(
      k8sInventory({
        nodes: [
          {
            name: "node-a",
            ready: null,
            kubelet_version: "v1.28.0",
            os_image: "Ubuntu",
            allocatable: { cpu: "4", memory: "8Gi" },
          },
          {
            name: "node-b",
            ready: false,
            kubelet_version: "v1.28.0",
            os_image: "Ubuntu",
            allocatable: { cpu: "4", memory: "8Gi" },
          },
        ],
      }),
    );
    mount(<KubernetesDashboard projectId="p1" />);
    // THREE STATES, THREE WORDS. "has not reported" would page nobody; "NotReady" pages somebody. They
    // must not be the same string.
    expect(await screen.findByTestId("k8s-node-ready-node-a")).toHaveTextContent(
      "has not reported",
    );
    expect(screen.getByTestId("k8s-node-ready-node-b")).toHaveTextContent("NotReady");
  });

  it("renders an unpopulated replica count as not reported rather than zero", async () => {
    get.mockResolvedValue(
      k8sInventory({
        workloads: [
          {
            namespace: "default",
            kind: "deployment",
            name: "api",
            desired_replicas: 0,
            ready_replicas: null,
            images: ["nginx:1.27"],
          },
        ],
      }),
    );
    mount(<KubernetesDashboard projectId="p1" />);
    // A workload at zero replicas reports NO readyReplicas, and so does one nothing has observed. "0 / 0"
    // would make a healthy scaled-down workload look broken and a broken one look scaled down.
    expect(await screen.findByTestId("k8s-replicas-api")).toHaveTextContent("not reported / 0");
  });

  it("shows a pod's readiness as a ratio and surfaces its waiting reason", async () => {
    get.mockResolvedValue(
      k8sInventory({
        pods: [
          {
            namespace: "default",
            name: "api-abc",
            phase: "Pending",
            ready_containers: 1,
            total_containers: 2,
            restarts: 7,
            node: "node-a",
            reason: "ImagePullBackOff",
            started_at: "2026-09-21T11:00:00Z",
          },
        ],
      }),
    );
    mount(<KubernetesDashboard projectId="p1" />);
    // "1/2" is a sidecar problem and "0/2" is a different one; a boolean would lose that.
    expect(await screen.findByTestId("k8s-pod-ready-api-abc")).toHaveTextContent("1/2");
    expect(screen.getByTestId("k8s-pod-restarts-api-abc")).toHaveTextContent("7");
    // The reason is what an operator acts on.
    expect(screen.getByTestId("k8s-pod-reason-api-abc")).toHaveTextContent("ImagePullBackOff");
  });

  it("never shows a config map's values, and says so", async () => {
    get.mockResolvedValue(
      k8sInventory({
        config_maps: [
          { namespace: "default", name: "app-config", keys: ["DATABASE_URL", "LOG_LEVEL"] },
        ],
      }),
    );
    mount(<KubernetesDashboard projectId="p1" />);
    const list = await screen.findByTestId("k8s-configmaps");
    expect(list).toHaveTextContent("DATABASE_URL");
    // Stated explicitly, because a reader who does not know values are withheld may assume the key list
    // is all there is.
    expect(list).toHaveTextContent("values are not read");
  });

  it("sends a scale through the governance surface and reports its outcome", async () => {
    get.mockResolvedValue(
      k8sInventory({
        workloads: [
          {
            namespace: "default",
            kind: "deployment",
            name: "api",
            desired_replicas: 1,
            ready_replicas: 1,
            images: ["nginx:1.27"],
          },
        ],
      }),
    );
    post.mockResolvedValue({ change_set_id: "cs-9", status: "approved", outcome: "applying" });
    mount(<KubernetesDashboard projectId="p1" />);
    await userEvent.click(await screen.findByTestId("k8s-scale-api"));
    expect(post).toHaveBeenCalledWith(
      "/projects/p1/kubernetes/workloads/actions",
      expect.objectContaining({
        action: "scale",
        kind: "deployment",
        name: "api",
        namespace: "default",
      }),
    );
    expect(await screen.findByTestId("k8s-last-outcome")).toHaveTextContent("Sent to the agent");
  });

  it("scopes the read by namespace when one is chosen", async () => {
    get.mockResolvedValue(k8sInventory({ namespaces: ["default", "production"] }));
    mount(<KubernetesDashboard projectId="p1" />);
    await screen.findByTestId("k8s-namespace-select");
    await userEvent.selectOptions(screen.getByTestId("k8s-namespace-select"), "production");
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/projects/p1/kubernetes/inventory?namespace=production");
    });
  });

  it("shows a failure as a failure, not as an empty cluster", async () => {
    get.mockRejectedValue(new Error("no Kubernetes cluster answered"));
    mount(<KubernetesDashboard projectId="p1" />);
    const error = await screen.findByTestId("k8s-error");
    expect(error).toHaveTextContent("not the same as an empty cluster");
    expect(screen.queryByTestId("k8s-workloads")).not.toBeInTheDocument();
  });

  it("says a cluster reporting no nodes is suspicious rather than normal", async () => {
    get.mockResolvedValue(k8sInventory({ nodes: [] }));
    mount(<KubernetesDashboard projectId="p1" />);
    // A cluster that answered has at least one node, so an empty list is a symptom and the panel says so
    // instead of rendering a blank table.
    expect(await screen.findByTestId("k8s-nodes-empty")).toHaveTextContent("has at least one");
  });
});
