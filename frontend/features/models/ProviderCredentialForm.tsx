// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * Set a provider's API key, and prove it works with a real call.
 *
 * WHY THIS SCREEN EXISTS. The Models page reported five hosted tiers, three of them "available", on a fresh
 * install where the only credentials were the placeholder values `.env.example` ships. `available` was
 * computed from the endpoint's PROTOCOL — "does ForgeOps have an adapter for this" — and rendered as though
 * it meant "this will answer". A user reasonably asked why models they had never configured a key for were
 * reported as available. The only way to supply a real key was to edit `.env` and restart.
 *
 * THE KEY IS NEVER READ BACK. There is no route that returns it and no reveal control here. What comes back
 * is a length and the last four characters — enough to see that something is configured and to tell two keys
 * for one provider apart. A reveal button is the affordance that ends up in a screenshot.
 *
 * "CONFIGURED" IS NOT "WORKS", and this component keeps them apart. Setting a key makes the tier available,
 * which means only that nothing is known to be missing. Whether the provider accepts it is a separate fact
 * with its own control and its own result, because a key can be expired, revoked, or for the wrong account.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiProblemError, queryKeys } from "@/lib/api";

/** Mirrors `ProviderCredentialResponse` in `backend/src/ai/routes.py`. */
export interface ProviderCredential {
  key_ref: string;
  length: number;
  hint: string;
  last_tested_at: string | null;
  last_test_ok: boolean | null;
  last_test_detail: string;
}

/** Mirrors `ConnectionTestResponse`. */
export interface ConnectionTest {
  endpoint_id: string;
  ok: boolean;
  detail: string;
  model_reported: string | null;
  latency_ms: number;
}

export function ProviderCredentialForm({
  keyRef,
  endpointId,
  configured,
}: {
  keyRef: string;
  /** The endpoint to test with. Testing needs a model and a base URL, which belong to an endpoint. */
  endpointId: string;
  configured: ProviderCredential | undefined;
}) {
  const client = useQueryClient();
  const [value, setValue] = useState("");
  const [testResult, setTestResult] = useState<ConnectionTest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => api.put<ProviderCredential>(`/ai/credentials/${keyRef}`, { value }),
    onSuccess: async () => {
      setValue("");
      // The test result described the PREVIOUS key. Leaving it on screen beside a newly saved one is the
      // same stale-evidence problem this whole screen exists to correct.
      setTestResult(null);
      setError(null);
      await client.invalidateQueries({ queryKey: queryKeys.ai.all });
    },
    onError: (caught) => {
      setError(
        caught instanceof ApiProblemError
          ? (caught.problem.detail ?? caught.problem.title)
          : "Could not save the credential.",
      );
    },
  });

  const remove = useMutation({
    mutationFn: () => api.delete<void>(`/ai/credentials/${keyRef}`),
    onSuccess: async () => {
      setTestResult(null);
      setError(null);
      await client.invalidateQueries({ queryKey: queryKeys.ai.all });
    },
  });

  const test = useMutation({
    mutationFn: () => api.post<ConnectionTest>(`/ai/endpoints/${endpointId}/test`),
    onSuccess: async (result) => {
      setTestResult(result);
      setError(null);
      // The outcome is recorded against the credential, so the tier list can show it too.
      await client.invalidateQueries({ queryKey: queryKeys.ai.all });
    },
    onError: (caught) => {
      setTestResult(null);
      setError(
        caught instanceof ApiProblemError
          ? (caught.problem.detail ?? caught.problem.title)
          : "Could not reach the server to run the test.",
      );
    },
  });

  return (
    <div className="mt-3 space-y-2 rounded-md border border-border bg-muted/20 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <label className="text-xs font-medium" htmlFor={`cred-${keyRef}`}>
          API key for <code>{keyRef}</code>
        </label>
        {configured ? (
          <span className="text-xs text-muted-foreground" data-testid={`cred-configured-${keyRef}`}>
            {/* The length and last four, never the value. */}
            configured · {configured.length} characters ending {configured.hint}
          </span>
        ) : (
          <span className="text-xs text-amber-600" data-testid={`cred-absent-${keyRef}`}>
            not configured
          </span>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        <input
          id={`cred-${keyRef}`}
          // `password` so it is not shoulder-read or captured by a screen recording. It is write-only
          // regardless: nothing reads the stored value back into this field.
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder={
            configured
              ? "paste a new key to replace the current one"
              : "paste the provider's API key"
          }
          className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-xs"
          data-testid={`cred-input-${keyRef}`}
        />
        <Button
          size="sm"
          disabled={value.trim() === "" || save.isPending}
          onClick={() => save.mutate()}
          data-testid={`cred-save-${keyRef}`}
        >
          {save.isPending ? "Saving…" : configured ? "Replace" : "Save"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={test.isPending}
          onClick={() => test.mutate()}
          data-testid={`cred-test-${keyRef}`}
        >
          {test.isPending ? "Testing…" : "Test connection"}
        </Button>
        {configured ? (
          <Button
            size="sm"
            variant="outline"
            disabled={remove.isPending}
            onClick={() => remove.mutate()}
            data-testid={`cred-remove-${keyRef}`}
          >
            Remove
          </Button>
        ) : null}
      </div>

      <p className="text-xs text-muted-foreground">
        Test connection sends one real request — one token, temperature zero — so it costs a
        fraction of a cent and proves the key is accepted, the model name exists and the host
        answers. Saving a key alone proves none of those.
      </p>

      {error !== null ? (
        <p className="text-xs text-destructive" data-testid={`cred-error-${keyRef}`}>
          {error}
        </p>
      ) : null}

      {testResult !== null ? (
        <div
          className={testResult.ok ? "text-xs text-emerald-600" : "text-xs text-destructive"}
          data-testid={`cred-test-result-${keyRef}`}
        >
          <p className="font-medium">
            {testResult.ok ? "The endpoint answered" : "The endpoint refused"} ·{" "}
            {testResult.latency_ms}ms
          </p>
          {/* The provider's own words. 401, 404 and a DNS failure have three different remedies, and no
              operator can choose between them from "test failed". */}
          <p className="mt-1 font-mono break-words">{testResult.detail}</p>
          {testResult.model_reported && testResult.model_reported !== endpointId ? (
            <p className="mt-1">It served {testResult.model_reported}.</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** Every stored credential, keyed by `key_ref`, for the tier list to look itself up in. */
export function useProviderCredentials() {
  return useQuery({
    queryKey: [...queryKeys.ai.all, "credentials"] as const,
    queryFn: () => api.get<ProviderCredential[]>("/ai/credentials"),
    retry: false,
  });
}
