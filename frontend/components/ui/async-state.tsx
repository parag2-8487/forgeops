// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import React from "react";
import { ApiProblemError } from "@/lib/api";

/**
 * The three states every live panel in this app has to render, in one place.
 *
 * It exists because the alternative is each page inventing its own, and the page that invents
 * its own is the page that renders a hardcoded array when the fetch is inconvenient — which is
 * the defect this whole route tree was built to remove. Anything reaching the network uses this.
 *
 * The error branch reads the RFC 9457 Problem Details the API client already normalises every
 * failure into, rather than printing `String(error)`. That is the point of `lib/api` doing the
 * normalisation: `title` and `detail` come from the backend's own contract, and `status` is the
 * real HTTP status, so what the user sees is what the server said.
 */

export function AsyncState({
  isPending,
  error,
  isEmpty,
  emptyMessage,
  label,
  children,
}: {
  isPending: boolean;
  error: unknown;
  isEmpty?: boolean;
  emptyMessage?: string;
  label: string;
  children: React.ReactNode;
}) {
  if (isPending) {
    return (
      <div
        role="status"
        aria-live="polite"
        className="rounded-lg border border-border bg-background p-6 text-sm text-muted-foreground"
      >
        Loading {label}…
      </div>
    );
  }

  if (error) return <ProblemPanel error={error} label={label} />;

  if (isEmpty) {
    return (
      <div
        role="status"
        className="rounded-lg border border-border bg-background p-6 text-sm text-muted-foreground"
      >
        {emptyMessage ?? `The backend returned no ${label}.`}
      </div>
    );
  }

  return <>{children}</>;
}

/**
 * A 401 or 403 is not an error worth alarming anyone about.
 *
 * This note used to say the app "ships no sign-in screen yet", which is why an unauthenticated
 * visitor saw this panel everywhere. That is no longer true: `app/login` starts the OIDC flow and
 * `AuthBoundary` redirects an anonymous visitor to it, so reaching a panel at all now means a
 * session was established.
 *
 * Which changes what a 401 here MEANS, and the panel says so. It is no longer "you never signed
 * in" — the client retries once through `POST /auth/refresh` before surfacing anything, so a 401
 * that survives to this component is a session that ended and could not be renewed. A 403 is
 * different again and always was: authenticated, and refused by policy. Those two are no longer
 * collapsed into one message.
 */
function ProblemPanel({ error, label }: { error: unknown; label: string }) {
  // `ApiTransportError extends ApiProblemError`, so this one check covers both and every
  // failure the client can produce carries `.problem`.
  const problem = error instanceof ApiProblemError ? error.problem : null;
  const status = problem?.status;

  if (status === 401) {
    return (
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-6 text-sm">
        <p className="font-semibold">Not authenticated to read {label}.</p>
        <p className="mt-2 text-muted-foreground">
          The backend answered <code>401</code>, and renewing from the session cookie did not
          succeed either — the client attempts <code>POST /auth/refresh</code> once before any 401
          reaches this panel. Signing in again is the usual fix. The panel reports that rather than
          showing sample data in its place.
        </p>
        <p className="mt-2 text-muted-foreground">
          If signing in does not help, the token is being <em>refused</em> rather than missing, and
          the cause is configuration rather than session age: the API requires the token&apos;s
          audience to match <code>OIDC_APP_AUDIENCE</code> and requires a <code>forgeops_role</code>{" "}
          claim, which the identity provider only emits when the <code>forgeops</code> scope is
          requested. <code>scripts/check-oidc-reachability.py</code> checks the surrounding
          topology.
        </p>
      </div>
    );
  }

  if (status === 403) {
    return (
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-6 text-sm">
        <p className="font-semibold">Not authorised to read {label}.</p>
        <p className="mt-2 text-muted-foreground">
          The backend answered <code>403</code>. You are signed in; this identity is refused by
          policy. §4.2 makes the response for a resource you may not read identical to the response
          for one that does not exist, so this does not tell you which — deliberately, because the
          person who benefits from being able to tell them apart is the one enumerating.
        </p>
      </div>
    );
  }

  return (
    <div
      role="alert"
      className="rounded-lg border border-destructive/40 bg-destructive/5 p-6 text-sm"
    >
      <p className="font-semibold">Could not load {label}.</p>
      {problem ? (
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-muted-foreground">
          <dt className="font-medium">Status</dt>
          <dd>{problem.status}</dd>
          <dt className="font-medium">Title</dt>
          <dd>{problem.title}</dd>
          {problem.detail ? (
            <>
              <dt className="font-medium">Detail</dt>
              <dd>{problem.detail}</dd>
            </>
          ) : null}
        </dl>
      ) : (
        <p className="mt-2 text-muted-foreground">
          An error with no Problem Details envelope. This should not happen: `lib/api` normalises
          every transport and HTTP failure into one, so seeing this means something bypassed the
          client.
        </p>
      )}
    </div>
  );
}
