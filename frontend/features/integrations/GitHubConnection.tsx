// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * Link, inspect and disconnect a GitHub account.
 *
 * NOT A SIGN-IN CONTROL, and the copy says so on its face. Authentik remains the only way into
 * ForgeOps; this attaches a GitHub credential to a session that already exists. A user who has not
 * signed in never sees this component, because the whole shell is behind the auth boundary.
 *
 * THREE STATES, NOT TWO, because they have three different remedies and the wrong one wastes a user's
 * time. `configured: false` is the operator's problem and is the state of every fresh install, so it
 * renders as the two settings to set rather than as an error. `connected: false` is the user's next
 * action. `connected: true` renders who is connected, what the link may do, and what the last real
 * call said.
 *
 * "CONNECTED" IS NOT "WORKS", so the last-use line is tri-state exactly as the pairing screen's
 * heartbeat is: never used, failed with a reason, or succeeded. A green tick on a link that has never
 * made a call would be a claim nobody measured.
 *
 * THE TOKEN IS NEVER DISPLAYED and there is no reveal control, for the reason the provider-credential
 * form gives: no route returns it, and a reveal affordance is the one that ends up in a screenshot.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiProblemError, queryKeys } from "@/lib/api";

/** Mirrors `GitHubLinkStatus` in `backend/src/integrations/routes.py`. */
export interface GitHubLinkStatus {
  configured: boolean;
  connected: boolean;
  configuration_hint: string;
  login: string | null;
  avatar_url: string | null;
  scopes: string[];
  connected_at: string | null;
  /** `null` means the link has never been used — not that it is fine, and not that it is broken. */
  last_use_ok: boolean | null;
  last_used_at: string | null;
  last_use_detail: string;
  access_token_expires_at: string | null;
}

/** Mirrors `ConnectResponse`. */
interface ConnectResponse {
  authorize_url: string;
  expires_in_seconds: number;
}

export function useGitHubLink() {
  return useQuery<GitHubLinkStatus>({
    queryKey: queryKeys.integrations.github(),
    queryFn: () => api.get<GitHubLinkStatus>("/integrations/github"),
  });
}

export function GitHubConnection() {
  const client = useQueryClient();
  const status = useGitHubLink();
  const [error, setError] = useState<string | null>(null);

  const connect = useMutation({
    mutationFn: () => api.post<ConnectResponse>("/integrations/github/connect"),
    onSuccess: (response) => {
      setError(null);
      // A full navigation rather than a fetch-followed redirect: the authorization page is GitHub's
      // and has to be rendered by the browser. `assign` rather than `replace`, so the browser's back
      // button returns here if the user abandons the authorization.
      window.location.assign(response.authorize_url);
    },
    onError: (caught: unknown) => setError(describe(caught)),
  });

  const disconnect = useMutation({
    mutationFn: () =>
      api.delete<{ login: string; revoked_at_github: boolean }>("/integrations/github"),
    onSuccess: (response) => {
      setError(
        response.revoked_at_github
          ? null
          : // Stated rather than swallowed: the local link is gone but the token may still be live at
            // GitHub, which is a different fact and the user may want to act on it.
            `Disconnected ${response.login}, but GitHub did not confirm the token was revoked. ` +
              "Review it under Settings → Applications on GitHub.",
      );
      void client.invalidateQueries({ queryKey: queryKeys.integrations.github() });
    },
    onError: (caught: unknown) => setError(describe(caught)),
  });

  if (status.isPending) {
    return <p data-testid="github-link-loading">Checking the GitHub connection…</p>;
  }
  if (status.isError || !status.data) {
    return (
      <p data-testid="github-link-error" role="alert">
        The GitHub connection status could not be read: {describe(status.error)}
      </p>
    );
  }

  const link = status.data;

  if (!link.configured) {
    return (
      <section data-testid="github-link-unconfigured" aria-labelledby="github-unconfigured-heading">
        <h3 id="github-unconfigured-heading">GitHub is not configured on this server</h3>
        <p>
          A GitHub account cannot be linked until an administrator registers a GitHub App for this
          deployment. This is the state of a fresh installation rather than a fault.
        </p>
        <p data-testid="github-link-hint">{link.configuration_hint}</p>
      </section>
    );
  }

  if (!link.connected) {
    return (
      <section data-testid="github-link-disconnected" aria-labelledby="github-connect-heading">
        <h3 id="github-connect-heading">Connect a GitHub account</h3>
        <p>
          Linking GitHub lets you pick a repository to clone onto your machine. It does not change
          how you sign in to ForgeOps, and it grants ForgeOps nothing beyond what the GitHub App
          asks for.
        </p>
        <Button
          type="button"
          data-testid="github-connect"
          disabled={connect.isPending}
          onClick={() => connect.mutate()}
        >
          {connect.isPending ? "Opening GitHub…" : "Connect GitHub"}
        </Button>
        {error ? (
          <p data-testid="github-link-problem" role="alert">
            {error}
          </p>
        ) : null}
      </section>
    );
  }

  return (
    <section data-testid="github-link-connected" aria-labelledby="github-connected-heading">
      <h3 id="github-connected-heading">Connected as {link.login}</h3>
      <dl>
        <dt>Connected</dt>
        <dd data-testid="github-connected-at">{link.connected_at ?? "unknown"}</dd>
        <dt>Last used</dt>
        <dd data-testid="github-last-use">{describeLastUse(link)}</dd>
      </dl>
      <Button
        type="button"
        variant="destructive"
        data-testid="github-disconnect"
        disabled={disconnect.isPending}
        onClick={() => disconnect.mutate()}
      >
        {disconnect.isPending ? "Disconnecting…" : "Disconnect GitHub"}
      </Button>
      {error ? (
        <p data-testid="github-link-problem" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}

/**
 * The tri-state, in words. Never used is its own sentence rather than an empty cell, because an empty
 * cell reads as "nothing wrong".
 */
export function describeLastUse(link: GitHubLinkStatus): string {
  if (link.last_use_ok === null) {
    return "never used — the link has not made a GitHub call yet";
  }
  const when = link.last_used_at ?? "an unknown time";
  return link.last_use_ok
    ? `worked at ${when}${link.last_use_detail ? ` — ${link.last_use_detail}` : ""}`
    : `failed at ${when}${link.last_use_detail ? ` — ${link.last_use_detail}` : ""}`;
}

/** The problem document's own detail where there is one, so the user reads the server's reason. */
function describe(caught: unknown): string {
  if (caught instanceof ApiProblemError) {
    return caught.problem.detail || caught.problem.title || "the request was refused";
  }
  return caught instanceof Error ? caught.message : "the request failed";
}
