"use client";

/**
 * The unified monitoring dashboard. Phase 2 §2.10.
 *
 * "UNIFIED" MEANS ONE PAGE THAT STATES ITS OWN TRUSTWORTHINESS FIRST, then shows figures. The order is the
 * design: readiness, then pipeline health, then application, then cost. An operator who reads top to bottom
 * cannot arrive at a cost figure without having passed the reason it might be wrong.
 *
 * EXEMPLAR SUPPORT IS A LINK INTO GRAFANA RATHER THAN A CHART HERE. An exemplar is a trace id attached to a
 * histogram bucket, and clicking one must open a trace -- which means a trace viewer. Grafana already has
 * one that understands span links and service graphs; a partial reimplementation would omit them silently,
 * and the omission would be discovered during an incident. So this page links out, and it says whether the
 * link will work instead of rendering a button that might not.
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";

import { AiCostPanel } from "./AiCostPanel";
import { ApplicationMetricsPanel, InfrastructureHealthPanel } from "./InfrastructurePanels";

interface MonitoringReadiness {
  metrics_store_configured: boolean;
  explanation: string;
}

export function MonitoringDashboard({
  grafanaUrl,
  tempoUrl,
}: {
  grafanaUrl?: string;
  tempoUrl?: string;
}) {
  const readiness = useQuery<MonitoringReadiness>({
    queryKey: ["monitoring", "readiness"],
    queryFn: async () => api.get<MonitoringReadiness>("/monitoring/readiness"),
    refetchInterval: 60_000,
  });

  return (
    <main aria-labelledby="monitoring-heading" data-testid="monitoring-dashboard">
      <h1 id="monitoring-heading">Monitoring</h1>

      <section aria-labelledby="monitoring-readiness-heading" data-testid="monitoring-readiness">
        <h2 id="monitoring-readiness-heading">Can this page tell you anything?</h2>
        {readiness.isLoading ? (
          <p role="status">Asking whether a metrics store is configured.</p>
        ) : readiness.error ? (
          <p role="alert" data-testid="monitoring-readiness-error">
            This application did not answer, so it is not known whether monitoring is configured.
            Nothing below can be relied on.
          </p>
        ) : (
          <p
            role={readiness.data?.metrics_store_configured ? "status" : "alert"}
            data-testid="monitoring-readiness-verdict"
          >
            {readiness.data?.explanation}
          </p>
        )}
      </section>

      {/* Rendered even when unconfigured. Hiding the panels would leave a page that looked broken rather
          than one that explains itself, and each panel states its own verdict anyway. */}
      <InfrastructureHealthPanel />
      <ApplicationMetricsPanel tempoUrl={tempoUrl} />
      <AiCostPanel />

      <section aria-labelledby="monitoring-deep-heading">
        <h2 id="monitoring-deep-heading">Exemplars and traces</h2>
        {grafanaUrl ? (
          <>
            <p>
              Latency histograms carry exemplars: a trace id attached to the bucket a slow request
              landed in. Clicking one in Grafana opens a request that was <em>actually</em> that
              slow, rather than searching the same time window and hoping.
            </p>
            <ul>
              <li>
                <a
                  href={`${grafanaUrl}/d/forgeops-infrastructure`}
                  rel="noreferrer noopener"
                  target="_blank"
                  data-testid="monitoring-grafana-infra"
                >
                  Infrastructure and application health, with exemplars on the latency panel
                </a>
              </li>
              <li>
                <a
                  href={`${grafanaUrl}/d/forgeops-ai-cost`}
                  rel="noreferrer noopener"
                  target="_blank"
                  data-testid="monitoring-grafana-cost"
                >
                  AI cost, across all tenants (operator view)
                </a>
              </li>
            </ul>
            <p className="monitoring-caveat">
              Those dashboards are an <strong>operator</strong> surface and are not scoped to one
              tenant -- Grafana queries with its own credentials, so a tenant selector there is a
              convenience and not a boundary. The figures on this page are scoped by this
              application to the tenant you are signed in as.
            </p>
          </>
        ) : (
          <p data-testid="monitoring-no-grafana">
            No Grafana is configured for this deployment, so exemplars cannot be followed to a
            trace. The figures above are unaffected: they come from this application&apos;s own read
            route.
          </p>
        )}
      </section>
    </main>
  );
}
