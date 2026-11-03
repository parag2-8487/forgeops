/**
 * The §2.10 monitoring panels. Phase 2 §2.10.
 *
 * EVERY TEST HERE IS ABOUT A PANEL THAT COULD LIE, and the specific lie is the worst one this codebase keeps
 * producing: a plausible number shown when the source cannot be reached. A cost figure is something an
 * operator acts on -- they cap a budget, they switch a model, they tell a customer what they owe -- so
 * "nothing has ever been reported", "the store is unreachable" and "the total is zero" must be three
 * different sentences on screen, not three renderings of `0`.
 *
 * The strongest assertions below are the NEGATIVE ones: that no digit appears where there is no measurement.
 * A test that only checked for the explanatory sentence would pass on a panel that showed the sentence AND a
 * zero beside it, which is the failure it was written to prevent.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AiCostPanel } from "@/features/monitoring/AiCostPanel";
import {
  ApplicationMetricsPanel,
  InfrastructureHealthPanel,
} from "@/features/monitoring/InfrastructurePanels";
import type { MetricResult, MetricVerdict } from "@/features/monitoring/MetricVerdict";
import { MonitoringDashboard } from "@/features/monitoring/MonitoringDashboard";
import {
  ResourceUtilisationPanel,
  type ProbeResourceReading,
} from "@/features/monitoring/ResourceUtilisationPanel";

const post = vi.fn();
const get = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: {
    post: (...args: unknown[]) => post(...args),
    get: (...args: unknown[]) => get(...args),
  },
}));

function mount(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

function result(overrides: Partial<MetricResult> = {}): MetricResult {
  return {
    name: "ai_cost_total",
    kind: "instant",
    verdict: "fresh",
    explanation: "Reported within the last minute.",
    has_numbers: true,
    newest_sample_age_seconds: 3,
    describes: "Total spend over the last 24 hours.",
    promql: 'sum (tenant_model:gen_ai_cost:increase24h{forgeops_tenant_id="t1"})',
    series: [{ labels: {}, points: [{ at: 1, value: 12.5 }] }],
    ...overrides,
  };
}

/** A result carrying no numbers, in each of the three ways that can happen. */
function absent(verdict: MetricVerdict, explanation: string): MetricResult {
  return result({ verdict, explanation, has_numbers: false, series: [] });
}

beforeEach(() => {
  post.mockReset();
  get.mockReset();
});

describe("the three absent states are three different sentences", () => {
  it.each([
    [
      "unconfigured",
      "No metrics store is configured for this deployment, so nothing has been measured.",
      "No metrics store configured",
    ],
    [
      "unreachable",
      "The metrics store did not answer, so the current value of this figure is unknown.",
      "Metrics store unreachable",
    ],
    [
      "never_reported",
      "The metrics store answered and holds no data for this query.",
      "Never reported",
    ],
  ] as Array<[MetricVerdict, string, string]>)(
    "%s renders its own heading and explanation",
    async (verdict, explanation, heading) => {
      post.mockResolvedValue(absent(verdict, explanation));
      mount(<AiCostPanel />);
      await waitFor(() => {
        expect(screen.getAllByText(new RegExp(heading)).length).toBeGreaterThan(0);
      });
      expect(screen.getAllByText(new RegExp(explanation.slice(0, 40))).length).toBeGreaterThan(0);
    },
  );

  it("shows no digit at all in the total when nothing was measured", async () => {
    post.mockResolvedValue(
      absent("unreachable", "The metrics store did not answer, so the current value is unknown."),
    );
    mount(<AiCostPanel />);
    const total = await screen.findByTestId("ai-cost-total");
    await waitFor(() => {
      expect(total.textContent).toContain("no figure to show");
    });
    // The assertion that matters. A `0.0000` beside the warning would defeat the whole design, and a test
    // looking only for the warning would not notice.
    const figure = total.querySelector(".monitoring-figure--absent");
    expect(figure).not.toBeNull();
    expect(figure?.textContent ?? "").not.toMatch(/\d/);
  });

  it("distinguishes never-reported from a measured zero", async () => {
    // A real zero: the store answered, there is a series, and its value is 0.
    post.mockResolvedValue(result({ series: [{ labels: {}, points: [{ at: 1, value: 0 }] }] }));
    mount(<AiCostPanel />);
    const total = await screen.findByTestId("ai-cost-total");
    await waitFor(() => {
      expect(total.textContent).toContain("0.0000");
    });
    // And it must NOT claim nothing was reported, because something was: zero.
    expect(total.textContent).not.toContain("no figure to show");
  });
});

