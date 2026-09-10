// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useQuery } from "@tanstack/react-query";

import { AsyncState } from "@/components/ui/async-state";
import { CustomEndpointForm } from "@/features/models/CustomEndpointForm";
import {
  ProviderCredentialForm,
  useProviderCredentials,
} from "@/features/models/ProviderCredentialForm";
import { api, queryKeys } from "@/lib/api";

/** Mirrors `TierInfoResponse` in `backend/src/ai/routes.py`. */
interface TierInfo {
  name: string;
  primary_endpoint: string;
  primary_protocol: string;
  available: boolean;
  /** Does ForgeOps have an adapter for this protocol at all. Static; no configuration changes it. */
  protocol_supported: boolean;
  /** Is a credential resolvable for this endpoint right now. */
  credential_configured: boolean;
  /** Does this endpoint need one. `false` for a local server, where having none is correct. */
  credential_required: boolean;
  /** Which of the above is missing, named. `null` when nothing is. */
  reason: string | null;
  /** Which credential this tier needs. Shared across tiers: several can name the same one. */
  key_ref: string | null;
  /** What the last REAL call said. `null` means never tested — not "fine". */
  last_test_ok: boolean | null;
  last_tested_at: string | null;
  last_test_detail: string;
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
 *
 * `closed` USED TO READ AS AN ASSURANCE. It said requests go to this endpoint and failures are counted,
 * which is a description of a working endpoint — and it was shown beside hosted tiers that had never
 * completed a single call, because a breaker that has never been asked to do anything is closed. It now
 * says what closed actually means: nothing has tripped it, which on an endpoint that has never been
 * called is not evidence of anything.
 */
const BREAKER_MEANING: Record<string, string> = {
  closed:
    "Nothing has tripped it. Requests are allowed through and failures are counted — five inside thirty seconds opens it. On an endpoint that has never been called, closed is the starting state rather than a sign of health.",
  open: "Tripped. No connection is attempted at all, so requests fail fast and fall straight through to the next endpoint in the cascade. It moves to half-open after a sixty-second cooldown.",
  half_open:
    "Probing. One request is allowed through to test whether the endpoint has recovered. Success closes the breaker and returns traffic here; failure opens it again for another cooldown.",
};

/** The one line that says what is actually known, in the order a user can act on. */
function statusLabel(tier: TierInfo): { text: string; className: string } {
  if (tier.last_test_ok === true) {
    return { text: "answered a real call", className: "text-xs text-emerald-600" };
  }
  if (tier.last_test_ok === false) {
    return { text: "failed its last real call", className: "text-xs text-destructive" };
  }
  if (!tier.protocol_supported) {
    return { text: "not supported by this build", className: "text-xs text-muted-foreground" };
  }
  if (!tier.credential_configured) {
    return { text: "no credential configured", className: "text-xs text-amber-600" };
  }
  return { text: "configured, never tested", className: "text-xs text-amber-600" };
}

/**
 * Model tiers — `GET /api/v1/ai/tiers`, plus the setup this screen used to only describe.
 *
 * WHY A USER NEEDS THIS. When a generation degrades — a slower model, a safe template instead of a
 * generated artifact, or an outright `generation-unavailable` — the cause is upstream of anything the
 * generation screen can see. Without this panel the only symptom is "the output is worse than last
 * time", which is unactionable and reads as the product being unreliable rather than as one endpoint
 * being down and the cascade doing its job.
 *
 * WHAT `available` USED TO MEAN, AND WHY IT WAS WRONG. It was computed from the endpoint's protocol
 * alone — "does this codebase have an adapter for this" — and rendered here as a green `available`
 * badge. On a fresh install, where every hosted key is the placeholder `.env.example` ships, three
 * tiers reported themselves available with nothing behind them. A user asked why. The honest answer was
 * that the badge had never been about whether the endpoint would answer. The backend now reports
 * protocol support, credential presence and credential necessity as three separate facts, and this
 * screen renders them separately, because they have different remedies: a missing adapter is a gap in
 * ForgeOps that no configuration fixes, and a missing key is one only the operator can fix.
 *
 * NONE OF IT IS A PROBE. Configuration cannot prove an endpoint works, so `last_test_ok` — the result
 * of a real call somebody asked for — is shown as the only evidence on this page, and "never tested" is
 * kept visibly distinct from "working". Loading this page still probes nothing, because a dashboard that
 * called every vendor on render would put load on all of them every time somebody glanced at it.
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
  const credentials = useProviderCredentials();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Model tiers</h1>
        <p className="mt-1 text-muted-foreground">
          Read from <code>GET /api/v1/ai/tiers</code>. Which model tier each request would reach,
          what is configured for it, and what the last real call to it actually said.
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
          {tiers.data?.tiers.map((tier) => {
            const status = statusLabel(tier);
            const credential = credentials.data?.find((entry) => entry.key_ref === tier.key_ref);
            return (
              <li
                key={tier.name}
                className="rounded-lg border border-border bg-background p-4 text-sm"
                data-testid={`tier-${tier.name}`}
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h2 className="font-semibold">{tier.name}</h2>
                  {/* The words carry the state; colour only reinforces it. */}
                  <span data-testid={`availability-${tier.name}`} className={status.className}>
                    {status.text}
                  </span>
                </div>

                <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
                  <dt className="font-medium">Primary endpoint</dt>
                  <dd>
                    <code>{tier.primary_endpoint}</code>
                    <span className="ml-2 text-muted-foreground">{tier.primary_protocol}</span>
                  </dd>

                  <dt className="font-medium">Adapter</dt>
                  <dd data-testid={`protocol-${tier.name}`}>
                    {tier.protocol_supported
                      ? "This build can speak this endpoint's protocol."
                      : `This build has no adapter for ${tier.primary_protocol}. That is a gap in ForgeOps, not in your configuration — no key will make this tier work.`}
                  </dd>

                  <dt className="font-medium">Credential</dt>
                  <dd data-testid={`credential-${tier.name}`}>
                    {!tier.credential_required
                      ? "None needed. This endpoint is reached without one."
                      : tier.credential_configured
                        ? "A credential is configured. That it exists does not mean the provider accepts it."
                        : "No credential is configured, so requests to this tier are skipped and the cascade falls through to the next endpoint."}
                  </dd>

                  <dt className="font-medium">Last real call</dt>
                  <dd data-testid={`test-${tier.name}`}>
                    {tier.last_test_ok === null ? (
                      <span className="text-muted-foreground">
                        Never tested. Nothing on this page other than this line is evidence that the
                        endpoint answers.
                      </span>
                    ) : (
                      <>
                        <span
                          className={tier.last_test_ok ? "text-emerald-600" : "text-destructive"}
                        >
                          {tier.last_test_ok ? "Succeeded" : "Failed"}
                        </span>
                        {tier.last_tested_at ? (
                          <span className="ml-2 text-muted-foreground">{tier.last_tested_at}</span>
                        ) : null}
                        {tier.last_test_detail ? (
                          <p className="mt-1 font-mono break-words">{tier.last_test_detail}</p>
                        ) : null}
                      </>
                    )}
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

                {tier.reason ? (
                  <p className="mt-2 text-xs text-amber-600" data-testid={`reason-${tier.name}`}>
                    {tier.reason}
                  </p>
                ) : null}

                {/* Setting a key is only useful where an adapter exists to use it. */}
                {tier.credential_required && tier.protocol_supported && tier.key_ref ? (
                  <ProviderCredentialForm
                    keyRef={tier.key_ref}
                    endpointId={tier.primary_endpoint}
                    configured={credential}
                  />
                ) : null}
              </li>
            );
          })}
        </ul>
      </AsyncState>

      <CustomEndpointForm />

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          What these figures are, and what they are not.
        </p>
        <p className="mt-2">
          Only the <strong>primary</strong> endpoint of each tier is reported, because that is what
          the endpoint returns. The cascade behind it — primary, then a cross-vendor backup, then
          self-hosted, then the safe template library — is not enumerated here, so a tier whose
          primary is unusable does not mean the tier cannot serve a request. It means the next
          request to that tier will fall through, and a generation that took the fallback is not a
          failed generation.
        </p>
        <p className="mt-2">
          <strong>Configuration is not proof.</strong> Loading this page tests nothing. Only the
          &ldquo;last real call&rdquo; line is evidence, and it only exists for endpoints somebody
          pressed Test connection on. A page that probed every configured vendor on load would put
          load on all of them every time somebody glanced at a dashboard, and would report a
          transient failure as a state change.
        </p>
        <p className="mt-2">
          <strong>Keys are write-only.</strong> A stored credential is sealed and no route returns
          its value — what comes back is a length and the last four characters, which is enough to
          see that something is set and to tell two keys apart, and not enough to leak one into a
          screenshot.
        </p>
      </aside>
    </div>
  );
}
