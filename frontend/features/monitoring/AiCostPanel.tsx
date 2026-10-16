"use client";

/**
 * AI cost, per tenant per model. Phase 2 §2.10.
 *
 * THE TENANT IS NOT A CONTROL ON THIS PANEL, and that is the security property rather than an omission. The
 * backend composes the tenant matcher from the verified principal; there is no field here through which a
 * different tenant could be requested, and no PromQL field either. What the panel shows is which tenant the
 * figures belong to, read back from the API, so an operator can see the scoping rather than assume it.
 *
 * WHY A CACHE HIT IS SHOWN BESIDE THE SPEND. A model served from L1 or L2 reports zero cost, so falling
 * spend means either less work or more cache hits -- and those call for opposite responses. Showing cost
 * alone would let a drop caused by a cache read as a saving when it might be a collapse in usage.
 *
 * WHY INPUT AND OUTPUT TOKENS ARE NEVER ADDED TOGETHER. They price differently on every provider, so a
 * combined total cannot be converted back into money; a single "tokens" figure invites exactly that.
 */

import { useMemo } from "react";

import {
  MetricFigure,
  MetricVerdictNotice,
  latestByLabel,
  latestTotal,
  useMetricQuery,
} from "./MetricVerdict";

export function AiCostPanel() {
  const total = useMetricQuery("ai_cost_total");
  const byModel = useMetricQuery("ai_cost_by_model");
  const tokens = useMetricQuery("ai_tokens_by_direction");
  const origins = useMetricQuery("ai_requests_by_outcome");

  const models = useMemo(
    () => latestByLabel(byModel.data, "gen_ai_response_model"),
    [byModel.data],
  );
  const tokenRows = useMemo(() => latestByLabel(tokens.data, "direction"), [tokens.data]);
  const originRows = useMemo(
    () => latestByLabel(origins.data, "forgeops_served_from"),
    [origins.data],
  );

  return (
    <section aria-labelledby="ai-cost-heading" data-testid="ai-cost-panel">
      <h2 id="ai-cost-heading">AI cost, last 24 hours</h2>

      <p className="monitoring-caveat">
        These are costs <strong>as reported by the generation pipeline</strong>, not a provider
        invoice. A generation served from a cache tier reports no cost at all, so read the origins
        table below before concluding that spend has fallen.
      </p>

      <div data-testid="ai-cost-total">
        <h3>Total</h3>
        <MetricVerdictNotice result={total.data} isLoading={total.isLoading} error={total.error} />
        <MetricFigure result={total.data} value={latestTotal(total.data)} decimals={4} />
      </div>

      <div data-testid="ai-cost-by-model">
        <h3>By model</h3>
        <MetricVerdictNotice
          result={byModel.data}
          isLoading={byModel.isLoading}
          error={byModel.error}
        />
        {byModel.data?.has_numbers && models.length > 0 ? (
          <table>
            <caption>
              Spend per model over the last 24 hours. The model shown is the one that ANSWERED;
              where a routing cascade fell back, this is the standby rather than the model asked
              for.
            </caption>
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col">Spend</th>
              </tr>
            </thead>
            <tbody>
              {models.map((row) => (
                <tr key={row.key}>
                  <td>{row.key}</td>
                  <td>
                    <MetricFigure result={byModel.data} value={row.value} decimals={4} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          // Not an empty table. An empty table with headers reads as "no models cost anything", which is a
          // measurement; this is the absence of one.
          <p data-testid="ai-cost-by-model-empty">
            No model has reported a cost. That is not the same as every model being free: if the
            collector cannot be reached, spend is still being incurred and simply is not recorded
            here.
          </p>
        )}
      </div>

      <div data-testid="ai-cost-tokens">
        <h3>Tokens</h3>
        <MetricVerdictNotice
          result={tokens.data}
          isLoading={tokens.isLoading}
          error={tokens.error}
        />
        {tokens.data?.has_numbers && tokenRows.length > 0 ? (
          <dl>
            {tokenRows.map((row) => (
              <div key={row.key}>
                <dt>{row.key}</dt>
                <dd>
                  <MetricFigure result={tokens.data} value={row.value} decimals={0} />
                </dd>
              </div>
            ))}
            <p className="monitoring-caveat">
              Shown separately because input and output tokens price differently on every provider.
              A single combined figure could not be converted back into money.
            </p>
          </dl>
        ) : (
          <p data-testid="ai-cost-tokens-empty">No token counts have been reported.</p>
        )}
      </div>

      <div data-testid="ai-cost-origins">
        <h3>Where answers came from</h3>
        <MetricVerdictNotice
          result={origins.data}
          isLoading={origins.isLoading}
          error={origins.error}
        />
        {origins.data?.has_numbers && originRows.length > 0 ? (
          <table>
            <caption>
              Generations by origin. A rise here in cache hits lowers spend without lowering work,
              which is what makes the cost figures above interpretable.
            </caption>
            <thead>
              <tr>
                <th scope="col">Served from</th>
                <th scope="col">Generations</th>
              </tr>
            </thead>
            <tbody>
              {originRows.map((row) => (
                <tr key={row.key}>
                  <td>{row.key}</td>
                  <td>
                    <MetricFigure result={origins.data} value={row.value} decimals={0} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p data-testid="ai-cost-origins-empty">
            No generation has reported where it was served from, so cache hits cannot be separated
            from provider calls.
          </p>
        )}
      </div>

      {total.data ? (
        <p className="monitoring-scope" data-testid="ai-cost-scope">
          {/* The rendered PromQL, so the tenant scoping is visible rather than trusted. */}
          Scoped by this application, not by anything selectable here:{" "}
          <code>{total.data.promql}</code>
        </p>
      ) : null}
    </section>
  );
}
