/**
 * The monitoring page. Phase 2 §2.10.
 *
 * The Grafana and Tempo URLs come from `NEXT_PUBLIC_*` and are UNSET BY DEFAULT, which the dashboard renders
 * as "no Grafana is configured" rather than as a broken link. That is the whole reason they are passed as
 * props rather than read inside the component: a component reading `process.env` directly cannot be tested
 * for the unconfigured case without mutating the environment, and the unconfigured case is the default.
 *
 * They are `NEXT_PUBLIC_` because they are links a BROWSER follows -- Grafana runs beside this application,
 * not behind it. Nothing secret passes through them; Grafana has its own login, which is the point made in
 * the dashboard's own caveat about it being an operator surface.
 */

import { MonitoringDashboard } from "@/features/monitoring/MonitoringDashboard";

export default function MonitoringPage() {
  return (
    <MonitoringDashboard
      grafanaUrl={process.env.NEXT_PUBLIC_GRAFANA_URL || undefined}
      tempoUrl={process.env.NEXT_PUBLIC_GRAFANA_URL || undefined}
    />
  );
}
