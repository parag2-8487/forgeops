// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * Register a model this deployment was not shipped knowing about.
 *
 * WHY. The shipped cascade names six tiers and a fixed set of endpoints from `config/model-tiers.yaml`. A
 * user running a model that file has never heard of — another Ollama host, a vLLM server, an OpenAI-compatible
 * gateway, a provider added after this release — had no way to reach it except editing YAML inside the image
 * and rebuilding. That is not a configuration story, it is a fork.
 *
 * TEST BEFORE SAVING, which is the whole reason the probe route exists separately from the endpoint test. A
 * base URL and a model name are two independent chances to make a typo, and the failure they produce arrives
 * much later as a degraded generation. `POST /ai/endpoints/probe` makes one real call against the values in
 * this form before any of them are written down; the credential it uses is not stored.
 *
 * IT DOES NOT SERVE TRAFFIC UNTIL A RESTART, and this says so rather than letting the user find out. The
 * router, the circuit breakers and the semantic cache are all built from `TierConfig` during the application
 * lifespan, so a row added now joins the cascade the next time that runs. Pretending otherwise would produce
 * exactly the silent-degradation shape the rest of this screen exists to remove.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiProblemError, queryKeys } from "@/lib/api";

import type { ConnectionTest } from "./ProviderCredentialForm";

/** Mirrors `CustomEndpointResponse` in `backend/src/ai/routes.py`. */
export interface CustomEndpoint {
  id: string;
  model: string;
  base_url: string;
  protocol: string;
  key_ref: string | null;
  tier: string;
}

/** The tiers a custom endpoint may join. Mirrors `ModelTier` in `backend/src/ai/routing/tiers.py`. */
const TIERS = [
  "high_coding",
  "high_analysis",
  "medium",
  "medium_value",
  "low_logs",
  "self_hosted",
] as const;

export function useCustomEndpoints() {
  return useQuery({
    queryKey: [...queryKeys.ai.all, "custom-endpoints"] as const,
    queryFn: () => api.get<CustomEndpoint[]>("/ai/endpoints/custom"),
    retry: false,
  });
}

