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
 * A 401 is not an error worth alarming anyone about, it is the design.
 *
 * §4.4 makes every route except the probes, the docs and the auth flow itself require an
 * authenticated principal, and this app ships no sign-in screen yet — so an unauthenticated
 * visitor SHOULD see this on every live panel. Saying so plainly is more honest than a red
 * failure box, and considerably more honest than filling the panel with sample data to avoid
 * the question.
 */
function ProblemPanel({ error, label }: { error: unknown; label: string }) {
  // `ApiTransportError extends ApiProblemError`, so this one check covers both and every
  // failure the client can produce carries `.problem`.
  const problem = error instanceof ApiProblemError ? error.problem : null;
  const status = problem?.status;

  if (status === 401 || status === 403) {
    return (
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-6 text-sm">
        <p className="font-semibold">Sign-in required to read {label}.</p>
        <p className="mt-2 text-muted-foreground">
          The backend answered <code>{status}</code>. This is the intended behaviour, not a failure:
          §4.4 makes every route other than the health probes, the API documentation and the auth
          flow itself require an authenticated principal. Phase 1 ships the OIDC flow on the backend
          but no sign-in screen in this shell, so these panels stay unreadable from a cold start.
          The panel reports that rather than showing sample data in its place.
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
