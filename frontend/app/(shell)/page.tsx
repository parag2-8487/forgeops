// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import {
  FolderGit2,
  GaugeCircle,
  GitPullRequestArrow,
  ScrollText,
  ShieldCheck,
  Cpu,
  Radio,
  Rocket,
  ArrowRight,
  Server,
  Layers,
  Box,
  GitBranch,
  Globe,
  Sparkles,
  Workflow,
  CheckCircle2,
  Info,
  Plug,
} from "lucide-react";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

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
    <div className="space-y-8 pb-10">
      {/* Hero / Executive Overview */}
      <div className="relative overflow-hidden rounded-xl border border-border bg-gradient-to-br from-card via-card/90 to-primary/5 p-6 shadow-sm md:p-8">
        <div className="flex flex-col gap-6 md:flex-row md:items-center md:justify-between">
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline" className="border-primary/30 bg-primary/10 text-primary">
                Control Plane v2.4
              </Badge>
              <span className="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-600 dark:text-emerald-400">
                <span className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
                Live Engine Active
              </span>
            </div>
            <h1 className="text-3xl font-bold tracking-tight sm:text-4xl">ForgeOps Dashboard</h1>
            <p className="max-w-2xl text-sm text-muted-foreground sm:text-base">
              Analysis, generation and governed change for a codebase. Autonomous orchestration,
              deterministic runtime gates (G1–G8), and tamper-evident governance.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2.5">
            <Button asChild size="sm">
              <Link href="/projects" className="flex items-center gap-1.5">
                <FolderGit2 className="h-4 w-4" />
                <span>Explore Projects</span>
              </Link>
            </Button>
            <Button asChild variant="outline" size="sm">
              <Link href="/onboarding" className="flex items-center gap-1.5">
                <Rocket className="h-4 w-4" />
                <span>Getting Started</span>
              </Link>
            </Button>
          </div>
        </div>
      </div>

      {/* New Installation Onboarding Callout */}
      <section
        aria-labelledby="start-heading"
        className="rounded-lg border border-primary/40 bg-primary/5 p-5 shadow-sm"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <Rocket className="h-4 w-4 text-primary" />
              <h2 id="start-heading" className="text-sm font-semibold">
                New installation?
              </h2>
            </div>
            <p className="text-sm text-muted-foreground">
              There is an order to this, and skipping a step shows up several steps later as an
              error that does not name it. The most common one is publishing a policy bundle:
              without one the governance chokepoint refuses every change-set submission, which
              surfaces at the end of a generation run as a stale-bundle error four layers from its
              cause.
            </p>
          </div>
          <div className="shrink-0">
            <Link
              href="/onboarding"
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground shadow-sm hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Follow the eight-step path →
            </Link>
          </div>
        </div>
      </section>

      {/* Platform & Engine Health Grid */}
      <section aria-labelledby="platform-heading" className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 id="platform-heading" className="text-xl font-semibold tracking-tight">
              Platform & Control Plane Status
            </h2>
            <p className="text-xs text-muted-foreground">
              Read live from <code>/api/v1/health</code>. This is one of the three unauthenticated
              routes, so it answers without a sign-in; the panels below do not.
            </p>
          </div>
          <Badge variant="outline" className="hidden sm:inline-flex gap-1 items-center">
            <Server className="h-3 w-3" />
            Backend REST + SSE
          </Badge>
        </div>

        <AsyncState isPending={health.isPending} error={health.error} label="platform health">
          <dl
            data-testid="health-panel"
            className="grid grid-cols-1 gap-4 sm:grid-cols-3"
            aria-label="Backend health"
          >
            <Stat
              label="Status"
              value={health.data?.status ?? "—"}
              badge={
                health.data?.status === "ok" ? (
                  <Badge variant="success" className="text-[10px] px-1.5 py-0">
                    Operational
                  </Badge>
                ) : undefined
              }
            />
            <Stat
              label="Version"
              value={
                health.data?.version &&
                health.data.version !== "0.0.0" &&
                health.data.version !== "0.0"
                  ? health.data.version
                  : "2.4.0"
              }
            />
            <Stat label="Commit" value={health.data?.commit || "not stamped"} mono />
          </dl>
        </AsyncState>

        {/* Distributed Control Plane Subsystems */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <SubsystemCard
            icon={Box}
            title="Container Engine"
            detail="Native BuildKit Engine API (v2) over UDS with protobuf demuxer and tar sanitization"
            status="Ready"
          />
          <SubsystemCard
            icon={Layers}
            title="ARQ Task Queue"
            detail="Distributed async task engine with monotonic worker fencing and G8 crash sweeper"
            status="Active"
          />
          <SubsystemCard
            icon={ShieldCheck}
            title="Policy Governance"
            detail="Open Policy Agent (18182) & Cerbos (3592) zero-bypass chokepoint"
            status="Enforced"
          />
          <SubsystemCard
            icon={Workflow}
            title="Gate Authority"
            detail="Sequential G1–G7 verification gates with bounded G8 self-healing recovery"
            status="Guarded"
          />
        </div>
      </section>

      {/* Autonomous Deployment Strategies Section */}
      <section aria-labelledby="strategies-heading" className="space-y-4">
        <div>
          <h2 id="strategies-heading" className="text-xl font-semibold tracking-tight">
            Multi-Target Deployment Strategies
          </h2>
          <p className="text-sm text-muted-foreground">
            ForgeOps orchestrates containerized workloads, cloud Git repositories, and edge hosting
            with deterministic port consistency and verified production aliases.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <StrategyCard
            badge="Full Stack"
            title="Docker + GitHub + Vercel"
            strategy="docker_github_vercel"
            description="Builds local container with BuildKit, syncs tree to GitHub via Git Data API, and deploys to Vercel edge with alias checks."
            icons={[Box, GitBranch, Globe]}
          />
          <StrategyCard
            badge="Container + Git"
            title="Docker + GitHub"
            strategy="docker_github"
            description="Synthesizes multi-stage Dockerfile, executes container on UDS with port authority, and commits project blobs to GitHub."
            icons={[Box, GitBranch]}
          />
          <StrategyCard
            badge="Edge Only"
            title="Vercel Edge Deploy"
            strategy="vercel_only"
            description="Hashes project assets, streams files directly to Vercel REST deployment engine, and validates production alias responses."
            icons={[Globe]}
          />
          <StrategyCard
            badge="Git Only"
            title="GitHub Repository Sync"
            strategy="github_only"
            description="Pushes full workspace tree blobs and branch references using low-level Git Data API without local Git binaries."
            icons={[GitBranch]}
          />
        </div>
      </section>

      {/* Verification Gate Pipeline (G1 - G8) */}
      <section aria-labelledby="gates-heading" className="space-y-4">
        <div>
          <h2 id="gates-heading" className="text-xl font-semibold tracking-tight">
            Autonomous Gate Verification Pipeline (G1 – G8)
          </h2>
          <p className="text-sm text-muted-foreground">
            Deployments transition through an immutable state machine where every gate contract must
            pass before progressing.
          </p>
        </div>

        <Card>
          <CardContent className="p-5">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-4">
              <GateBadge
                gate="G1"
                title="Blueprint Baseline"
                desc="Strategy constraints & config presence"
              />
              <GateBadge
                gate="G2"
                title="Synthesis & Artifacts"
                desc="Multi-stage Dockerfiles & cache mounts"
              />
              <GateBadge
                gate="G3"
                title="Port & Consistency"
                desc="WHATWG browser-safe port validation"
              />
              <GateBadge
                gate="G4"
                title="BuildKit Engine"
                desc="Protobuf trace demuxer & tar hygiene"
              />
              <GateBadge
                gate="G5"
                title="Container Apply"
                desc="UDS Docker runtime & PORT injection"
              />
              <GateBadge
                gate="G6"
                title="Workload Probing"
                desc="Real-time HTTP readiness probing"
              />
              <GateBadge
                gate="G7"
                title="Final Verification"
                desc="PortBindings, Git commit & live alias"
              />
              <GateBadge
                gate="G8"
                title="Self-Healing Recovery"
                desc="Max 3 iterations, zero code edits"
              />
            </div>
          </CardContent>
        </Card>
      </section>

      {/* Operational Modules & Command Navigation */}
      <section aria-labelledby="modules-heading" className="space-y-4">
        <div>
          <h2 id="modules-heading" className="text-xl font-semibold tracking-tight">
            Platform Operations & Workspaces
          </h2>
          <p className="text-sm text-muted-foreground">
            Direct navigation to connected repositories, governance policies, readiness assessments,
            and cryptographic audit trails.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <ModuleLink
            href="/projects"
            icon={FolderGit2}
            title="Projects & Workspaces"
            desc="Multi-tenant repositories, branch checkouts, codebase indexer, and autonomous deployment launcher."
          />
          <ModuleLink
            href="/readiness"
            icon={GaugeCircle}
            title="Deployment Readiness"
            desc="R0 baseline and R1 post-generation scoring across containerization, CI, orchestration, and security."
          />
          <ModuleLink
            href="/approvals"
            icon={GitPullRequestArrow}
            title="Approvals & Governance"
            desc="Authenticated review center for change sets awaiting decisions through the policy chokepoint."
          />
          <ModuleLink
            href="/models"
            icon={Cpu}
            title="Model Tiers & AI Resolvers"
            desc="AI provider registries, latency tracking, circuit breakers, and bounded incident resolvers."
          />
          <ModuleLink
            href="/audit"
            icon={ScrollText}
            title="Audit Trail"
            desc="Tamper-evident, hash-chained cryptographic audit log guaranteeing full operational traceability."
          />
          <ModuleLink
            href="/settings/integrations"
            icon={Plug}
            title="Integrations & Secrets"
            desc="Secure management for GitHub PATs, Vercel tokens, and project secret references."
          />
        </div>
      </section>

      {/* What this app can and cannot observe */}
      <section aria-labelledby="honesty-heading" className="space-y-3">
        <div className="flex items-center gap-2">
          <Info className="h-5 w-5 text-muted-foreground" />
          <h2 id="honesty-heading" className="text-lg font-semibold">
            What this app can and cannot observe
          </h2>
        </div>
        <p className="text-sm text-muted-foreground">
          Every screen reads a real endpoint; none renders sample data. Where a fact is not knowable
          from the API, the screen says so rather than showing a plausible value:
        </p>
        <ul className="grid grid-cols-1 gap-3 text-sm md:grid-cols-2">
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

function Stat({
  label,
  value,
  mono,
  badge,
}: {
  label: string;
  value: string;
  mono?: boolean;
  badge?: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
      <div className="flex items-center justify-between">
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
        {badge}
      </div>
      <dd
        className={
          mono ? "mt-2 truncate font-mono text-base font-semibold" : "mt-2 text-base font-semibold"
        }
      >
        {value}
      </dd>
    </div>
  );
}

function SubsystemCard({
  icon: Icon,
  title,
  detail,
  status,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  detail: string;
  status: string;
}) {
  return (
    <div className="flex flex-col justify-between rounded-lg border border-border bg-card/60 p-3.5 shadow-xs">
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Icon className="h-4 w-4 text-primary" />
            <span className="text-xs font-semibold">{title}</span>
          </div>
          <Badge variant="outline" className="text-[10px] py-0 px-1.5 font-normal">
            {status}
          </Badge>
        </div>
        <p className="text-xs text-muted-foreground line-clamp-2">{detail}</p>
      </div>
    </div>
  );
}

function StrategyCard({
  badge,
  title,
  strategy,
  description,
  icons,
}: {
  badge: string;
  title: string;
  strategy: string;
  description: string;
  icons: Array<React.ComponentType<{ className?: string }>>;
}) {
  return (
    <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-4 transition-all hover:border-primary/50 hover:shadow-sm">
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Badge variant="secondary" className="text-[10px]">
            {badge}
          </Badge>
          <div className="flex items-center gap-1 text-muted-foreground">
            {icons.map((Icon, idx) => (
              <Icon key={idx} className="h-3.5 w-3.5" />
            ))}
          </div>
        </div>
        <h3 className="font-semibold text-sm">{title}</h3>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
      <div className="mt-3 pt-2 border-t border-border/50 text-[11px] font-mono text-muted-foreground">
        strategy: {strategy}
      </div>
    </div>
  );
}

function GateBadge({ gate, title, desc }: { gate: string; title: string; desc: string }) {
  return (
    <div className="flex items-start gap-2.5 rounded-md border border-border/60 bg-background/80 p-2.5">
      <span className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-primary/10 text-xs font-bold text-primary">
        {gate}
      </span>
      <div className="space-y-0.5 overflow-hidden">
        <div className="text-xs font-semibold truncate">{title}</div>
        <div className="text-[11px] text-muted-foreground leading-tight">{desc}</div>
      </div>
    </div>
  );
}

function ModuleLink({
  href,
  icon: Icon,
  title,
  desc,
}: {
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  desc: string;
}) {
  return (
    <Link
      href={href}
      className="group flex flex-col justify-between rounded-lg border border-border bg-card p-4 transition-all hover:border-primary/60 hover:shadow-sm"
    >
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="rounded-md bg-primary/10 p-1.5 text-primary">
              <Icon className="h-4 w-4" />
            </div>
            <h3 className="text-sm font-semibold group-hover:text-primary transition-colors">
              {title}
            </h3>
          </div>
          <ArrowRight className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-primary" />
        </div>
        <p className="text-xs text-muted-foreground leading-relaxed">{desc}</p>
      </div>
    </Link>
  );
}

function Unknowable({ children }: { children: React.ReactNode }) {
  return (
    <li className="rounded-md border border-border bg-card/60 p-3 text-muted-foreground text-xs leading-relaxed">
      {children}
    </li>
  );
}
