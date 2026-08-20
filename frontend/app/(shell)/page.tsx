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

      <section aria-labelledby="scope-heading" className="space-y-3">
        <h2 id="scope-heading" className="text-lg font-semibold">
          What is wired, and what is not
        </h2>
        <p className="text-sm text-muted-foreground">
          Seven of the nine routes read from real endpoints. Approvals and generation now have
          mounted, authenticated backend surfaces — approvals was an unmounted router requiring no
          authentication, and generation had no HTTP surface at all — but their reviewer and wizard
          screens are not built, so those two say so rather than showing sample data. Pairing became
          readable this pass: it had no GET, so a paired agent could not be observed.
        </p>
        <p className="text-sm text-muted-foreground">
          This dashboard used to render a single hardcoded project whose readiness score no backend
          had ever computed. The honest gap is the point: a visible one can be planned around.
        </p>
        <ul className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
          <ScopeItem href="/projects" live>
            Projects — <code>GET /projects/{"{id}"}</code> and its activity feed
          </ScopeItem>
          <ScopeItem href="/readiness" live>
            Readiness — a real <code>ReadinessEngine</code> evaluation
          </ScopeItem>
          <ScopeItem href="/audit" live>
            Audit — the hash-chained event log
          </ScopeItem>
          <ScopeItem href="/policies" live>
            Policies — the governance templates
          </ScopeItem>
          <ScopeItem href="/vault" live>
            Vault — secret references, never values
          </ScopeItem>
          <ScopeItem href="/approvals">
            Approvals — endpoint mounted, reviewer UI not built
          </ScopeItem>
          <ScopeItem href="/generation">
            Generation — SSE endpoint mounted, wizard not wired
          </ScopeItem>
          <ScopeItem href="/pairing" live>
            Pairing — observed device state, heartbeat included
          </ScopeItem>
        </ul>
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

function ScopeItem({
  href,
  live,
  children,
}: {
  href: string;
  live?: boolean;
  children: React.ReactNode;
}) {
  return (
    <li className="flex items-start gap-2 rounded-md border border-border bg-background p-3">
      <span
        aria-hidden="true"
        className={
          live
            ? "mt-1.5 h-2 w-2 shrink-0 rounded-full bg-emerald-500"
            : "mt-1.5 h-2 w-2 shrink-0 rounded-full bg-muted-foreground/40"
        }
      />
      <Link href={href} className="underline-offset-4 hover:underline">
        {children}
      </Link>
      <span className="sr-only">{live ? " (reads live data)" : " (not implemented)"}</span>
    </li>
  );
}