export function CustomEndpointForm() {
  const client = useQueryClient();
  const existing = useCustomEndpoints();

  const [id, setId] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [tier, setTier] = useState<string>("self_hosted");
  const [credential, setCredential] = useState("");
  const [probe, setProbe] = useState<ConnectionTest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const complete = id.trim() !== "" && model.trim() !== "" && baseUrl.trim() !== "";

  const runProbe = useMutation({
    mutationFn: () =>
      api.post<ConnectionTest>("/ai/endpoints/probe", {
        base_url: baseUrl.trim(),
        model: model.trim(),
        // Sent to be used for this one call and not persisted. An empty box means "no credential",
        // which is the correct and common case for a self-hosted server.
        credential: credential.trim() === "" ? null : credential.trim(),
      }),
    onSuccess: (result) => {
      setProbe(result);
      setError(null);
    },
    onError: (caught) => {
      setProbe(null);
      setError(
        caught instanceof ApiProblemError
          ? (caught.problem.detail ?? caught.problem.title)
          : "Could not reach the server to run the probe.",
      );
    },
  });

  const save = useMutation({
    mutationFn: () =>
      api.put<CustomEndpoint>(`/ai/endpoints/custom/${id.trim()}`, {
        model: model.trim(),
        base_url: baseUrl.trim(),
        tier,
        key_ref: credential.trim() === "" ? null : id.trim(),
      }),
    onSuccess: async (result) => {
      setSaved(result.id);
      setError(null);
      await client.invalidateQueries({ queryKey: queryKeys.ai.all });
    },
    onError: (caught) => {
      setError(
        caught instanceof ApiProblemError
          ? (caught.problem.detail ?? caught.problem.title)
          : "Could not save the endpoint.",
      );
    },
  });

  const remove = useMutation({
    mutationFn: (endpointId: string) => api.delete<void>(`/ai/endpoints/custom/${endpointId}`),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: queryKeys.ai.all });
    },
  });

  return (
    <section className="rounded-lg border border-border bg-background p-4">
      <h2 className="text-sm font-semibold">Add your own model</h2>
      <p className="mt-1 text-xs text-muted-foreground">
        Any server speaking the OpenAI-compatible <code>/chat/completions</code> API — another
        Ollama or vLLM host, a gateway, or a provider this release does not ship a tier for. It
        joins the chosen tier&apos;s self-hosted fallbacks rather than becoming its primary, so
        adding one cannot silently redirect traffic away from an endpoint that already works.
      </p>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <label className="text-xs font-medium" htmlFor="custom-id">
            Endpoint id
          </label>
          <input
            id="custom-id"
            value={id}
            onChange={(event) => setId(event.target.value)}
            placeholder="my-local-qwen"
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
            data-testid="custom-id"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Your name for it. Saving again with the same id edits that endpoint.
          </p>
        </div>

        <div>
          <label className="text-xs font-medium" htmlFor="custom-model">
            Model name
          </label>
          <input
            id="custom-model"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder="qwen2.5-coder:7b"
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
            data-testid="custom-model"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Exactly as the server names it. A probe reports which model actually answered, so a
            substitution shows up there.
          </p>
        </div>

        <div>
          <label className="text-xs font-medium" htmlFor="custom-base-url">
            Base URL
          </label>
          <input
            id="custom-base-url"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
            placeholder="http://host.docker.internal:11434/v1"
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
            data-testid="custom-base-url"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Including the version path. <code>/chat/completions</code> is appended to it.
          </p>
        </div>

        <div>
          <label className="text-xs font-medium" htmlFor="custom-tier">
            Tier
          </label>
          <select
            id="custom-tier"
            value={tier}
            onChange={(event) => setTier(event.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
            data-testid="custom-tier"
          >
            {TIERS.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-muted-foreground">
            Which tier&apos;s cascade it backs up.
          </p>
        </div>

        <div className="sm:col-span-2">
          <label className="text-xs font-medium" htmlFor="custom-credential">
            API key (leave empty for a server that needs none)
          </label>
          <input
            id="custom-credential"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={credential}
            onChange={(event) => setCredential(event.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
            data-testid="custom-credential"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Used for the probe immediately. Stored only when you save, sealed, and never returned by
            any route.
          </p>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={!complete || runProbe.isPending}
          onClick={() => runProbe.mutate()}
          data-testid="custom-probe"
        >
          {runProbe.isPending ? "Probing…" : "Probe before saving"}
        </Button>
        <Button
          size="sm"
          disabled={!complete || save.isPending}
          onClick={() => save.mutate()}
          data-testid="custom-save"
        >
          {save.isPending ? "Saving…" : "Save endpoint"}
        </Button>
      </div>

      {error !== null ? (
        <p className="mt-2 text-xs text-destructive" data-testid="custom-error">
          {error}
        </p>
      ) : null}

      {probe !== null ? (
        <div
          className={probe.ok ? "mt-2 text-xs text-emerald-600" : "mt-2 text-xs text-destructive"}
          data-testid="custom-probe-result"
        >
          <p className="font-medium">
            {probe.ok ? "The server answered" : "The server did not answer"} · {probe.latency_ms}ms
          </p>
          <p className="mt-1 font-mono break-words">{probe.detail}</p>
          {probe.model_reported && probe.model_reported !== model.trim() ? (
            <p className="mt-1">
              It answered as <code>{probe.model_reported}</code>, not <code>{model.trim()}</code>.
              The server substituted a different model, which is worth knowing before this endpoint
              serves a generation.
            </p>
          ) : null}
        </div>
      ) : null}

      {saved !== null ? (
        <p className="mt-2 text-xs text-amber-600" data-testid="custom-saved">
          Saved <code>{saved}</code>. It will not serve traffic until the backend restarts — the
          router and the circuit breakers are built from the tier configuration at start-up.
        </p>
      ) : null}

      <div className="mt-4">
        <h3 className="text-xs font-semibold">Endpoints you have added</h3>
        {existing.data && existing.data.length > 0 ? (
          <ul className="mt-2 space-y-1" data-testid="custom-list">
            {existing.data.map((endpoint) => (
              <li
                key={endpoint.id}
                className="flex flex-wrap items-baseline justify-between gap-2 rounded-md border border-border px-2 py-1 text-xs"
                data-testid={`custom-row-${endpoint.id}`}
              >
                <span>
                  <code>{endpoint.id}</code> → <code>{endpoint.model}</code> at{" "}
                  <code>{endpoint.base_url}</code>
                  <span className="ml-2 text-muted-foreground">{endpoint.tier}</span>
                </span>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => remove.mutate(endpoint.id)}
                  data-testid={`custom-remove-${endpoint.id}`}
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-xs text-muted-foreground" data-testid="custom-empty">
            None yet. The six shipped tiers are unchanged.
          </p>
        )}
      </div>
    </section>
  );
}
