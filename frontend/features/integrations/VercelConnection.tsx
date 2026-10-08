// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * Link, inspect and disconnect a Vercel account via personal access token.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { api, ApiProblemError, queryKeys } from "@/lib/api";

export interface VercelLinkStatus {
  configured: boolean;
  connected: boolean;
  username: string | null;
  email: string | null;
  token_hint: string | null;
  last_tested_at: string | null;
  last_test_ok: boolean | null;
  last_test_detail: string;
}

export function useVercelLink() {
  return useQuery<VercelLinkStatus>({
    queryKey: queryKeys.integrations.vercel(),
    queryFn: () => api.get<VercelLinkStatus>("/integrations/vercel"),
  });
}

function describe(err: unknown): string {
  if (err instanceof ApiProblemError) {
    return err.problem.detail || err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return String(err);
}

export function VercelConnection() {
  const client = useQueryClient();
  const status = useVercelLink();
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState("");

  const linkWithToken = useMutation({
    mutationFn: () =>
      api.put<VercelLinkStatus>("/integrations/vercel/token", { token: token.trim() }),
    onSuccess: () => {
      setError(null);
      setToken("");
      void client.invalidateQueries({ queryKey: queryKeys.integrations.vercel() });
    },
    onError: (caught: unknown) => setError(describe(caught)),
  });

  const disconnect = useMutation({
    mutationFn: () => api.delete<VercelLinkStatus>("/integrations/vercel"),
    onSuccess: () => {
      setError(null);
      void client.invalidateQueries({ queryKey: queryKeys.integrations.vercel() });
    },
    onError: (caught: unknown) => setError(describe(caught)),
  });

  if (status.isLoading) {
    return (
      <Card data-testid="vercel-link-loading">
        <CardHeader>
          <CardTitle className="text-lg font-semibold">Vercel Deployment Integration</CardTitle>
          <CardDescription>Checking your Vercel connection status…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const link = status.data;
  const isConnected = link?.connected ?? false;

  if (!isConnected) {
    return (
      <Card data-testid="vercel-link-disconnected" aria-labelledby="vercel-connect-heading">
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle id="vercel-connect-heading" className="text-lg font-semibold">
              Connect Vercel Account
            </CardTitle>
            <Badge variant="outline">Not Connected</Badge>
          </div>
          <CardDescription>
            Link your Vercel Access Token to enable seamless, one-click production deployments
            directly from ForgeOps projects.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <form
            data-testid="vercel-token-form"
            className="space-y-4 rounded-lg border border-border bg-muted/20 p-4"
            onSubmit={(event) => {
              event.preventDefault();
              linkWithToken.mutate();
            }}
          >
            <div className="space-y-1.5">
              <label htmlFor="vercel-token-input" className="block text-sm font-medium">
                Vercel Access Token
              </label>
              <input
                id="vercel-token-input"
                data-testid="vercel-token-input"
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="vcp_..."
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>

            <div className="rounded-md border border-border/60 bg-background/50 p-3 text-xs text-muted-foreground space-y-1.5">
              <p>
                Get your token from{" "}
                <a
                  href="https://vercel.com/account/tokens"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-medium text-primary underline underline-offset-2 hover:opacity-80"
                >
                  vercel.com/account/tokens →
                </a>
                . The token is encrypted and stored sealed using envelope encryption.
              </p>
            </div>

            <Button
              type="submit"
              data-testid="vercel-token-submit"
              disabled={linkWithToken.isPending || token.trim().length < 10}
              className="w-full sm:w-auto"
            >
              {linkWithToken.isPending ? "Validating with Vercel…" : "Connect Vercel"}
            </Button>
          </form>

          {error ? (
            <div
              data-testid="vercel-link-problem"
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
            >
              {error}
            </div>
          ) : null}
        </CardContent>
      </Card>
    );
  }

  return (
    <Card data-testid="vercel-link-connected" aria-labelledby="vercel-connected-heading">
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle id="vercel-connected-heading" className="text-lg font-semibold">
            Connected as @{link?.username || "Vercel User"}
          </CardTitle>
          <Badge variant="success">Connected</Badge>
        </div>
        <CardDescription>
          Your account is linked to Vercel. You can deploy projects to Vercel with one click.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 rounded-lg border border-border bg-muted/20 p-4">
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">
              User / Account
            </dt>
            <dd data-testid="vercel-account-name" className="mt-1 font-mono text-xs">
              {link?.username ? `@${link.username}` : "Connected"}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">Token Hint</dt>
            <dd data-testid="vercel-token-hint" className="mt-1 font-mono text-xs">
              {link?.token_hint ? `••••••••${link.token_hint}` : "••••••••"}
            </dd>
          </div>
        </dl>

        <div className="flex gap-3">
          <Button
            type="button"
            variant="destructive"
            data-testid="vercel-disconnect"
            disabled={disconnect.isPending}
            onClick={() => disconnect.mutate()}
          >
            {disconnect.isPending ? "Disconnecting…" : "Disconnect Vercel"}
          </Button>
        </div>

        {error ? (
          <div
            data-testid="vercel-link-problem"
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
          >
            {error}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
