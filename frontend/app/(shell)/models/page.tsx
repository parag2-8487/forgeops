// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";

/** Mirrors `TierInfoResponse` in `backend/src/ai/routes.py`. */
interface TierInfo {
  name: string;
  primary_endpoint: string;
  primary_protocol: string;
  available: boolean;
  breaker_state: string;
}

interface TiersResponse {
  tiers: TierInfo[];
}

/**
 * What each circuit-breaker state means and what it implies for the next request.
 *
 * The lifecycle is the whole reason this screen exists: `closed → open → half_open → closed` was
 * proven end to end across two genuinely separate live endpoints — four attempts falling through to a
 * secondary, the fifth failure inside thirty seconds opening the breaker, subsequent attempts recorded
 * as `skipped(circuit_breaker_open)` with latency dropping from 4.6s to 0.7s because no connection is
 * attempted, `half_open` after the sixty-second cooldown, and a successful probe closing it. None of
 * that was observable to a user, so a generation that silently fell back to a secondary endpoint or a
 * safe template looked identical to one that did not.
 */
const BREAKER_MEANING: Record<string, string> = {
  closed:
    "Normal. Requests go to this endpoint, and failures are counted — five inside thirty seconds opens it.",
  open: "Tripped. No connection is attempted at all, so requests fail fast and fall straight through to the next endpoint in the cascade. It moves to half-open after a sixty-second cooldown.",
  half_open:
    "Probing. One request is allowed through to test whether the endpoint has recovered. Success closes the breaker and returns traffic here; failure opens it again for another cooldown.",
};

/**
 * Model tier health — `GET /api/v1/ai/tiers`, which nothing called.
 *
 * WHY A USER NEEDS THIS. When a generation degrades — a slower model, a safe template instead of a
 * generated artifact, or an outright `generation-unavailable` — the cause is upstream of anything the
 * generation screen can see. Without this panel the only symptom is "the output is worse than last
 * time", which is unactionable and reads as the product being unreliable rather than as one endpoint
 * being down and the cascade doing its job.
 *
 * WHAT IT DOES NOT CLAIM. `available` is the registry's last observation, not a probe issued now:
 * reading this page does not test the endpoints, and it says so. A page that probed every configured
 * model on load would turn an operator glancing at a dashboard into load on every vendor.
 */
export default function ModelTiersPage() {
  const tiers = useQuery({
    queryKey: queryKeys.ai.tiers(),
    queryFn: () => api.get<TiersResponse>("/ai/tiers"),
    // Breaker state changes on a sixty-second cooldown, so a panel refreshed less often than that
    // would routinely show a breaker as open after it had already moved to half-open.
    refetchInterval: 20_000,
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Model tiers</h1>
        <p className="mt-1 text-muted-foreground">
          Read from <code>GET /api/v1/ai/tiers</code>. Which model tier each request would reach,
          and whether its circuit breaker is letting anything through.
        </p>
      </div>

      <AsyncState
        isPending={tiers.isPending}
        error={tiers.error}
        isEmpty={tiers.data?.tiers.length === 0}
        emptyMessage="No model tiers are configured. `config/model-tiers.yaml` is what defines them, and an empty registry means generation has nothing to route to."
        label="model tiers"
      >
        <ul className="space-y-3" data-testid="tier-list">
          {tiers.data?.tiers.map((tier) => (
            <li
              key={tier.name}
              className="rounded-lg border border-border bg-background p-4 text-sm"
              data-testid={`tier-${tier.name}`}
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="font-semibold">{tier.name}</h2>
                <span
                  data-testid={`availability-${tier.name}`}
                  className={tier.available ? "text-xs text-emerald-600" : "text-xs text-amber-600"}
                >
                  {/* The word carries the state; the colour only reinforces it. */}
                  {tier.available ? "available" : "unavailable"}
                </span>
              </div>

              <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
                <dt className="font-medium">Primary endpoint</dt>
                <dd>
                  <code>{tier.primary_endpoint}</code>
                  <span className="ml-2 text-muted-foreground">{tier.primary_protocol}</span>
                </dd>
                <dt className="font-medium">Circuit breaker</dt>
                <dd>
                  <span data-testid={`breaker-${tier.name}`} className="font-mono">
                    {tier.breaker_state}
                  </span>
                  <span className="ml-2 text-muted-foreground">
                    {BREAKER_MEANING[tier.breaker_state] ??
                      "This breaker state has no description here, so only the reported value is shown."}
                  </span>
                </dd>
              </dl>
            </li>
          ))}
        </ul>
      </AsyncState>

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          What these figures are, and what they are not.
        </p>
        <p className="mt-2">
          Only the <strong>primary</strong> endpoint of each tier is reported, because that is what
          the endpoint returns. The cascade behind it — primary, then a cross-vendor backup, then
          self-hosted, then the safe template library — is not enumerated here, so an unavailable
          primary does not mean the tier cannot serve a request. It means the next request to that
          tier will fall through, and a generation that took the fallback is not a failed
          generation.
        </p>
        <p className="mt-2">
          <strong>Availability is the registry&apos;s last observation, not a probe.</strong>{" "}
          Loading this page does not test any endpoint. A page that did would put load on every
          configured vendor every time somebody glanced at a dashboard, and would report a transient
          failure as a state change.
        </p>
        <p className="mt-2">
          <strong>The stated limit.</strong> Only self-hosted endpoints have ever served a live call
          in this deployment: the hosted vendor keys are placeholders, so the five hosted tiers are
          configured and unconfigured at once — a tier can appear here with an endpoint it has never
          successfully reached. The cascade and the breaker lifecycle are proven across real
          endpoints, not across vendors.
        </p>
      </aside>
    </div>
  );
}
