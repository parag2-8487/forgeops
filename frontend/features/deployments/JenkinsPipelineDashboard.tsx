// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  Ban,
  Box,
  CheckCircle2,
  Clock,
  ExternalLink,
  GitBranch,
  GitCommit,
  Loader2,
  Play,
  RotateCcw,
  Server,
  XCircle,
} from "lucide-react";
import { AutonomousLogConsole } from "./AutonomousLogConsole";
import { useAutonomousDeployStream, type AutonomousLogEntry } from "./useAutonomousDeployStream";

export type DeploymentStrategy =
  "docker_github_vercel" | "docker_github" | "github_only" | "vercel_only";

export type StageStatus =
  | "pending"
  | "waiting"
  | "running"
  | "cancelling"
  | "cancelled"
  | "succeeded"
  | "failed"
  | "skipped"
  | "rolled_back";

export type AutonomousRunStatus =
  "pending" | "running" | "cancelling" | "cancelled" | "succeeded" | "failed" | "rolled_back";

export interface StagePublicResponse {
  id: string;
  run_id: string;
  stage_name: string;
  gate_id?: string | null;
  position: number;
  status: StageStatus;
  progress_pct: number;
  started_at?: string | null;
  completed_at?: string | null;
  error_message?: string | null;
  stage_metadata?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  created_at?: string | null;
}

export interface AutonomousRunPublicResponse {
  id: string;
  project_id: string;
  parent_run_id?: string | null;
  attempt_number: number;
  status: AutonomousRunStatus;
  strategy: DeploymentStrategy;
  configuration?: Record<string, unknown>;
  progress_pct: number;
  current_stage?: string | null;
  error_summary?: string | null;
  primary_error?: Record<string, unknown> | null;
  compensation_error?: Record<string, unknown> | null;
  dispatch_status?: string;
  dispatch_requested_at?: string | null;
  idempotency_key?: string | null;
  created_by?: string;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  stages: StagePublicResponse[];
  agent_connected?: boolean | null;
}

export type VerificationTargetStatus = "Verified" | "Not Applicable" | "Failed" | "Pending";

export interface JenkinsPipelineDashboardProps {
  projectId: string;
  runId?: string;
  projectName?: string;
  initialRun?: AutonomousRunPublicResponse;
  run?: AutonomousRunPublicResponse;
  initialLogs?: AutonomousLogEntry[];
  logs?: AutonomousLogEntry[];
  onRefresh?: () => void;
  onRunChange?: (run: AutonomousRunPublicResponse) => void;
}