describe("staleness is stated rather than implied", () => {
  it("names the age of the newest sample", async () => {
    post.mockResolvedValue(
      result({
        verdict: "stale",
        explanation:
          "The newest sample is 900 seconds old, past the 60-second freshness threshold. The figure is real but describes the past.",
        newest_sample_age_seconds: 900,
      }),
    );
    mount(<AiCostPanel />);
    await waitFor(() => {
      expect(screen.getAllByText(/Stale reading/).length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText(/900 seconds old/).length).toBeGreaterThan(0);
  });

  it("still shows the figure when stale, because the number is real", async () => {
    post.mockResolvedValue(
      result({
        verdict: "stale",
        explanation: "The newest sample is 900 seconds old.",
        has_numbers: true,
      }),
    );
    mount(<AiCostPanel />);
    const total = await screen.findByTestId("ai-cost-total");
    await waitFor(() => expect(total.textContent).toContain("12.5"));
  });

  it("renders a notice for the fresh state too, so its absence never carries meaning", async () => {
    post.mockResolvedValue(result());
    mount(<AiCostPanel />);
    await waitFor(() => {
      expect(screen.getAllByText(/Reported just now/).length).toBeGreaterThan(0);
    });
  });
});

describe("the cost panel", () => {
  it("shows the model that answered, per model", async () => {
    post.mockImplementation((_path: string, body: { name: string }) => {
      if (body.name === "ai_cost_by_model") {
        return Promise.resolve(
          result({
            name: "ai_cost_by_model",
            series: [
              {
                labels: { gen_ai_response_model: "qwen3-coder-next" },
                points: [{ at: 1, value: 1.5 }],
              },
              { labels: { gen_ai_response_model: "bge-m3" }, points: [{ at: 1, value: 4.6 }] },
            ],
          }),
        );
      }
      return Promise.resolve(result({ name: body.name }));
    });
    mount(<AiCostPanel />);
    await waitFor(() => {
      expect(screen.getByText("qwen3-coder-next")).toBeInTheDocument();
    });
    expect(screen.getByText("bge-m3")).toBeInTheDocument();
    // Sorted descending, so the expensive model is first.
    const rows = screen.getByTestId("ai-cost-by-model").querySelectorAll("tbody tr");
    expect(rows[0]?.textContent).toContain("bge-m3");
  });

  it("does not render an empty table, which would read as 'no model costs anything'", async () => {
    post.mockResolvedValue(
      absent("never_reported", "The metrics store holds no data for this query."),
    );
    mount(<AiCostPanel />);
    const empty = await screen.findByTestId("ai-cost-by-model-empty");
    expect(empty.textContent).toContain("not the same as every model being free");
    expect(screen.getByTestId("ai-cost-by-model").querySelector("table")).toBeNull();
  });

  it("warns that a cache hit reports no cost, so falling spend is ambiguous", async () => {
    post.mockResolvedValue(result());
    mount(<AiCostPanel />);
    await waitFor(() => {
      expect(screen.getByText(/served from a cache tier reports no cost/)).toBeInTheDocument();
    });
  });

  it("shows the rendered PromQL so the tenant scoping is visible rather than trusted", async () => {
    post.mockResolvedValue(result());
    mount(<AiCostPanel />);
    const scope = await screen.findByTestId("ai-cost-scope");
    expect(scope.textContent).toContain("forgeops_tenant_id");
  });

  it("never combines input and output tokens into one figure", async () => {
    post.mockImplementation((_path: string, body: { name: string }) => {
      if (body.name === "ai_tokens_by_direction") {
        return Promise.resolve(
          result({
            name: body.name,
            series: [
              { labels: { direction: "input" }, points: [{ at: 1, value: 1000 }] },
              { labels: { direction: "output" }, points: [{ at: 1, value: 250 }] },
            ],
          }),
        );
      }
      return Promise.resolve(result({ name: body.name }));
    });
    mount(<AiCostPanel />);
    const tokens = await screen.findByTestId("ai-cost-tokens");
    await waitFor(() => expect(tokens.textContent).toContain("1000"));
    expect(tokens.textContent).toContain("250");
    // 1250 must not appear: a combined total cannot be converted back into money.
    expect(tokens.textContent).not.toContain("1250");
  });
});

describe("infrastructure health", () => {
  it("says shedding means the figures elsewhere are incomplete", async () => {
    post.mockImplementation((_path: string, body: { name: string }) => {
      if (body.name === "collector_refused_spans") {
        return Promise.resolve(
          result({
            name: body.name,
            series: [{ labels: { job: "otel-gateway" }, points: [{ at: 1, value: 12 }] }],
          }),
        );
      }
      return Promise.resolve(result({ name: body.name }));
    });
    mount(<InfrastructureHealthPanel />);
    const warning = await screen.findByTestId("infra-shedding-warning");
    expect(warning.textContent).toContain("incomplete");
    expect(warning.textContent).toContain("biased towards the requests that got through");
  });

  it("states the good state positively rather than leaving a blank", async () => {
    post.mockImplementation((_path: string, body: { name: string }) =>
      Promise.resolve(
        result({
          name: body.name,
          series: [{ labels: { job: "otel-gateway" }, points: [{ at: 1, value: 0 }] }],
        }),
      ),
    );
    mount(<InfrastructureHealthPanel />);
    const ok = await screen.findByTestId("infra-shedding-ok");
    expect(ok.textContent).toContain("no data is being lost");
  });

  it("treats zero accepted spans as a measurement worth alarming on, not an absence", async () => {
    post.mockImplementation((_path: string, body: { name: string }) =>
      Promise.resolve(
        result({
          name: body.name,
          series: [{ labels: { job: "otel-agent" }, points: [{ at: 1, value: 0 }] }],
        }),
      ),
    );
    mount(<InfrastructureHealthPanel />);
    const zero = await screen.findByTestId("infra-accepted-zero");
    expect(zero.textContent).toContain("a measurement and not an absence");
  });
});

describe("application metrics", () => {
  it("leaves the error column blank with words where a route had no traffic", async () => {
    post.mockImplementation((_path: string, body: { name: string }) => {
      if (body.name === "http_latency_p95") {
        return Promise.resolve(
          result({
            name: body.name,
            series: [{ labels: { http_route: "/api/v1/quiet" }, points: [{ at: 1, value: 42 }] }],
          }),
        );
      }
      if (body.name === "http_error_ratio") {
        // Absent for that route -- Prometheus returns no series when the denominator is zero.
        return Promise.resolve(result({ name: body.name, series: [], has_numbers: false }));
      }
      return Promise.resolve(result({ name: body.name }));
    });
    mount(<ApplicationMetricsPanel />);
    const cell = await screen.findByTestId("app-error-/api/v1/quiet");
    expect(cell.textContent).toContain("no requests in this window");
    // Not 0%, which would say a route nobody called had no errors.
    expect(cell.textContent).not.toContain("0.00%");
  });

  it("does not render a trace link when no trace store is configured", async () => {
    post.mockImplementation((_path: string, body: { name: string }) => {
      if (body.name === "http_latency_p95") {
        return Promise.resolve(
          result({
            name: body.name,
            series: [
              { labels: { http_route: "/api/v1/projects" }, points: [{ at: 1, value: 42 }] },
            ],
          }),
        );
      }
      return Promise.resolve(result({ name: body.name }));
    });
    mount(<ApplicationMetricsPanel />);
    await waitFor(() => expect(screen.getByText("/api/v1/projects")).toBeInTheDocument());
    // A link that goes nowhere is worse than its absence: a human clicks it during an incident.
    expect(screen.queryByText("open traces")).toBeNull();
    expect(screen.getByText("no trace store configured")).toBeInTheDocument();
  });

  it("links to narrowed traces when tempo is configured", async () => {
    post.mockImplementation((_path: string, body: { name: string }) => {
      if (body.name === "http_latency_p95") {
        return Promise.resolve(
          result({
            name: body.name,
            series: [
              { labels: { http_route: "/api/v1/projects" }, points: [{ at: 1, value: 42 }] },
            ],
          }),
        );
      }
      return Promise.resolve(result({ name: body.name }));
    });
    mount(<ApplicationMetricsPanel tempoUrl="http://grafana.test" />);
    const link = await screen.findByText("open traces");
    // The route is in the query, so it opens narrowed rather than on every trace in the deployment.
    expect(link.getAttribute("href")).toContain(encodeURIComponent("/api/v1/projects"));
  });

  it("says the figures are deployment-wide rather than implying they are tenant-scoped", async () => {
    post.mockResolvedValue(result());
    mount(<ApplicationMetricsPanel />);
    await waitFor(() => {
      expect(screen.getByText(/whole deployment rather than one tenant/)).toBeInTheDocument();
    });
  });
});

describe("the unified dashboard", () => {
  it("states whether it can tell you anything before showing a figure", async () => {
    get.mockResolvedValue({
      metrics_store_configured: false,
      explanation:
        "No metrics store is configured. Monitoring panels have no source and will say so.",
    });
    post.mockResolvedValue(
      absent("unconfigured", "No metrics store is configured for this deployment."),
    );
    mount(<MonitoringDashboard />);
    const verdict = await screen.findByTestId("monitoring-readiness-verdict");
    expect(verdict.textContent).toContain("No metrics store is configured");
    // An alert, not a status, because every figure below is unavailable.
    expect(verdict.getAttribute("role")).toBe("alert");
  });

  it("distinguishes this application failing from the metrics store failing", async () => {
    get.mockRejectedValue(new Error("network"));
    post.mockResolvedValue(absent("unconfigured", "No metrics store is configured."));
    mount(<MonitoringDashboard />);
    const error = await screen.findByTestId("monitoring-readiness-error");
    expect(error.textContent).toContain("not known whether monitoring is configured");
  });

  it("says exemplars cannot be followed when grafana is absent", async () => {
    get.mockResolvedValue({
      metrics_store_configured: true,
      explanation: "A metrics store is configured.",
    });
    post.mockResolvedValue(result());
    mount(<MonitoringDashboard />);
    const notice = await screen.findByTestId("monitoring-no-grafana");
    expect(notice.textContent).toContain("exemplars cannot be followed");
    // And it must say the figures are unaffected, or an operator distrusts data that is fine.
    expect(notice.textContent).toContain("figures above are unaffected");
  });

  it("warns that the grafana dashboards are not tenant-scoped", async () => {
    get.mockResolvedValue({
      metrics_store_configured: true,
      explanation: "A metrics store is configured.",
    });
    post.mockResolvedValue(result());
    mount(<MonitoringDashboard grafanaUrl="http://grafana.test" />);
    await waitFor(() => {
      expect(screen.getByTestId("monitoring-grafana-cost")).toBeInTheDocument();
    });
    expect(screen.getByText(/convenience and not a\s+boundary/)).toBeInTheDocument();
  });

  it("sends no promql, ever", async () => {
    get.mockResolvedValue({ metrics_store_configured: true, explanation: "ok" });
    post.mockResolvedValue(result());
    mount(<MonitoringDashboard />);
    await waitFor(() => expect(post).toHaveBeenCalled());
    for (const call of post.mock.calls) {
      const body = call[1] as Record<string, unknown>;
      expect(Object.keys(body).sort()).toEqual(["arguments", "name", "window"]);
      expect(body).not.toHaveProperty("query");
      expect(body).not.toHaveProperty("promql");
    }
  });
});

describe("resource utilisation shows two sources without reconciling them", () => {
  function probe(overrides: Partial<ProbeResourceReading> = {}): ProbeResourceReading {
    return {
      container: "forgeops-backend-1",
      cpu_percent: 12.5,
      memory_bytes: 400 * 1024 * 1024,
      memory_limit_bytes: 2048 * 1024 * 1024,
      network_rx_bytes: 1024,
      network_tx_bytes: 2048,
      observed_at: new Date().toISOString(),
      ...overrides,
    };
  }

  it("states that the two sources are not reconciled and why", async () => {
    post.mockResolvedValue(result());
    mount(<ResourceUtilisationPanel probe={probe()} />);
    const note = await screen.findByTestId("resource-two-sources");
    expect(note.textContent).toContain("not");
    expect(note.textContent).toContain("Where the");
    expect(note.textContent).toContain("both can be correct");
  });

  it("keeps the two freshness verdicts separate, so a fresh probe cannot vouch for a dead collector", async () => {
    // The probe was sampled a moment ago; the metrics tier is unreachable. Both facts must appear.
    post.mockResolvedValue(
      absent(
        "unreachable",
        "The metrics store did not answer, so the current value of this figure is unknown.",
      ),
    );
    mount(<ResourceUtilisationPanel probe={probe()} />);
    const probeSide = await screen.findByTestId("resource-probe-freshness");
    const seriesSide = screen.getByTestId("resource-series-freshness");
    expect(probeSide.textContent).toContain("Sampled");
    // Awaited, because the probe renders synchronously while the query is still in flight -- which is
    // itself the point: the panel shows the probe's verdict without waiting on the metrics tier, so one
    // source being slow never blocks or vouches for the other.
    await waitFor(() => expect(seriesSide.textContent).toContain("Metrics store unreachable"));
    // And the metrics column must carry no digit, despite the probe column being full of them.
    const cell = screen.getByTestId("resource-series-memory");
    expect(cell.textContent).toContain("no figure to show");
    expect(cell.textContent).not.toMatch(/\d/);
  });

  it("reports an unparsable probe timestamp as never-reported rather than as now", async () => {
    post.mockResolvedValue(result());
    mount(<ResourceUtilisationPanel probe={probe({ observed_at: "not a date" })} />);
    const probeSide = await screen.findByTestId("resource-probe-freshness");
    expect(probeSide.textContent).toContain("has not reported a sample");
    expect(probeSide.textContent).toContain("not zero usage");
    // And the probe columns must not show the values that came with the bad timestamp.
    expect(screen.getByTestId("resource-probe-cpu").textContent).toBe("not measured");
  });

  it("says network is not collected by the metrics tier rather than leaving the cell blank", async () => {
    post.mockResolvedValue(result());
    mount(<ResourceUtilisationPanel probe={probe()} />);
    const cell = await screen.findByTestId("resource-series-network");
    expect(cell.textContent).toContain("not collected by the metrics tier");
    // A blank cell would read as zero traffic.
    expect(cell.textContent).not.toMatch(/^\s*$/);
  });

  it("does not average or prefer either source for the same quantity", async () => {
    post.mockResolvedValue(
      result({ series: [{ labels: {}, points: [{ at: 1, value: 50 * 1024 * 1024 }] }] }),
    );
    mount(<ResourceUtilisationPanel probe={probe()} />);
    // Probe says 400 MiB (the container); the series says 50 MiB (the process). BOTH appear, unchanged.
    const probeCell = await screen.findByTestId("resource-probe-memory");
    await waitFor(() =>
      expect(screen.getByTestId("resource-series-memory").textContent).toContain("50.0"),
    );
    expect(probeCell.textContent).toContain("400 MiB");
    // 225 MiB -- the average -- must appear nowhere.
    expect(document.body.textContent).not.toContain("225");
  });

  it("says the metrics column covers the process only, not the container", async () => {
    post.mockResolvedValue(result());
    mount(<ResourceUtilisationPanel probe={probe()} />);
    await waitFor(() =>
      expect(screen.getByText(/Covers the application process only/)).toBeInTheDocument(),
    );
  });
});
