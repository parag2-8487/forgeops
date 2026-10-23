"use client";

/**
 * Resource utilisation from TWO independent sources. Phase 2 §2.4 (combined box) and §2.10.
 *
 * THE BOX THIS SATISFIES NAMES ITS OWN HAZARD: "two panels showing the same quantity from two sources is how
 * a stale number gets read as a live one". So the design decision here is what this component REFUSES to do,
 * which is reconcile them.
 *
 * The two sources are not two measurements of one thing:
 *
 *   The Docker probe is a POINT SAMPLE of a CONTAINER, taken when an operator asked. It covers every process
 *   in that container. It is exact at the instant it was taken and says nothing about any other instant.
 *
 *   The metrics tier is a TIME SERIES of ONE PROCESS, scraped every fifteen seconds. It covers the Python
 *   application and not its neighbours in the container. It describes a window, not a moment.
 *
 * A single "CPU: 12%" row fed by whichever source answered would be wrong in a way nobody could see. So each
 * source keeps its own column, its own freshness verdict computed by its own rule, and a sentence naming what
 * it measured. Where they disagree the component says they measure different things -- it does not average
 * them, prefer one, or hide the difference.
 *
 * AND THE TWO FRESHNESS RULES ARE DELIBERATELY NOT UNIFIED. The probe's freshness comes from `freshnessOf`
 * over an `observed_at` the agent stamped; the metrics tier's comes from the backend's verdict over the
 * newest sample in the store. Collapsing them into one badge would require picking one, and then a fresh
 * probe beside a dead collector would render as "fresh".
 */

import { freshnessOf, type Freshness } from "@/features/hostops/DockerDashboard";

import { MetricFigure, MetricVerdictNotice, latestTotal, useMetricQuery } from "./MetricVerdict";

function bytes(value: number): string {
  const units = ["B", "KiB", "MiB", "GiB"];
  let scaled = value;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return `${scaled.toFixed(scaled < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
}

/** The probe's side, in words. Separate from the metrics tier's verdict on purpose -- see the file header. */
function probeFreshnessSentence(freshness: Freshness): string {
  if (freshness.kind === "never-reported") {
    return "The agent has not reported a sample, so there is no container reading at all. This is not zero usage.";
  }
  if (freshness.kind === "stale") {
    return `This container reading is ${freshness.ageSeconds} seconds old. It was exact when taken and describes that moment only.`;
  }
  return `Sampled ${freshness.ageSeconds} seconds ago.`;
}

export interface ProbeResourceReading {
  /** Null where the probe ran but could not measure -- never coerced to 0. */
  cpu_percent: number | null;
  memory_bytes: number | null;
  memory_limit_bytes: number | null;
  network_rx_bytes: number | null;
  network_tx_bytes: number | null;
  /** When the agent took the sample. Null or unparsable resolves to never-reported. */
  observed_at: string | null;
  container: string;
}

export function ResourceUtilisationPanel({ probe }: { probe?: ProbeResourceReading }) {
  const cpu = useMetricQuery("process_cpu_utilisation");
  const memory = useMetricQuery("process_memory_rss");

  const probeFreshness = freshnessOf(probe?.observed_at);
  const seriesCpu = latestTotal(cpu.data);
  const seriesMemory = latestTotal(memory.data);

  return (
    <section aria-labelledby="resource-heading" data-testid="resource-utilisation-panel">
      <h2 id="resource-heading">Resource utilisation</h2>

      <p className="monitoring-caveat" data-testid="resource-two-sources">
        Two independent sources are shown side by side and are <strong>not</strong> reconciled,
        because they do not measure the same thing. The container probe is a point sample of the
        whole container, taken when it was asked. The metrics tier is a scraped series for the
        application process alone. Where the two disagree, both can be correct.
      </p>

      <table>
        <caption>
          Each column carries its own freshness, computed by its own rule. A fresh reading in one
          column says nothing about the other.
        </caption>
        <thead>
          <tr>
            <th scope="col">Quantity</th>
            <th scope="col">Container probe (point sample)</th>
            <th scope="col">Metrics tier (scraped series)</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">CPU</th>
            <td data-testid="resource-probe-cpu">
              {probeFreshness.kind === "never-reported" ||
              probe?.cpu_percent === null ||
              probe === undefined
                ? "not measured"
                : `${probe.cpu_percent.toFixed(2)}% of the container`}
            </td>
            <td data-testid="resource-series-cpu">
              <MetricFigure result={cpu.data} value={seriesCpu} unit="of one core" decimals={3} />
            </td>
          </tr>
          <tr>
            <th scope="row">Memory</th>
            <td data-testid="resource-probe-memory">
              {probeFreshness.kind === "never-reported" ||
              probe?.memory_bytes === null ||
              probe === undefined
                ? "not measured"
                : probe.memory_limit_bytes === null
                  ? `${bytes(probe.memory_bytes)}, limit not reported`
                  : `${bytes(probe.memory_bytes)} of ${bytes(probe.memory_limit_bytes)}`}
            </td>
            <td data-testid="resource-series-memory">
              <MetricFigure
                result={memory.data}
                value={seriesMemory === null ? null : seriesMemory / (1024 * 1024)}
                unit="MiB resident"
                decimals={1}
              />
            </td>
          </tr>
          <tr>
            <th scope="row">Network</th>
            <td data-testid="resource-probe-network">
              {probeFreshness.kind === "never-reported" ||
              probe === undefined ||
              probe.network_rx_bytes === null ||
              probe.network_tx_bytes === null
                ? "not measured"
                : `${bytes(probe.network_rx_bytes)} in, ${bytes(probe.network_tx_bytes)} out`}
            </td>
            <td data-testid="resource-series-network">
              {/* NOT COLLECTED, and said so rather than left blank. Per-interface network counters multiply
                  series by the number of interfaces on the host, which is cardinality bought for a chart the
                  probe already provides. A blank cell would read as zero traffic. */}
              <span className="monitoring-figure monitoring-figure--absent">
                not collected by the metrics tier
              </span>
            </td>
          </tr>
        </tbody>
      </table>

      <div data-testid="resource-probe-freshness">
        <h3>Container probe</h3>
        <p role={probeFreshness.kind === "never-reported" ? "alert" : "status"}>
          {probeFreshnessSentence(probeFreshness)}
        </p>
      </div>

      <div data-testid="resource-series-freshness">
        <h3>Metrics tier</h3>
        <MetricVerdictNotice
          result={memory.data}
          isLoading={memory.isLoading}
          error={memory.error}
        />
        <p className="monitoring-caveat">
          Covers the application process only. Anything else running in the same container -- a
          sidecar, a shell, a worker -- is counted by the probe column and not by this one.
        </p>
      </div>
    </section>
  );
}
