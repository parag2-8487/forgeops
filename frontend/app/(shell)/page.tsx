// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";

/**
 * What the versioned health echo returns. Unauthenticated by design (§4.4), which makes it the
 * one panel in this app that shows real backend data from a cold start.
 */
interface HealthEcho {
  status: string;
  version: string;
  commit: string;
}

export default function HomePage() {
  const health = useQuery({
    queryKey: queryKeys.health.status(),
    queryFn: () => api.get<HealthEcho>("/health"),
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">ForgeOps Dashboard</h1>
        <p className="mt-1 text-muted-foreground">
          Analysis, generation and governed change for a codebase.
        </p>
      </div>

      <section
        aria-labelledby="start-heading"
        className="rounded-lg border border-primary/40 bg-primary/5 p-4"
      >
        <h2 id="start-heading" className="text-sm font-semibold">
          New installation?
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          There is an order to this, and skipping a step shows up several steps later as an error
          that does not name it. The most common one is publishing a policy bundle: without one the
          governance chokepoint refuses every change-set submission, which surfaces at the end of a
          generation run as a stale-bundle error four layers from its cause.
        </p>
        <p className="mt-2">
          <Link
            href="/onboarding"
            className="text-sm font-medium underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Follow the eight-step path →
          </Link>
        </p>
      </section>

      <section aria-labelledby="platform-heading" className="space-y-3">
        <h2 id="platform-heading" className="text-lg font-semibold">
          Platform
        </h2>
        <AsyncState isPending={health.isPending} error={health.error} label="platform health">
          <dl
            data-testid="health-panel"
            className="grid grid-cols-1 gap-4 sm:grid-cols-3"
            aria-label="Backend health"
          >
            <Stat label="Status" value={health.data?.status ?? "—"} />
            <Stat label="Version" value={health.data?.version ?? "—"} />
            <Stat label="Commit" value={health.data?.commit || "not stamped"} mono />
          </dl>
        </AsyncState>
        <p className="text-xs text-muted-foreground">
          Read live from <code>/api/v1/health</code>. This is one of the three unauthenticated
          routes, so it answers without a sign-in; the panels below do not.
        </p>
      </section>

      {/*
        THIS SECTION USED TO BE WRONG, and it is worth recording what it said. It read: "Seven of the
        nine routes read from real endpoints. Approvals and generation now have mounted, authenticated
        backend surfaces ... but their reviewer and wizard screens are not built, so those two say so
        rather than showing sample data." Both screens had been built for some time. The behaviour
        changed and the copy did not, which is the same defect as the readiness panel's stale
        five-category note, and it is worse than never having written the copy: a reader who trusts it
        stops looking for features that are there.

        So this no longer enumerates which routes are "live" — every one of them reads real data, and a
        list that says so for all twelve carries no information and is one edit away from being wrong
        again. What is worth stating is the one thing a reader cannot check from the navigation: which
        facts the app can observe and which it cannot.
      */}
      <section aria-labelledby="honesty-heading" className="space-y-3">
        <h2 id="honesty-heading" className="text-lg font-semibold">
          What this app can and cannot observe
        </h2>
        <p className="text-sm text-muted-foreground">
          Every screen reads a real endpoint; none renders sample data. Where a fact is not knowable
          from the API, the screen says so rather than showing a plausible value:
        </p>
        <ul className="space-y-2 text-sm">
          <Unknowable>
            <strong>Whether a policy bundle is published.</strong> There is no read route reporting
            a tenant&apos;s active bundle, so the onboarding path leaves step 5 unchecked rather
            than ticking it. The publish control reports the digest it activated.
          </Unknowable>
          <Unknowable>
            <strong>Whether an agent is attested.</strong> Pairing shows the certificate and the
            heartbeat, and distinguishes &ldquo;never reported&rdquo; from &ldquo;stale&rdquo;. It
            does not claim attestation, because Phase 1 ships no hardware-rooted attestation to
            read.
          </Unknowable>
          <Unknowable>
            <strong>A readiness score on the project list.</strong> A real score is an engine walk
            of the whole index, so the list reports whether anything is indexed and the detail page
            computes the score. A project that has never been scanned reads &ldquo;not
            scanned&rdquo; rather than zero.
          </Unknowable>
          <Unknowable>
            <strong>Whether a model endpoint is up right now.</strong> The tier panel reports the
            registry&apos;s last observation and the circuit-breaker state; loading it does not
            probe any vendor.
          </Unknowable>
        </ul>
        <p className="text-sm text-muted-foreground">
          Where an action would be refused by role, the control is not offered rather than offered
          and 403&apos;d — and where the server refuses a mutation, the refusal is rendered with
          what the rule is for and which step to go and do, not only the registry&apos;s own
          wording.
        </p>
      </section>
    </div>
  );
}

function Stat({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="rounded-lg border border-border bg-background p-4">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className={mono ? "mt-1 truncate font-mono text-sm" : "mt-1 text-sm font-semibold"}>
        {value}
      </dd>
    </div>
  );
}

function Unknowable({ children }: { children: React.ReactNode }) {
  return (
    <li className="rounded-md border border-border bg-background p-3 text-muted-foreground">
      {children}
    </li>
  );
}
