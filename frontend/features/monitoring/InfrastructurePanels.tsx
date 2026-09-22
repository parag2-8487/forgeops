"use client";

/**
 * Infrastructure health, and application metrics with trace correlation. Phase 2 §2.10.
 *
 * TWO PANELS IN ONE FILE because they answer one question in sequence, and separating them invites reading
 * the second without the first. The application panel's figures are only as trustworthy as the telemetry
 * pipeline that carried them: a collector shedding spans produces a latency percentile computed from the
 * requests that survived, which is biased towards the fast ones. So `InfrastructureHealthPanel` renders the
 * pipeline's own state, and `ApplicationMetricsPanel` says out loud that it depends on it.
 *
 * SHED AND ACCEPTED ARE SHOWN TOGETHER, deliberately. An empty "shed" panel is the good state; an empty
 * "shed" panel because the collector is down looks identical. Only the accepted count distinguishes them,
 * which is why neither is shown alone.
 *
 * TRACE CORRELATION IS A LINK OUT, NOT A TRACE VIEWER. Building a waterfall here would be a worse Grafana,
 * and worse in a specific way: Tempo's own UI knows about span links and node graphs, and a partial
 * reimplementation would quietly omit them. The link carries the route and the time window, so it opens
 * narrowed rather than on everything.
 */

import { useMemo } from "react";

import {
  MetricFigure,
  MetricVerdictNotice,
  latestByLabel,
  latestTotal,
  useMetricQuery,
} from "./MetricVerdict";

const WINDOW = "15m";

export function InfrastructureHealthPanel() {
  const accepted = useMetricQuery("collector_accepted_spans", { arguments: { window: WINDOW } });
  const refused = useMetricQuery("collector_refused_spans", { arguments: { window: WINDOW } });
  const queue = useMetricQuery("collector_queue_size");

  const refusedTotal = latestTotal(refused.data);
  const acceptedTotal = latestTotal(accepted.data);

  return (
    <section aria-labelledby="infra-health-heading" data-testid="infrastructure-health-panel">
      <h2 id="infra-health-heading">Telemetry pipeline health</h2>

      <p className="monitoring-caveat">
        Read this before the application figures below. If the pipeline is shedding data, those
        figures are computed from what survived.
      </p>

      <div data-testid="infra-shedding">
        <h3>Spans being shed</h3>
        <MetricVerdictNotice
          result={refused.data}
          isLoading={refused.isLoading}
          error={refused.error}
        />
        <MetricFigure result={refused.data} value={refusedTotal} unit="per second" />
        {/* The interpretation, in words, because the number alone is ambiguous: shedding is the collector
            working as designed AND a reason to distrust everything downstream. */}
        {refused.data?.has_numbers && refusedTotal !== null ? (
          refusedTotal > 0 ? (
            <p role="alert" data-testid="infra-shedding-warning">
              The collector is refusing data. That is it working as designed -- a collector that
              refuses loses only what it refused, one killed by the OOM killer loses everything it
              held -- but it means the metrics and traces on this page are{" "}
              <strong>incomplete</strong>, and latency percentiles are biased towards the requests
              that got through.
            </p>
          ) : (
            <p data-testid="infra-shedding-ok">
              Nothing is being shed, so no data is being lost here.
            </p>
          )
        ) : null}
      </div>

      <div data-testid="infra-accepted">
        <h3>Spans being accepted</h3>
        <MetricVerdictNotice
          result={accepted.data}
          isLoading={accepted.isLoading}
          error={accepted.error}
        />
        <MetricFigure result={accepted.data} value={acceptedTotal} unit="per second" />
        {accepted.data?.has_numbers && acceptedTotal === 0 ? (
          <p role="alert" data-testid="infra-accepted-zero">
            Nothing is arriving at the collector. Either the application is not instrumented, or it
            cannot reach the collector. A zero here is a measurement and not an absence -- the
            collector is answering, it is simply receiving nothing.
          </p>
        ) : null}
      </div>

      <div data-testid="infra-queue">
        <h3>Waiting to be exported</h3>
        <MetricVerdictNotice result={queue.data} isLoading={queue.isLoading} error={queue.error} />
        <MetricFigure
          result={queue.data}
          value={latestTotal(queue.data)}
          decimals={0}
          unit="items"
        />
        <p className="monitoring-caveat">
          A queue that only grows means a store downstream is unreachable. The queue is bounded, so
          past its limit telemetry is dropped rather than held indefinitely.
        </p>
      </div>
    </section>
  );
}