export function formatElapsedTime(
  startedAt?: string | null,
  completedAt?: string | null,
  nowMs?: number,
): string {
  if (!startedAt) return "--";
  const start = new Date(startedAt).getTime();
  if (isNaN(start)) return "--";
  const end = completedAt ? new Date(completedAt).getTime() : (nowMs ?? Date.now());
  const elapsedMs = Math.max(0, end - start);
  const seconds = Math.floor(elapsedMs / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remSeconds = seconds % 60;
  return `${minutes}m ${remSeconds}s`;
}

export function getStrategyLabel(strategy: DeploymentStrategy | string): string {
  switch (strategy) {
    case "docker_github_vercel":
      return "Docker + GitHub + Vercel";
    case "docker_github":
      return "Docker + GitHub";
    case "github_only":
      return "GitHub Only";
    case "vercel_only":
      return "Vercel Only";
    default:
      return strategy;
  }
}

export function getStageDisplayInfo(stage: StagePublicResponse): {
  title: string;
  subtitle: string;
  badge: string;
  isGate: boolean;
} {
  const normName = stage.stage_name.toLowerCase();
  const gateId = stage.gate_id?.toUpperCase();

  if (gateId === "G1" || normName.includes("g1") || normName.includes("blueprint")) {
    return {
      title: "G1 — Blueprint Validation",
      subtitle: "Project & Config Spec",
      badge: "G1",
      isGate: true,
    };
  }
  if (gateId === "G2" || normName.includes("g2") || normName.includes("artifact")) {
    return {
      title: "G2 — Artifact Validation",
      subtitle: "Dockerfile & Compose Generation",
      badge: "G2",
      isGate: true,
    };
  }
  if (gateId === "G3" || normName.includes("g3") || normName.includes("consistency")) {
    return {
      title: "G3 — Pre-Execution Consistency",
      subtitle: "Ports, Branch & Config Verification",
      badge: "G3",
      isGate: true,
    };
  }
  if (
    gateId === "G4" ||
    normName.includes("g4") ||
    normName === "docker_build" ||
    (normName.includes("build") && !normName.includes("blueprint"))
  ) {
    return {
      title: "G4 — Build & Compile",
      subtitle: "Container Image Build & Validation",
      badge: "G4",
      isGate: true,
    };
  }
  if (
    gateId === "G5" ||
    normName.includes("g5") ||
    normName === "docker_apply" ||
    normName === "docker_deploy" ||
    normName.includes("apply")
  ) {
    return {
      title: "G5 — Apply & Startup",
      subtitle: "Container Startup & Port Binding",
      badge: "G5",
      isGate: true,
    };
  }
  if (
    gateId === "G6" ||
    normName.includes("g6") ||
    normName === "docker_workload" ||
    normName.includes("workload")
  ) {
    return {
      title: "G6 — Workload Verification",
      subtitle: "HTTP Health Probe & Verification",
      badge: "G6",
      isGate: true,
    };
  }
  if (normName === "github_release" || normName.includes("github")) {
    return {
      title: "GitHub Release",
      subtitle: "Code Sync & Release",
      badge: "Git",
      isGate: false,
    };
  }
  if (normName === "vercel_deploy" || normName.includes("vercel")) {
    return {
      title: "Vercel Deploy",
      subtitle: "Cloud Edge Deployment",
      badge: "Cloud",
      isGate: false,
    };
  }
  if (gateId === "G7" || normName.includes("g7") || normName.includes("verification")) {
    return {
      title: "G7 — Final Deployment Sign-off",
      subtitle: "Multi-Target E2E Probe & Sign-off",
      badge: "G7",
      isGate: true,
    };
  }

  return {
    title: stage.stage_name,
    subtitle: stage.gate_id ? `Gate ${stage.gate_id}` : "Operational Stage",
    badge: stage.gate_id ?? "Step",
    isGate: Boolean(stage.gate_id),
  };
}

export function getTargetVerificationStatus(
  target: "docker" | "github" | "vercel",
  strategy: DeploymentStrategy,
  stages: StagePublicResponse[],
): VerificationTargetStatus {
  const hasDocker = strategy === "docker_github_vercel" || strategy === "docker_github";
  const hasGithub =
    strategy === "docker_github_vercel" ||
    strategy === "docker_github" ||
    strategy === "github_only";
  const hasVercel = strategy === "docker_github_vercel" || strategy === "vercel_only";

  const isApplicable =
    (target === "docker" && hasDocker) ||
    (target === "github" && hasGithub) ||
    (target === "vercel" && hasVercel);

  if (!isApplicable) {
    return "Not Applicable";
  }

  const g7Stage = stages.find(
    (s) =>
      s.gate_id === "G7" ||
      s.stage_name === "G7_verification" ||
      s.stage_name.toLowerCase().includes("g7"),
  );

  const rawMetadata = g7Stage?.stage_metadata || g7Stage?.metadata || {};
  const metadata = rawMetadata as Record<string, unknown>;
  const targetResults =
    (metadata.target_results as Record<string, string> | undefined) ||
    (metadata.targets as Record<string, string> | undefined) ||
    ((metadata.details as Record<string, unknown> | undefined)?.target_results as
      Record<string, string> | undefined);

  if (targetResults && target in targetResults) {
    const raw = String(targetResults[target]).toLowerCase();
    if (raw === "verified" || raw === "passed" || raw === "succeeded") return "Verified";
    if (raw === "failed" || raw === "error") return "Failed";
    if (raw === "not_applicable" || raw === "skipped") return "Not Applicable";
  }

  if (g7Stage?.status === "succeeded") {
    return "Verified";
  }

  if (g7Stage?.status === "failed") {
    if (target === "docker") {
      const dockerFailed = stages.some(
        (s) =>
          (s.stage_name.includes("G4") ||
            s.stage_name.includes("G5") ||
            s.stage_name.includes("G6") ||
            s.stage_name.toLowerCase().includes("docker")) &&
          s.status === "failed",
      );
      if (dockerFailed) return "Failed";
    }
    if (target === "github") {
      const ghFailed = stages.some(
        (s) => s.stage_name.toLowerCase().includes("github") && s.status === "failed",
      );
      if (ghFailed) return "Failed";
    }
    if (target === "vercel") {
      const vercelFailed = stages.some(
        (s) => s.stage_name.toLowerCase().includes("vercel") && s.status === "failed",
      );
      if (vercelFailed) return "Failed";
    }
    return "Failed";
  }

  return "Pending";
}

export function getStageStatusStyles(status: StageStatus): {
  cardClasses: string;
  badgeClasses: string;
  icon: React.ReactNode;
  label: string;
} {
  switch (status) {
    case "succeeded":
      return {
        cardClasses:
          "border-emerald-500/60 bg-emerald-500/10 text-emerald-950 dark:text-emerald-100 hover:bg-emerald-500/20",
        badgeClasses: "bg-emerald-500 text-white",
        icon: <CheckCircle2 className="size-3.5 text-emerald-600 dark:text-emerald-400 shrink-0" />,
        label: "succeeded",
      };
    case "failed":
    case "rolled_back":
      return {
        cardClasses:
          "border-rose-500/60 bg-rose-500/10 text-rose-950 dark:text-rose-100 hover:bg-rose-500/20",
        badgeClasses: "bg-rose-500 text-white",
        icon: <XCircle className="size-3.5 text-rose-600 dark:text-rose-400 shrink-0" />,
        label: status,
      };
    case "running":
    case "cancelling":
      return {
        cardClasses:
          "border-blue-500/60 bg-blue-500/10 text-blue-950 dark:text-blue-100 animate-pulse hover:bg-blue-500/20",
        badgeClasses: "bg-blue-500 text-white",
        icon: (
          <Loader2 className="size-3.5 text-blue-600 dark:text-blue-400 animate-spin shrink-0" />
        ),
        label: status,
      };
    case "waiting":
      return {
        cardClasses:
          "border-amber-500/60 bg-amber-500/10 text-amber-950 dark:text-amber-100 animate-pulse hover:bg-amber-500/20",
        badgeClasses: "bg-amber-500 text-white",
        icon: <Clock className="size-3.5 text-amber-600 dark:text-amber-400 shrink-0" />,
        label: "waiting",
      };
    case "cancelled":
      return {
        cardClasses:
          "border-amber-500/50 bg-amber-500/10 text-amber-950 dark:text-amber-100 hover:bg-amber-500/15",
        badgeClasses: "bg-amber-500 text-white",
        icon: <Ban className="size-3.5 text-amber-600 dark:text-amber-400 shrink-0" />,
        label: "cancelled",
      };
    case "skipped":
      return {
        cardClasses:
          "border-2 border-dashed border-muted-foreground/40 bg-muted/15 text-muted-foreground",
        badgeClasses: "bg-muted text-muted-foreground",
        icon: <Ban className="size-3.5 text-muted-foreground shrink-0" />,
        label: "skipped",
      };
    case "pending":
    default:
      return {
        cardClasses: "border-border bg-muted/25 text-muted-foreground hover:bg-muted/40",
        badgeClasses: "bg-muted text-muted-foreground",
        icon: <Clock className="size-3.5 text-muted-foreground shrink-0" />,
        label: "pending",
      };
  }
}

export interface StageCardProps {
  stage: StagePublicResponse;
  isSelected: boolean;
  onSelect: () => void;
  nowMs: number;
  badgeOverride?: string;
  isSubOperation?: boolean;
}

export function StageCard({
  stage,
  isSelected,
  onSelect,
  nowMs,
  badgeOverride,
  isSubOperation = false,
}: StageCardProps) {
  const info = getStageDisplayInfo(stage);
  const statusInfo = getStageStatusStyles(stage.status);
  const duration = formatElapsedTime(stage.started_at, stage.completed_at, nowMs);

  return (
    <button
      type="button"
      data-testid={`stage-node-${stage.stage_name}`}
      onClick={onSelect}
      aria-label={`Select stage ${info.title}`}
      aria-pressed={isSelected}
      className={cn(
        "group relative flex flex-col justify-between w-full text-left rounded-lg border p-2.5 transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-primary",
        isSubOperation ? "min-h-[64px]" : "min-h-[88px]",
        isSelected
          ? "ring-2 ring-primary ring-offset-2 ring-offset-background shadow-md border-primary"
          : "hover:border-foreground/30 hover:shadow-xs",
        statusInfo.cardClasses,
      )}
    >
      {/* Top row: badge + duration */}
      <div className="flex items-center justify-between gap-1 w-full text-[11px]">
        <span className="font-mono font-semibold px-1.5 py-0.5 rounded bg-background/70 border border-border/40 text-foreground text-[10px]">
          {badgeOverride ?? info.badge}
        </span>
        <span className="font-mono text-[11px] text-muted-foreground flex items-center gap-1">
          <Clock className="size-3" />
          {duration}
        </span>
      </div>

      {/* Title & subtitle */}
      <div className="my-1.5 w-full">
        <div className="font-semibold text-xs text-foreground truncate w-full" title={info.title}>
          {isSubOperation ? info.title : (info.title.split(" — ")[1] ?? info.title)}
        </div>
        {!isSubOperation && (
          <div className="text-[10px] text-muted-foreground truncate w-full">{info.subtitle}</div>
        )}
      </div>

      {/* Bottom row: Status icon + label + progress */}
      <div className="flex items-center justify-between gap-1 w-full pt-1 border-t border-border/30 text-[11px]">
        <div className="flex items-center gap-1.5 font-medium truncate">
          {statusInfo.icon}
          <span className="capitalize text-foreground font-mono">{stage.status}</span>
        </div>
        {stage.status === "running" && stage.progress_pct > 0 && (
          <span className="font-mono text-[10px] font-bold text-blue-600 dark:text-blue-400">
            {stage.progress_pct}%
          </span>
        )}
      </div>
    </button>
  );
}

export interface CanonicalGateColumn {
  key: string;
  gateId: string;
  title: string;
  shortTitle: string;
  subtitle: string;
  badge: string;
  primaryStage?: StagePublicResponse;
  subOperations: StagePublicResponse[];
}

export function JenkinsPipelineDashboard({
  projectId,
  runId,
  projectName = "Project",
  initialRun,
  run: propRun,
  initialLogs,
  logs: propLogs,
  onRefresh,
  onRunChange,
}: JenkinsPipelineDashboardProps) {
  const router = useRouter();

  // Internal state for run tracking
  const [localRun, setLocalRun] = useState<AutonomousRunPublicResponse | null>(
    propRun ?? initialRun ?? null,
  );
  const [selectedStageId, setSelectedStageId] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());
  const [actionError, setActionError] = useState<string | null>(null);

  // Button loading states
  const [isStarting, setIsStarting] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);

  const targetRunId = localRun?.id ?? propRun?.id ?? runId;

  // React Query hook fallback if run wasn't passed directly
  const runQuery = useQuery({
    queryKey: ["projects", projectId, "autonomous-deploy", targetRunId],
    queryFn: () =>
      api.get<AutonomousRunPublicResponse>(
        `/projects/${projectId}/autonomous-deploy/${targetRunId}`,
      ),
    enabled: Boolean(!propRun && projectId && targetRunId),
    initialData: initialRun,
  });

  // Live real-time stream hook
  const stream = useAutonomousDeployStream({
    projectId,
    runId: targetRunId ?? "",
    initialRun: propRun ?? initialRun ?? undefined,
    initialLogs: propLogs ?? initialLogs ?? undefined,
    enabled: Boolean(projectId && targetRunId),
  });

  // Notify parent on streaming run updates
  useEffect(() => {
    if (stream.run) {
      onRunChange?.(stream.run);
    }
  }, [stream.run, onRunChange]);

  const activeRun = stream.run ?? propRun ?? localRun ?? runQuery.data ?? initialRun ?? null;

  // Live timer tick for running executions
  useEffect(() => {
    if (activeRun?.status === "running" || activeRun?.status === "cancelling") {
      const interval = setInterval(() => {
        setNowMs(Date.now());
      }, 1000);
      return () => clearInterval(interval);
    }
  }, [activeRun?.status]);

  const activeRunStages = activeRun?.stages;

  // Stage list sorted by position with live stream priority
  const stages = useMemo(() => {
    if (stream.stages && stream.stages.length > 0) {
      return [...stream.stages].sort((a, b) => a.position - b.position);
    }
    if (!activeRunStages) return [];
    return [...activeRunStages].sort((a, b) => a.position - b.position);
  }, [stream.stages, activeRunStages]);

  const activeLogs = stream.logs.length > 0 ? stream.logs : (propLogs ?? initialLogs ?? []);

  // Determine fallback stage without mutating state in an effect
  const defaultStageId = useMemo(() => {
    if (stages.length === 0) return null;
    const failed = stages.find((s) => s.status === "failed");
    const running = stages.find((s) => s.status === "running" || s.status === "cancelling");
    const g7 = stages.find(
      (s) =>
        s.gate_id === "G7" ||
        s.stage_name === "G7_verification" ||
        s.stage_name.toLowerCase().includes("g7"),
    );
    if (failed) return failed.id;
    if (running) return running.id;
    if (activeRun?.status === "succeeded" && g7) return g7.id;
    return stages[0].id;
  }, [stages, activeRun?.status]);

  const canonicalColumns = useMemo<CanonicalGateColumn[]>(() => {
    const defs = [
      {
        key: "g1",
        gateId: "G1",
        title: "G1 — Blueprint Validation",
        shortTitle: "G1 Blueprint",
        subtitle: "Project & Config Spec",
        badge: "G1",
      },
      {
        key: "g2",
        gateId: "G2",
        title: "G2 — Artifact Validation",
        shortTitle: "G2 Artifacts",
        subtitle: "Dockerfile / Compose",
        badge: "G2",
      },
      {
        key: "g3",
        gateId: "G3",
        title: "G3 — Pre-Execution Consistency",
        shortTitle: "G3 Consistency",
        subtitle: "Ports, Branch & Config",
        badge: "G3",
      },
      {
        key: "g4",
        gateId: "G4",
        title: "G4 — Build & Compile",
        shortTitle: "G4 Build",
        subtitle: "Docker Build",
        badge: "G4",
      },
      {
        key: "g5",
        gateId: "G5",
        title: "G5 — Apply & Startup",
        shortTitle: "G5 Apply",
        subtitle: "Docker Apply",
        badge: "G5",
      },
      {
        key: "g6",
        gateId: "G6",
        title: "G6 — Workload Verification",
        shortTitle: "G6 Workload",
        subtitle: "Health & Strategy Sync",
        badge: "G6",
      },
      {
        key: "g7",
        gateId: "G7",
        title: "G7 — Final Deployment Sign-off",
        shortTitle: "G7 Sign-off",
        subtitle: "Multi-Target Verification",
        badge: "G7",
      },
    ];

    const matchedStageIds = new Set<string>();

    const cols: CanonicalGateColumn[] = defs.map((def) => {
      let primary: StagePublicResponse | undefined;

      if (def.gateId === "G1") {
        primary = stages.find(
          (s) =>
            s.gate_id?.toUpperCase() === "G1" ||
            s.stage_name.toLowerCase().includes("g1") ||
            s.stage_name.toLowerCase().includes("blueprint"),
        );
      } else if (def.gateId === "G2") {
        primary = stages.find(
          (s) =>
            s.gate_id?.toUpperCase() === "G2" ||
            s.stage_name.toLowerCase().includes("g2") ||
            s.stage_name.toLowerCase().includes("artifact"),
        );
      } else if (def.gateId === "G3") {
        primary = stages.find(
          (s) =>
            s.gate_id?.toUpperCase() === "G3" ||
            s.stage_name.toLowerCase().includes("g3") ||
            s.stage_name.toLowerCase().includes("consistency"),
        );
      } else if (def.gateId === "G4") {
        primary = stages.find(
          (s) =>
            s.gate_id?.toUpperCase() === "G4" ||
            s.stage_name.toLowerCase().includes("g4") ||
            s.stage_name.toLowerCase() === "docker_build" ||
            (s.stage_name.toLowerCase().includes("build") &&
              !s.stage_name.toLowerCase().includes("blueprint")),
        );
      } else if (def.gateId === "G5") {
        primary = stages.find(
          (s) =>
            s.gate_id?.toUpperCase() === "G5" ||
            s.stage_name.toLowerCase().includes("g5") ||
            s.stage_name.toLowerCase() === "docker_apply" ||
            s.stage_name.toLowerCase() === "docker_deploy" ||
            s.stage_name.toLowerCase().includes("apply"),
        );
      } else if (def.gateId === "G6") {
        primary = stages.find(
          (s) =>
            s.gate_id?.toUpperCase() === "G6" ||
            s.stage_name.toLowerCase().includes("g6") ||
            s.stage_name.toLowerCase() === "docker_workload" ||
            s.stage_name.toLowerCase().includes("workload"),
        );
      } else if (def.gateId === "G7") {
        primary = stages.find(
          (s) =>
            s.gate_id?.toUpperCase() === "G7" ||
            s.stage_name.toLowerCase().includes("g7") ||
            s.stage_name.toLowerCase().includes("verification"),
        );
      }

      if (primary) matchedStageIds.add(primary.id);

      return {
        ...def,
        primaryStage: primary,
        subOperations: [],
      };
    });

    // Strategy sub-operations under G6
    const g6Col = cols.find((c) => c.gateId === "G6");
    if (g6Col) {
      const subOps = stages.filter((s) => {
        if (matchedStageIds.has(s.id)) return false;
        const norm = s.stage_name.toLowerCase();
        return norm.includes("github") || norm.includes("vercel");
      });
      for (const sub of subOps) {
        matchedStageIds.add(sub.id);
        g6Col.subOperations.push(sub);
      }
    }

    // Remaining unmatched stages
    const unmatched = stages.filter((s) => !matchedStageIds.has(s.id));
    for (const extra of unmatched) {
      const extraInfo = getStageDisplayInfo(extra);
      cols.push({
        key: `custom-${extra.id}`,
        gateId: extra.gate_id ?? "Step",
        title: extraInfo.title,
        shortTitle: extraInfo.title,
        subtitle: extraInfo.subtitle,
        badge: extraInfo.badge,
        primaryStage: extra,
        subOperations: [],
      });
    }

    return cols;
  }, [stages]);

  const currentSelectedStageId =
    selectedStageId && stages.some((s) => s.id === selectedStageId)
      ? selectedStageId
      : defaultStageId;

  if (!activeRun) {
    return (
      <div
        data-testid="pipeline-loading"
        className="flex min-h-[400px] items-center justify-center rounded-lg border border-border p-12 text-sm text-muted-foreground"
      >
        <Loader2 className="mr-2 size-5 animate-spin" />
        Loading autonomous deployment pipeline...
      </div>
    );
  }

  const selectedStage = stages.find((s) => s.id === currentSelectedStageId) || stages[0];
  const selectedInfo = selectedStage ? getStageDisplayInfo(selectedStage) : null;

  // Action state rules
  const isAgentConnected = Boolean(activeRun.agent_connected);
  const isPending = activeRun.status === "pending";
  const isRunning = activeRun.status === "running";
  const isCancellingStatus = activeRun.status === "cancelling";
  const isFailed = activeRun.status === "failed";
  const isRolledBack = activeRun.status === "rolled_back";

  const isStartDisabled = !isPending || !isAgentConnected || isStarting;
  const canCancel = isPending || isRunning;
  const canRetry = isFailed || isRolledBack;

  const startTooltip = !isAgentConnected
    ? "Local agent disconnected. An active paired agent is required to start deployments."
    : !isPending
      ? `Cannot start pipeline in status '${activeRun.status}'.`
      : "Start deployment pipeline";

  const handleStart = async () => {
    if (isStartDisabled) return;
    setIsStarting(true);
    setActionError(null);
    try {
      const updated = await api.post<AutonomousRunPublicResponse>(
        `/projects/${projectId}/autonomous-deploy/${activeRun.id}/start`,
      );
      setLocalRun(updated);
      onRunChange?.(updated);
      onRefresh?.();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to start autonomous deployment");
    } finally {
      setIsStarting(false);
    }
  };

  const handleCancel = async () => {
    if (!canCancel || isCancelling || isCancellingStatus) return;
    setIsCancelling(true);
    setActionError(null);
    try {
      const updated = await api.post<AutonomousRunPublicResponse>(
        `/projects/${projectId}/autonomous-deploy/${activeRun.id}/cancel`,
      );
      setLocalRun(updated);
      onRunChange?.(updated);
      onRefresh?.();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to cancel deployment run");
    } finally {
      setIsCancelling(false);
    }
  };

  const handleRetry = async () => {
    if (!canRetry || isRetrying) return;
    setIsRetrying(true);
    setActionError(null);
    try {
      const newAttempt = await api.post<AutonomousRunPublicResponse>(
        `/projects/${projectId}/autonomous-deploy/${activeRun.id}/retry`,
      );
      router.push(`/projects/${projectId}/autonomous-deploy/${newAttempt.id}`);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to retry deployment run");
      setIsRetrying(false);
    }
  };

  // Extract metadata details for active stage inspector
  const stageMeta =
    ((selectedStage?.stage_metadata || selectedStage?.metadata || {}) as Record<string, unknown>) ||
    {};
  const metaDetails = (stageMeta.details as Record<string, unknown> | undefined) || {};

  const containerId =
    (stageMeta.container_id as string | undefined) ||
    (metaDetails.container_id as string | undefined);
  const commitSha =
    (stageMeta.commit_sha as string | undefined) || (metaDetails.commit_sha as string | undefined);
  const deploymentUrl =
    (stageMeta.deployment_url as string | undefined) ||
    (metaDetails.deployment_url as string | undefined);
  const prNumber =
    (stageMeta.pr_number as number | undefined) || (metaDetails.pr_number as number | undefined);
  const prUrl =
    (stageMeta.pr_url as string | undefined) || (metaDetails.pr_url as string | undefined);
  const prState =
    (stageMeta.pr_state as string | undefined) ||
    (metaDetails.pr_state as string | undefined) ||
    "open";
  const prMerged =
    (stageMeta.pr_merged as boolean | undefined) ??
    (metaDetails.pr_merged as boolean | undefined) ??
    false;
  const sourceBranch =
    (stageMeta.source_branch as string | undefined) ||
    (metaDetails.source_branch as string | undefined);
  const baseBranch =
    (stageMeta.base_branch as string | undefined) ||
    (metaDetails.base_branch as string | undefined) ||
    (stageMeta.target_branch as string | undefined) ||
    (metaDetails.target_branch as string | undefined);
  const isSimulated =
    stageMeta.simulated === true ||
    metaDetails.simulated === true ||
    stageMeta.execution_mode === "simulation" ||
    (activeRun.configuration as Record<string, unknown> | undefined)?.simulation_mode === true ||
    (activeRun.configuration as Record<string, unknown> | undefined)?.dry_run === true;

  // Strategy target statuses for G7 summary
  const dockerTargetStatus = getTargetVerificationStatus("docker", activeRun.strategy, stages);
  const githubTargetStatus = getTargetVerificationStatus("github", activeRun.strategy, stages);
  const vercelTargetStatus = getTargetVerificationStatus("vercel", activeRun.strategy, stages);

  const getTargetBadgeVariant = (
    status: VerificationTargetStatus,
  ): "success" | "outline" | "destructive" | "secondary" => {
    switch (status) {
      case "Verified":
        return "success";
      case "Failed":
        return "destructive";
      case "Not Applicable":
        return "outline";
      case "Pending":
      default:
        return "secondary";
    }
  };

  return (
    <div className="space-y-6 w-full pb-16">
      {/* Header Summary Bar */}
      <div
        data-testid="header-summary-bar"
        className="rounded-xl border border-border bg-card p-6 shadow-sm space-y-4"
      >
        <div className="flex flex-wrap items-center justify-between gap-4">
          {/* Breadcrumb Navigation */}
          <nav
            data-testid="breadcrumb"
            aria-label="Breadcrumb"
            className="flex items-center gap-2 text-sm text-muted-foreground"
          >
            <Link
              href="/projects"
              className="hover:text-foreground underline underline-offset-4 transition-colors"
            >
              Projects
            </Link>
            <span className="text-muted-foreground/60">/</span>
            <Link
              href={`/projects/${projectId}`}
              className="hover:text-foreground underline underline-offset-4 transition-colors"
            >
              {projectName}
            </Link>
            <span className="text-muted-foreground/60">/</span>
            <span className="font-semibold text-foreground">Run #{activeRun.attempt_number}</span>
          </nav>

          {/* Action Controls */}
          <div className="flex flex-wrap items-center gap-2">
            <Button data-testid="btn-back-project" variant="outline" size="sm" asChild>
              <Link href={`/projects/${projectId}`}>
                <ArrowLeft className="mr-1.5 size-4" />
                Back to project
              </Link>
            </Button>

            {canRetry && (
              <Button
                data-testid="btn-retry"
                variant="outline"
                size="sm"
                disabled={isRetrying}
                onClick={handleRetry}
              >
                {isRetrying ? (
                  <Loader2 className="mr-1.5 size-4 animate-spin" />
                ) : (
                  <RotateCcw className="mr-1.5 size-4" />
                )}
                Retry Run
              </Button>
            )}

            {(canCancel || isCancellingStatus) && (
              <Button
                data-testid="btn-cancel"
                variant="destructive"
                size="sm"
                disabled={isCancelling || isCancellingStatus}
                onClick={handleCancel}
              >
                {isCancelling || isCancellingStatus ? (
                  <>
                    <Loader2 className="mr-1.5 size-4 animate-spin" />
                    Cancelling...
                  </>
                ) : (
                  <>
                    <Ban className="mr-1.5 size-4" />
                    Cancel Run
                  </>
                )}
              </Button>
            )}

            {isPending && (
              <Button
                data-testid="btn-start"
                variant="default"
                size="sm"
                disabled={isStartDisabled}
                title={startTooltip}
                onClick={handleStart}
                className="bg-primary text-primary-foreground hover:bg-primary/90"
              >
                {isStarting ? (
                  <Loader2 className="mr-1.5 size-4 animate-spin" />
                ) : (
                  <Play className="mr-1.5 size-4 fill-current" />
                )}
                Start Pipeline
              </Button>
            )}
          </div>
        </div>

        {/* Badges and Timer Row */}
        <div className="flex flex-wrap items-center justify-between gap-3 pt-2 border-t border-border/60">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              data-testid="run-id-badge"
              variant="outline"
              className="font-mono text-xs font-semibold px-2 py-0.5"
            >
              {activeRun.id}
            </Badge>

            <Badge data-testid="strategy-badge" variant="secondary" className="font-medium text-xs">
              {getStrategyLabel(activeRun.strategy)}
            </Badge>

            <Badge
              data-testid="status-badge"
              variant={
                activeRun.status === "succeeded"
                  ? "success"
                  : activeRun.status === "failed" || activeRun.status === "rolled_back"
                    ? "destructive"
                    : activeRun.status === "cancelling" || activeRun.status === "cancelled"
                      ? "warning"
                      : activeRun.status === "running"
                        ? "default"
                        : "secondary"
              }
              className="capitalize text-xs font-medium"
            >
              {activeRun.status}
            </Badge>
          </div>

          <div
            data-testid="elapsed-timer"
            className="flex items-center gap-1.5 text-xs text-muted-foreground font-mono"
          >
            <Clock className="size-3.5" />
            <span>
              Elapsed: {formatElapsedTime(activeRun.started_at, activeRun.completed_at, nowMs)}
            </span>
          </div>
        </div>

        {/* Monotonic Progress Bar */}
        <div className="space-y-1.5">
          <div className="flex justify-between items-center text-xs text-muted-foreground">
            <span>Pipeline Execution Progress</span>
            <span className="font-mono font-medium">{activeRun.progress_pct}%</span>
          </div>
          <div
            data-testid="progress-bar"
            role="progressbar"
            aria-valuenow={activeRun.progress_pct}
            aria-valuemin={0}
            aria-valuemax={100}
            className="h-2 w-full overflow-hidden rounded-full bg-muted"
          >
            <div
              className="h-full bg-primary transition-all duration-300 ease-out"
              style={{ width: `${Math.min(100, Math.max(0, activeRun.progress_pct))}%` }}
            />
          </div>
        </div>

        {/* Agent Pairing Status Banner */}
        <div data-testid="agent-status-banner">
          {isAgentConnected ? (
            <div className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3.5 py-2 text-xs text-emerald-600 dark:text-emerald-400">
              <Server className="size-4 shrink-0" />
              <span>Paired Agent Connected: Ready for local orchestration execution.</span>
            </div>
          ) : (
            <div
              role="alert"
              className="flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3.5 py-2 text-xs text-amber-600 dark:text-amber-400"
            >
              <AlertTriangle className="size-4 shrink-0" />
              <span>
                Local Agent Disconnected: An active paired agent with a recent heartbeat (&le; 30s)
                is required to start deployments.
              </span>
            </div>
          )}
        </div>

        {/* Error notification banner if action fails */}
        {actionError && (
          <div
            role="alert"
            className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3.5 py-2 text-xs text-destructive"
          >
            <AlertCircle className="size-4 shrink-0" />
            <span>{actionError}</span>
          </div>
        )}
      </div>

      {/* Jenkins Pipeline Stage View */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold tracking-tight text-foreground flex items-center gap-2">
              <span className="inline-block size-2 rounded-full bg-primary" />
              Jenkins Pipeline Stage View
            </h3>
            <p className="text-xs text-muted-foreground">
              Autonomous execution matrix across canonical deployment gates G1–G7
            </p>
          </div>
          <span className="text-xs text-muted-foreground font-mono">
            Click any stage node or sub-operation card to inspect verdicts &amp; logs
          </span>
        </div>

        <div
          data-testid="blue-ocean-graph"
          className="rounded-xl border border-border bg-card shadow-sm overflow-hidden"
        >
          <div className="overflow-x-auto">
            <div className="min-w-max">
              {/* Header row: Column Titles & Durations */}
              <div className="flex items-stretch bg-muted/30 border-b border-border/80">
                <div className="w-44 shrink-0 p-3 flex flex-col justify-center">
                  <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Pipeline Stages
                  </span>
                  <span className="text-[11px] text-muted-foreground/80">
                    Execution Attempt #{activeRun.attempt_number}
                  </span>
                </div>

                {canonicalColumns.map((col) => {
                  const colDuration = col.primaryStage
                    ? formatElapsedTime(
                        col.primaryStage.started_at,
                        col.primaryStage.completed_at,
                        nowMs,
                      )
                    : col.subOperations.length > 0 && col.subOperations[0].started_at
                      ? formatElapsedTime(
                          col.subOperations[0].started_at,
                          col.subOperations[0].completed_at,
                          nowMs,
                        )
                      : "--";

                  return (
                    <div
                      key={col.key}
                      className="w-56 shrink-0 p-3 border-l border-border/60 flex flex-col justify-between"
                    >
                      <div className="flex items-center justify-between gap-1.5 mb-1">
                        <span
                          className="font-semibold text-xs text-foreground truncate"
                          title={col.title}
                        >
                          {col.shortTitle}
                        </span>
                        <span className="font-mono text-[11px] text-muted-foreground bg-background/80 px-1.5 py-0.5 rounded border border-border/40">
                          {colDuration}
                        </span>
                      </div>
                      <p className="text-[11px] text-muted-foreground truncate">{col.subtitle}</p>
                    </div>
                  );
                })}
              </div>

              {/* Execution Matrix Row */}
              <div className="flex items-stretch bg-card divide-x divide-border/60">
                {/* Left Run Identity Cell */}
                <div className="w-44 shrink-0 p-3 flex flex-col justify-between bg-muted/15">
                  <div className="space-y-1.5">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-base text-foreground font-mono">
                        #{activeRun.attempt_number}
                      </span>
                      <Badge
                        variant={
                          activeRun.status === "succeeded"
                            ? "success"
                            : activeRun.status === "failed" || activeRun.status === "rolled_back"
                              ? "destructive"
                              : activeRun.status === "cancelling" ||
                                  activeRun.status === "cancelled"
                                ? "warning"
                                : activeRun.status === "running"
                                  ? "default"
                                  : "secondary"
                        }
                        className="capitalize text-[10px] font-semibold px-2 py-0"
                      >
                        {activeRun.status}
                      </Badge>
                    </div>

                    <div className="text-[11px] text-muted-foreground font-mono flex items-center gap-1">
                      <Clock className="size-3" />
                      <span>
                        {formatElapsedTime(activeRun.started_at, activeRun.completed_at, nowMs)}
                      </span>
                    </div>
                  </div>

                  <div className="text-[10px] text-muted-foreground/70 font-mono pt-2">
                    {activeRun.started_at
                      ? new Date(activeRun.started_at).toLocaleTimeString()
                      : "Pending"}
                  </div>
                </div>

                {/* Stage Columns */}
                {canonicalColumns.map((col) => {
                  return (
                    <div
                      key={col.key}
                      className="w-56 shrink-0 p-2.5 flex flex-col gap-2 justify-start"
                    >
                      {/* Primary stage card if present */}
                      {col.primaryStage ? (
                        <StageCard
                          stage={col.primaryStage}
                          isSelected={col.primaryStage.id === currentSelectedStageId}
                          onSelect={() => {
                            if (col.primaryStage) setSelectedStageId(col.primaryStage.id);
                          }}
                          nowMs={nowMs}
                          badgeOverride={col.badge}
                        />
                      ) : col.subOperations.length === 0 ? (
                        <div className="flex flex-col items-center justify-center min-h-[88px] rounded-lg border border-dashed border-border/50 bg-muted/5 p-3 text-center">
                          <span className="text-[11px] font-mono text-muted-foreground/60">--</span>
                          <span className="text-[10px] text-muted-foreground/50">
                            Not in strategy
                          </span>
                        </div>
                      ) : null}

                      {/* Strategy Sub-Operations (Docker, GitHub Release, Vercel Deploy) */}
                      {col.subOperations.length > 0 && (
                        <div className="space-y-1.5 pt-1 border-t border-border/40">
                          <div className="flex items-center justify-between text-[10px] font-medium text-muted-foreground uppercase tracking-wider">
                            <span>Strategy Operations</span>
                            <span className="font-mono text-[9px]">
                              {col.subOperations.length} sub-tasks
                            </span>
                          </div>
                          <div className="space-y-1.5">
                            {col.subOperations.map((sub) => (
                              <StageCard
                                key={sub.id}
                                stage={sub}
                                isSelected={sub.id === currentSelectedStageId}
                                onSelect={() => setSelectedStageId(sub.id)}
                                nowMs={nowMs}
                                isSubOperation={true}
                              />
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Active Stage Details Card */}
      {selectedStage && selectedInfo && (
        <div
          data-testid="stage-details-card"
          className="rounded-xl border border-border bg-card p-6 shadow-sm space-y-6"
        >
          {/* Card Title & Verdict */}
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 pb-4">
            <div className="space-y-1">
              <div className="flex items-center gap-2.5">
                <Badge variant="outline" className="font-mono text-xs">
                  {selectedInfo.badge}
                </Badge>
                <h3 className="text-lg font-semibold text-foreground">{selectedInfo.title}</h3>
              </div>
              <p className="text-xs text-muted-foreground">{selectedInfo.subtitle}</p>
            </div>

            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">Gate Verdict:</span>
              <Badge
                data-testid="gate-verdict"
                variant={
                  selectedStage.status === "succeeded"
                    ? "success"
                    : selectedStage.status === "failed" || selectedStage.status === "rolled_back"
                      ? "destructive"
                      : selectedStage.status === "cancelled" ||
                          selectedStage.status === "cancelling"
                        ? "warning"
                        : selectedStage.status === "running"
                          ? "default"
                          : "secondary"
                }
                className="capitalize text-xs font-semibold px-2.5 py-0.5"
              >
                {selectedStage.status === "succeeded"
                  ? "Passed"
                  : selectedStage.status === "failed"
                    ? "Failed"
                    : selectedStage.status}
              </Badge>
            </div>
          </div>

          {/* Timestamps and Elapsed Duration */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 text-xs">
            <div className="rounded-lg border border-border bg-background p-3 space-y-1">
              <span className="text-muted-foreground">Started At</span>
              <p className="font-mono font-medium text-foreground">
                {selectedStage.started_at
                  ? new Date(selectedStage.started_at).toLocaleString()
                  : "--"}
              </p>
            </div>

            <div className="rounded-lg border border-border bg-background p-3 space-y-1">
              <span className="text-muted-foreground">Completed At</span>
              <p className="font-mono font-medium text-foreground">
                {selectedStage.completed_at
                  ? new Date(selectedStage.completed_at).toLocaleString()
                  : "--"}
              </p>
            </div>

            <div className="rounded-lg border border-border bg-background p-3 space-y-1">
              <span className="text-muted-foreground">Elapsed Time</span>
              <p data-testid="stage-elapsed-time" className="font-mono font-medium text-foreground">
                {formatElapsedTime(selectedStage.started_at, selectedStage.completed_at, nowMs)}
              </p>
            </div>
          </div>

          {/* Status sentence / Message */}
          <div className="space-y-1.5 text-xs">
            <span className="text-muted-foreground font-medium">Status Summary</span>
            <div className="rounded-lg border border-border bg-background p-3.5">
              {selectedStage.error_message ? (
                <div className="text-destructive font-medium flex items-center gap-2">
                  <AlertCircle className="size-4 shrink-0" />
                  <span>{selectedStage.error_message}</span>
                </div>
              ) : (
                <p className="text-foreground">
                  {typeof stageMeta.message === "string" && stageMeta.message
                    ? stageMeta.message
                    : typeof metaDetails.message === "string" && metaDetails.message
                      ? metaDetails.message
                      : selectedStage.status === "succeeded"
                        ? `Stage ${selectedInfo.title} completed successfully.`
                        : selectedStage.status === "running"
                          ? `Stage ${selectedInfo.title} is currently executing...`
                          : selectedStage.status === "pending"
                            ? `Stage ${selectedInfo.title} is queued.`
                            : `Stage status: ${selectedStage.status}.`}
                </p>
              )}
            </div>
          </div>

          {/* Contextual Operational Metadata */}
          {(containerId || commitSha || deploymentUrl || prNumber || prUrl || isSimulated) && (
            <div className="space-y-2 border-t border-border/60 pt-4">
              <div className="flex items-center justify-between">
                <h4 className="text-xs font-semibold text-foreground uppercase tracking-wider">
                  Operational Artifacts
                </h4>
                {isSimulated && (
                  <Badge
                    data-testid="simulated-badge"
                    variant="outline"
                    className="text-[10px] uppercase font-bold border-amber-500/40 text-amber-600 dark:text-amber-400"
                  >
                    Simulated / Dry Run
                  </Badge>
                )}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 text-xs">
                {(prNumber || prUrl) && (
                  <div
                    data-testid="github-pr-card"
                    className="rounded-lg border border-border bg-background p-3.5 space-y-2 sm:col-span-2 lg:col-span-3"
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <GitBranch className="size-4 text-primary" />
                        <span className="font-semibold text-sm">Pull Request #{prNumber}</span>
                        <Badge
                          data-testid="pr-status-badge"
                          variant={
                            prMerged
                              ? "default"
                              : prState === "closed"
                                ? "destructive"
                                : "secondary"
                          }
                          className="text-[10px] uppercase font-bold"
                        >
                          {prMerged ? "Merged" : prState}
                        </Badge>
                      </div>
                      {prUrl && (
                        <a
                          data-testid="github-pr-url"
                          href={prUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-xs text-primary underline font-medium hover:opacity-80"
                        >
                          View PR on GitHub <ExternalLink className="size-3" />
                        </a>
                      )}
                    </div>
                    <div className="flex flex-wrap items-center gap-3 pt-1 text-xs">
                      {sourceBranch && baseBranch && (
                        <div
                          data-testid="branch-flow-pill"
                          className="inline-flex items-center gap-1.5 font-mono text-[11px] bg-muted px-2.5 py-1 rounded-full border border-border"
                        >
                          <span>{sourceBranch}</span>
                          <span className="text-muted-foreground">&rarr;</span>
                          <span className="font-bold">{baseBranch}</span>
                        </div>
                      )}
                      {commitSha && (
                        <div className="text-muted-foreground font-mono text-[11px]">
                          Commit:{" "}
                          <span data-testid="pr-commit-sha" className="text-foreground">
                            {commitSha.slice(0, 8)}
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {containerId && (
                  <div className="rounded-lg border border-border bg-background p-3 space-y-1">
                    <span className="text-muted-foreground flex items-center gap-1.5">
                      <Box className="size-3.5" /> Container ID
                    </span>
                    <span
                      data-testid="container-id"
                      className="font-mono font-medium block truncate"
                    >
                      {containerId}
                    </span>
                  </div>
                )}

                {commitSha && !prNumber && !prUrl && (
                  <div className="rounded-lg border border-border bg-background p-3 space-y-1">
                    <span className="text-muted-foreground flex items-center gap-1.5">
                      <GitCommit className="size-3.5" /> Commit SHA
                    </span>
                    <span data-testid="commit-sha" className="font-mono font-medium block truncate">
                      {commitSha}
                    </span>
                  </div>
                )}

                {isSimulated &&
                  !commitSha &&
                  !prNumber &&
                  (selectedStage?.stage_name === "github_release" ||
                    selectedStage?.stage_name?.includes("github")) && (
                    <div className="rounded-lg border border-border bg-background p-3 space-y-1">
                      <span className="text-muted-foreground flex items-center gap-1.5">
                        <GitCommit className="size-3.5" /> Commit Status
                      </span>
                      <span
                        data-testid="simulated-commit"
                        className="font-mono text-amber-600 dark:text-amber-400 block truncate"
                      >
                        (Simulated / Dry Run)
                      </span>
                    </div>
                  )}

                {isSimulated &&
                  !containerId &&
                  (selectedStage?.stage_name === "apply_startup" ||
                    selectedStage?.gate_id === "G5") && (
                    <div className="rounded-lg border border-border bg-background p-3 space-y-1">
                      <span className="text-muted-foreground flex items-center gap-1.5">
                        <Box className="size-3.5" /> Container
                      </span>
                      <span
                        data-testid="simulated-container"
                        className="font-mono text-amber-600 dark:text-amber-400 block truncate"
                      >
                        (Simulated / Dry Run)
                      </span>
                    </div>
                  )}

                {deploymentUrl && (
                  <div className="rounded-lg border border-border bg-background p-3 space-y-1 sm:col-span-2 lg:col-span-1">
                    <span className="text-muted-foreground flex items-center gap-1.5">
                      <ExternalLink className="size-3.5" /> Deployment URL
                    </span>
                    <a
                      data-testid="deployment-url"
                      href={deploymentUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-mono text-primary underline truncate block"
                    >
                      {deploymentUrl}
                    </a>
                  </div>
                )}

                {isSimulated &&
                  !deploymentUrl &&
                  (selectedStage?.stage_name === "vercel_deploy" ||
                    selectedStage?.stage_name?.includes("vercel")) && (
                    <div className="rounded-lg border border-border bg-background p-3 space-y-1 sm:col-span-2 lg:col-span-1">
                      <span className="text-muted-foreground flex items-center gap-1.5">
                        <ExternalLink className="size-3.5" /> Deployment
                      </span>
                      <span
                        data-testid="simulated-deployment"
                        className="font-mono text-amber-600 dark:text-amber-400 block truncate"
                      >
                        (Simulated / Dry Run)
                      </span>
                    </div>
                  )}
              </div>
            </div>
          )}

          {/* Strategy-Aware G7 Verification Summary */}
          <div
            data-testid="g7-verification-summary"
            className="space-y-3 border-t border-border/60 pt-4"
          >
            <div className="flex items-center justify-between">
              <h4 className="text-xs font-semibold text-foreground uppercase tracking-wider">
                Multi-Target Verification Breakdown
              </h4>
              <span className="text-[11px] text-muted-foreground">
                Strategy: {getStrategyLabel(activeRun.strategy)}
              </span>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              {/* Docker target */}
              <div className="flex items-center justify-between rounded-lg border border-border bg-background p-3">
                <span className="text-xs font-medium text-foreground flex items-center gap-2">
                  <Box className="size-4 text-muted-foreground" /> Docker
                </span>
                <Badge
                  data-testid="g7-target-docker"
                  variant={getTargetBadgeVariant(dockerTargetStatus)}
                  className="text-xs font-semibold"
                >
                  {dockerTargetStatus}
                </Badge>
              </div>

              {/* GitHub target */}
              <div className="flex items-center justify-between rounded-lg border border-border bg-background p-3">
                <span className="text-xs font-medium text-foreground flex items-center gap-2">
                  <GitBranch className="size-4 text-muted-foreground" /> GitHub
                </span>
                <Badge
                  data-testid="g7-target-github"
                  variant={getTargetBadgeVariant(githubTargetStatus)}
                  className="text-xs font-semibold"
                >
                  {githubTargetStatus}
                </Badge>
              </div>

              {/* Vercel target */}
              <div className="flex items-center justify-between rounded-lg border border-border bg-background p-3">
                <span className="text-xs font-medium text-foreground flex items-center gap-2">
                  <ExternalLink className="size-4 text-muted-foreground" /> Vercel
                </span>
                <Badge
                  data-testid="g7-target-vercel"
                  variant={getTargetBadgeVariant(vercelTargetStatus)}
                  className="text-xs font-semibold"
                >
                  {vercelTargetStatus}
                </Badge>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Live Autonomous Log Terminal Console */}
      <div className="space-y-2">
        <AutonomousLogConsole
          logs={activeLogs}
          stages={stages}
          activeStageName={selectedStage?.stage_name}
          onSelectStage={(stageName) => {
            const found = stages.find((s) => s.stage_name === stageName);
            if (found) setSelectedStageId(found.id);
          }}
        />
      </div>
    </div>
  );
}