export function ApplicationMetricsPanel({ tempoUrl }: { tempoUrl?: string }) {
  const rate = useMetricQuery("http_request_rate", { arguments: { window: WINDOW } });
  const latency = useMetricQuery("http_latency_p95");
  const errors = useMetricQuery("http_error_ratio");

  const routes = useMemo(() => latestByLabel(latency.data, "http_route"), [latency.data]);
  const errorRows = useMemo(() => latestByLabel(errors.data, "http_route"), [errors.data]);
  const errorByRoute = useMemo(
    () => new Map(errorRows.map((row) => [row.key, row.value])),
    [errorRows],
  );

  return (
    <section aria-labelledby="app-metrics-heading" data-testid="application-metrics-panel">
      <h2 id="app-metrics-heading">Application: request rate, latency and errors</h2>

      <p className="monitoring-caveat">
        These cover the whole deployment rather than one tenant, because the instrumentation that
        records them starts its timer before a principal has been resolved -- there is no tenant to
        attribute a request to at that point.
      </p>

      <div data-testid="app-request-rate">
        <h3>Requests per second</h3>
        <MetricVerdictNotice result={rate.data} isLoading={rate.isLoading} error={rate.error} />
        <MetricFigure result={rate.data} value={latestTotal(rate.data)} unit="per second" />
      </div>

      <div data-testid="app-latency">
        <h3>95th percentile latency, by route</h3>
        <MetricVerdictNotice
          result={latency.data}
          isLoading={latency.isLoading}
          error={latency.error}
        />
        {latency.data?.has_numbers && routes.length > 0 ? (
          <table>
            <caption>
              Slowest routes first. The error column is blank where a route had no traffic: the
              ratio&apos;s denominator is the request count, so there is nothing to divide by --
              which is different from no errors having occurred.
            </caption>
            <thead>
              <tr>
                <th scope="col">Route</th>
                <th scope="col">p95</th>
                <th scope="col">5xx</th>
                <th scope="col">Traces</th>
              </tr>
            </thead>
            <tbody>
              {routes.map((row) => {
                const ratio = errorByRoute.get(row.key);
                return (
                  <tr key={row.key}>
                    <td>{row.key}</td>
                    <td>
                      <MetricFigure
                        result={latency.data}
                        value={row.value}
                        decimals={0}
                        unit="ms"
                      />
                    </td>
                    <td data-testid={`app-error-${row.key}`}>
                      {ratio === undefined ? (
                        <span className="monitoring-figure monitoring-figure--absent">
                          no requests in this window
                        </span>
                      ) : (
                        `${(ratio * 100).toFixed(2)}%`
                      )}
                    </td>
                    <td>
                      {tempoUrl ? (
                        <a
                          href={`${tempoUrl}/explore?left=${encodeURIComponent(
                            JSON.stringify({
                              datasource: "tempo",
                              queries: [{ query: `{ .http.route = "${row.key}" }` }],
                              range: { from: "now-15m", to: "now" },
                            }),
                          )}`}
                          rel="noreferrer noopener"
                          target="_blank"
                        >
                          open traces
                        </a>
                      ) : (
                        // Not a dead link and not a hidden column. A link that goes nowhere is worse than
                        // its absence, because a human clicks it during an incident.
                        <span className="monitoring-figure monitoring-figure--absent">
                          no trace store configured
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <p data-testid="app-latency-empty">
            No route has reported a latency. With no requests measured, nothing here can be said
            about how fast the application is -- including that it is fast.
          </p>
        )}
      </div>
    </section>
  );
}
