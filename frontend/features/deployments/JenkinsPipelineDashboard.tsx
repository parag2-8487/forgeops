// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  Ban,
  Box,
  Check,
  CheckCircle2,
  Clock,
  Copy,
  ExternalLink,
  GitBranch,
  GitCommit,
  Info,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
  Server,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Wifi,
  WifiOff,
  XCircle,
} from "lucide-react";
import { AutonomousLogConsole } from "./AutonomousLogConsole";
import { useAutonomousDeployStream, type AutonomousLogEntry } from "./useAutonomousDeployStream";

export type DeploymentStrategy =
  | "docker_only"
  | "docker_github_vercel"
  | "docker_github"
  | "github_only"
  | "vercel_only";

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
  | "pending"
  | "running"
  | "cancelling"
  | "cancelled"
  | "succeeded"
  | "failed"
  | "rolled_back";

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

export interface ReadinessCheckpointPublicResponse {
  id: string;
  run_id: string;
  checkpoint_type: string;
  assessment_version: number | string;
  status: string;
  score: number | null;
  max_score: number;
  score_delta?: number | null;
  selected_strategy: string;
  detected_stack?: string | null;
  snapshot_identity?: Record<string, unknown> | null;
  dimension_scores?: Record<string, number> | null;
  dimension_explanations?: Record<string, string> | null;
  blockers?: string[] | null;
  warnings?: string[] | null;
  artifacts_detected?: string[] | null;
  resolved_blockers?: string[] | null;
  new_blockers?: string[] | null;
  explanation?: string | null;
  assessment_error?: string | null;
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
  initial_readiness?: ReadinessCheckpointPublicResponse | null;
  post_generation_readiness?: ReadinessCheckpointPublicResponse | null;
  readiness_checkpoints?: ReadinessCheckpointPublicResponse[];
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
  let end: number;
  if (completedAt) {
    const parsedEnd = new Date(completedAt).getTime();
    end = isNaN(parsedEnd) ? (nowMs ?? Date.now()) : parsedEnd;
  } else {
    end = nowMs ?? Date.now();
  }
  const elapsedMs = Math.max(0, end - start);
  if (completedAt && elapsedMs < 1000) {
    return "<1s";
  }
  const seconds = Math.floor(elapsedMs / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remSeconds = seconds % 60;
  return `${minutes}m ${remSeconds}s`;
}

export function getStrategyLabel(strategy: DeploymentStrategy | string): string {
  switch (strategy) {
    case "docker_only":
      return "Docker Only (Local)";
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
  strategy: DeploymentStrategy | string | undefined,
  stages: StagePublicResponse[],
): VerificationTargetStatus {
  if (!strategy) return "Not Applicable";
  const hasDocker =
    strategy === "docker_only" ||
    strategy === "docker_github_vercel" ||
    strategy === "docker_github";
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
          "border-emerald-500/70 bg-emerald-500/10 text-emerald-950 dark:text-emerald-100 hover:bg-emerald-500/15 focus-visible:ring-emerald-500",
        badgeClasses: "bg-emerald-500 text-white",
        icon: <CheckCircle2 className="size-3.5 text-emerald-600 dark:text-emerald-400 shrink-0" />,
        label: "succeeded",
      };
    case "failed":
    case "rolled_back":
      return {
        cardClasses:
          "border-rose-500/70 bg-rose-500/10 text-rose-950 dark:text-rose-100 hover:bg-rose-500/15 focus-visible:ring-rose-500",
        badgeClasses: "bg-rose-500 text-white",
        icon: <XCircle className="size-3.5 text-rose-600 dark:text-rose-400 shrink-0" />,
        label: status,
      };
    case "running":
    case "cancelling":
      return {
        cardClasses:
          "border-blue-500/80 bg-blue-500/10 text-blue-950 dark:text-blue-100 ring-1 ring-blue-500/40 hover:bg-blue-500/15 focus-visible:ring-blue-500",
        badgeClasses: "bg-blue-500 text-white",
        icon: (
          <Loader2 className="size-3.5 text-blue-600 dark:text-blue-400 animate-spin shrink-0" />
        ),
        label: status,
      };
    case "waiting":
      return {
        cardClasses:
          "border-amber-500/70 bg-amber-500/10 text-amber-950 dark:text-amber-100 hover:bg-amber-500/15 focus-visible:ring-amber-500",
        badgeClasses: "bg-amber-500 text-white",
        icon: <Clock className="size-3.5 text-amber-600 dark:text-amber-400 shrink-0" />,
        label: "waiting",
      };
    case "cancelled":
      return {
        cardClasses:
          "border-amber-500/60 bg-amber-500/10 text-amber-950 dark:text-amber-100 hover:bg-amber-500/15 focus-visible:ring-amber-500",
        badgeClasses: "bg-amber-500 text-white",
        icon: <Ban className="size-3.5 text-amber-600 dark:text-amber-400 shrink-0" />,
        label: "cancelled",
      };
    case "skipped":
      return {
        cardClasses:
          "border-2 border-dashed border-muted-foreground/40 bg-muted/15 text-muted-foreground hover:bg-muted/20 focus-visible:ring-muted-foreground",
        badgeClasses: "bg-muted text-muted-foreground",
        icon: <Ban className="size-3.5 text-muted-foreground shrink-0" />,
        label: "skipped",
      };
    case "pending":
    default:
      return {
        cardClasses:
          "border-border bg-muted/20 text-muted-foreground hover:bg-muted/30 focus-visible:ring-primary",
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
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      aria-label={`Select stage ${info.title}`}
      aria-pressed={isSelected}
      className={cn(
        "group relative flex flex-col justify-between w-full text-left rounded-lg border p-2.5 transition-all outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        isSubOperation ? "min-h-[64px]" : "min-h-[88px]",
        isSelected
          ? "ring-2 ring-primary ring-offset-2 ring-offset-background shadow-md border-primary"
          : "hover:border-foreground/30 hover:shadow-xs",
        statusInfo.cardClasses,
      )}
    >
      {/* Top row: badge + duration */}
      <div className="flex items-center justify-between gap-1 w-full text-[11px]">
        <span className="font-mono font-semibold px-1.5 py-0.5 rounded bg-background/80 border border-border/50 text-foreground text-[10px]">
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
  const [copiedRunId, setCopiedRunId] = useState(false);

  const [isStarting, setIsStarting] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);

  const targetRunId = runId || propRun?.id || localRun?.id;

  const [prevRunId, setPrevRunId] = useState(runId);
  if (runId !== prevRunId) {
    setPrevRunId(runId);
    setLocalRun(null);
  }

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

  const handleCopyRunId = useCallback(() => {
    if (activeRun?.id && typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(activeRun.id);
      setCopiedRunId(true);
      setTimeout(() => setCopiedRunId(false), 2000);
    }
  }, [activeRun?.id]);

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

  // Stage list sorted by position with live stream and snapshot reconciliation
  const stages = useMemo(() => {
    const streamList = stream.stages || [];
    const runList = activeRunStages || [];
    if (streamList.length === 0) return [...runList].sort((a, b) => a.position - b.position);
    if (runList.length === 0) return [...streamList].sort((a, b) => a.position - b.position);

    const map = new Map<string, StagePublicResponse>();
    for (const s of streamList) map.set(s.stage_name, s);
    for (const r of runList) {
      const cur = map.get(r.stage_name);
      if (!cur) {
        map.set(r.stage_name, r);
      } else if (r.status !== "pending" && cur.status === "pending") {
        map.set(r.stage_name, r);
      } else if ((r.status === "succeeded" || r.status === "failed") && cur.status !== r.status) {
        map.set(r.stage_name, r);
      } else if (r.progress_pct > cur.progress_pct) {
        map.set(r.stage_name, { ...cur, ...r });
      }
    }
    return Array.from(map.values()).sort((a, b) => a.position - b.position);
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

  // Arrow key navigation between stages
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (stages.length === 0) return;
      const curIdx = stages.findIndex((s) => s.id === currentSelectedStageId);
      if (curIdx === -1) return;

      if (e.key === "ArrowRight") {
        e.preventDefault();
        const nextIdx = (curIdx + 1) % stages.length;
        setSelectedStageId(stages[nextIdx].id);
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        const prevIdx = (curIdx - 1 + stages.length) % stages.length;
        setSelectedStageId(stages[prevIdx].id);
      }
    },
    [stages, currentSelectedStageId],
  );

  const selectedStage = stages.find((s) => s.id === currentSelectedStageId) || stages[0];
  const selectedInfo = selectedStage ? getStageDisplayInfo(selectedStage) : null;

  // Action state rules
  const isAgentConnected = Boolean(activeRun?.agent_connected);
  const isPending = activeRun?.status === "pending";
  const isRunning = activeRun?.status === "running";
  const isCancellingStatus = activeRun?.status === "cancelling";
  const isFailed = activeRun?.status === "failed";
  const isRolledBack = activeRun?.status === "rolled_back";

  const isStartDisabled = !isPending || !isAgentConnected || isStarting;
  const canCancel = isPending || isRunning;
  const canRetry = isFailed || isRolledBack;

  const startTooltip = !isAgentConnected
    ? "Local agent disconnected. An active paired agent is required to start deployments."
    : !isPending
      ? `Cannot start pipeline in status '${activeRun?.status}'.`
      : "Start deployment pipeline";

  const handleStart = async () => {
    if (isStartDisabled || !activeRun) return;
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
    if (!canCancel || isCancelling || isCancellingStatus || !activeRun) return;
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
    if (!canRetry || isRetrying || !activeRun) return;
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
  const hostPort =
    (stageMeta.resolved_host_port as number | undefined) ??
    (metaDetails.resolved_host_port as number | undefined) ??
    (stageMeta.target_host_port as number | undefined) ??
    (metaDetails.target_host_port as number | undefined) ??
    (stageMeta.configured_host_port as number | undefined) ??
    (metaDetails.configured_host_port as number | undefined) ??
    (stageMeta.host_port as number | undefined) ??
    (metaDetails.host_port as number | undefined) ??
    ((activeRun?.configuration?.docker_config as Record<string, unknown> | undefined)?.host_port as number | undefined);
  const containerPort =
    (stageMeta.resolved_container_port as number | undefined) ??
    (metaDetails.resolved_container_port as number | undefined) ??
    (stageMeta.target_container_port as number | undefined) ??
    (metaDetails.target_container_port as number | undefined) ??
    (stageMeta.configured_container_port as number | undefined) ??
    (metaDetails.configured_container_port as number | undefined) ??
    (stageMeta.container_port as number | undefined) ??
    (metaDetails.container_port as number | undefined) ??
    ((activeRun?.configuration?.docker_config as Record<string, unknown> | undefined)?.container_port as number | undefined);
  const portConflict =
    Boolean(stageMeta.port_conflict ?? metaDetails.port_conflict);
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
    (activeRun?.configuration as Record<string, unknown> | undefined)?.simulation_mode === true ||
    (activeRun?.configuration as Record<string, unknown> | undefined)?.dry_run === true;

  // Source Preparation preflight state
  const sourcePrep = (activeRun?.configuration?.source_preparation || {}) as Record<string, unknown>;
  const sourcePrepStatus =
    (sourcePrep.status as string) ||
    (activeRun?.current_stage === "source_preparation" ? "preparing_workspace" : null);
  const sourcePrepLabel =
    (sourcePrep.status_label as string) ||
    (sourcePrepStatus === "cloning_repository"
      ? "Cloning repository"
      : sourcePrepStatus === "preparing_workspace"
        ? "Preparing workspace"
        : sourcePrepStatus === "verifying_checkout"
          ? "Verifying checkout"
          : sourcePrepStatus === "source_ready"
            ? "Source ready"
            : sourcePrepStatus === "failed"
              ? "Source preparation failed"
              : null);
  const sourcePrepRepo = (sourcePrep.repo_full_name as string) || "";
  const sourcePrepBranch = (sourcePrep.branch as string) || "";
  const sourcePrepPath = (sourcePrep.workspace_path as string) || "";
  const sourcePrepError =
    sourcePrepStatus === "failed"
      ? (sourcePrep.error as string) ||
        (activeRun?.primary_error?.stage_name === "source_preparation"
          ? (activeRun?.primary_error?.message as string) || activeRun?.error_summary
          : null)
      : null;

  // Strategy target statuses for G7 summary
  const dockerTargetStatus = getTargetVerificationStatus("docker", activeRun?.strategy, stages);
  const githubTargetStatus = getTargetVerificationStatus("github", activeRun?.strategy, stages);
  const vercelTargetStatus = getTargetVerificationStatus("vercel", activeRun?.strategy, stages);

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

  // Readiness Checkpoints & G8 Recovery Engine state
  const r0 =
    activeRun?.initial_readiness ||
    activeRun?.readiness_checkpoints?.find(
      (c) => c.checkpoint_type === "initial_r0" || c.checkpoint_type === "initial_readiness",
    );

  const r1 =
    activeRun?.post_generation_readiness ||
    activeRun?.readiness_checkpoints?.find(
      (c) =>
        c.checkpoint_type === "post_generation_r1" ||
        c.checkpoint_type === "post_generation_readiness",
    );

  const recoveryCheckpoints =
    activeRun?.readiness_checkpoints?.filter(
      (c) =>
        c.checkpoint_type === "recovery_recheck" ||
        c.checkpoint_type === "recovery" ||
        c.checkpoint_type.includes("recovery"),
    ) || [];

  const latestRecovery =
    recoveryCheckpoints.length > 0
      ? recoveryCheckpoints[recoveryCheckpoints.length - 1]
      : null;

  const recoveryAttempts =
    (activeRun?.configuration?.recovery_attempts as number | undefined) ??
    (recoveryCheckpoints.length > 0 ? recoveryCheckpoints.length : 0);

  const hasRecovery =
    recoveryAttempts > 0 ||
    Boolean(latestRecovery) ||
    Boolean(
      activeRun?.primary_error &&
        (activeRun.primary_error as Record<string, unknown>).recovery,
    );

  // Calculate score delta between R1 and R0
  const scoreDelta = useMemo(() => {
    if (typeof r1?.score_delta === "number") return r1.score_delta;
    if (
      typeof r1?.score === "number" &&
      typeof r0?.score === "number" &&
      r1.score !== null &&
      r0.score !== null
    ) {
      return r1.score - r0.score;
    }
    return null;
  }, [r1, r0]);

  // Derive human-readable progress bar state and current activity label
  const progressInfo = useMemo(() => {
    if (!activeRun) {
      return {
        pct: 0,
        label: "Loading autonomous deployment pipeline...",
        ariaText: "Loading autonomous deployment pipeline",
        fillColor: "bg-muted-foreground/30",
        trackColor: "bg-muted",
        textColor: "text-muted-foreground",
        isIndeterminate: true,
      };
    }
    const rawPct = activeRun.progress_pct ?? 0;
    const clampedPct = Math.min(100, Math.max(0, rawPct));

    const runningStage = stages.find((s) => s.status === "running" || s.status === "cancelling");
    const failedStage = stages.find((s) => s.status === "failed");
    const currentName = activeRun.current_stage || runningStage?.stage_name || failedStage?.stage_name;
    const activeStageInfo = currentName ? getStageDisplayInfo({ stage_name: currentName, position: 0, status: "running", progress_pct: 0, id: "", run_id: "" }) : null;

    if (activeRun.status === "succeeded") {
      return {
        pct: 100,
        label: "Completed — All deployment gates G1–G7 verified & signed",
        ariaText: "100 percent - Deployment succeeded and verified",
        fillColor: "bg-emerald-500",
        trackColor: "bg-emerald-500/20",
        textColor: "text-emerald-600 dark:text-emerald-400",
        isIndeterminate: false,
      };
    }

    if (activeRun.status === "failed") {
      const errTarget = activeStageInfo?.title || activeRun.current_stage || "Pipeline execution";
      return {
        pct: clampedPct,
        label: `Failed at ${errTarget} (${clampedPct}%)`,
        ariaText: `Failed at ${clampedPct} percent on ${errTarget}`,
        fillColor: "bg-rose-500",
        trackColor: "bg-rose-500/20",
        textColor: "text-rose-600 dark:text-rose-400",
        isIndeterminate: false,
      };
    }

    if (activeRun.status === "rolled_back") {
      return {
        pct: clampedPct,
        label: `Rolled Back — Compensation cleanup complete (${clampedPct}%)`,
        ariaText: `Rolled back at ${clampedPct} percent`,
        fillColor: "bg-amber-500",
        trackColor: "bg-amber-500/20",
        textColor: "text-amber-600 dark:text-amber-400",
        isIndeterminate: false,
      };
    }

    if (activeRun.status === "cancelled") {
      return {
        pct: clampedPct,
        label: `Cancelled — Stopped at ${clampedPct}%`,
        ariaText: `Cancelled at ${clampedPct} percent`,
        fillColor: "bg-amber-500",
        trackColor: "bg-amber-500/20",
        textColor: "text-amber-600 dark:text-amber-400",
        isIndeterminate: false,
      };
    }

    if (activeRun.status === "cancelling") {
      return {
        pct: clampedPct,
        label: "Cancelling — Awaiting process settlement...",
        ariaText: "Cancelling deployment run",
        fillColor: "bg-amber-500",
        trackColor: "bg-amber-500/20",
        textColor: "text-amber-600 dark:text-amber-400",
        isIndeterminate: true,
      };
    }

    if (activeRun.status === "running") {
      if (hasRecovery && recoveryAttempts > 0) {
        return {
          pct: clampedPct,
          label: `G8 AI Recovery in progress (Iteration ${recoveryAttempts}/3) — Retrying ${activeStageInfo?.title || "stage"}`,
          ariaText: `G8 AI recovery in progress, ${clampedPct} percent`,
          fillColor: "bg-blue-500",
          trackColor: "bg-blue-500/20",
          textColor: "text-blue-600 dark:text-blue-400",
          isIndeterminate: false,
        };
      }
      if (sourcePrepStatus && sourcePrepStatus !== "source_ready") {
        return {
          pct: clampedPct,
          label: `Source Preparation — ${sourcePrepLabel || "Preparing workspace"}`,
          ariaText: `Source preparation in progress, ${clampedPct} percent`,
          fillColor: "bg-blue-500",
          trackColor: "bg-blue-500/20",
          textColor: "text-blue-600 dark:text-blue-400",
          isIndeterminate: false,
        };
      }
      return {
        pct: clampedPct,
        label: `Executing: ${activeStageInfo?.title || activeRun.current_stage || "Pipeline"} (${clampedPct}%)`,
        ariaText: `${clampedPct} percent - Executing ${activeStageInfo?.title || "pipeline stage"}`,
        fillColor: "bg-primary",
        trackColor: "bg-muted",
        textColor: "text-primary",
        isIndeterminate: false,
      };
    }

    // Pending
    return {
      pct: clampedPct,
      label: isAgentConnected
        ? "Ready to start — Pipeline queued (0%)"
        : "Awaiting active paired agent connection",
      ariaText: "Pipeline queued, 0 percent",
      fillColor: "bg-muted-foreground/30",
      trackColor: "bg-muted",
      textColor: "text-muted-foreground",
      isIndeterminate: false,
    };
  }, [activeRun, stages, hasRecovery, recoveryAttempts, sourcePrepStatus, sourcePrepLabel, isAgentConnected]);

  if (!activeRun) {
    return (
      <div
        data-testid="pipeline-loading"
        className="flex min-h-[400px] flex-col items-center justify-center rounded-xl border border-border bg-card p-12 text-sm text-muted-foreground shadow-sm space-y-3"
      >
        <Loader2 className="size-6 animate-spin text-primary" />
        <span className="font-medium">Loading autonomous deployment pipeline...</span>
      </div>
    );
  }

  return (
    <div
      className="space-y-6 w-full pb-16 outline-none"
      onKeyDown={handleKeyDown}
      tabIndex={0}
      aria-label="Autonomous deployment pipeline console. Use left and right arrow keys to navigate stages."
    >
      {/* Stream Reconnection Notice Banner */}
      {stream.isReconnecting && (
        <div
          data-testid="reconnection-banner"
          role="status"
          className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 dark:bg-amber-950/20 px-4 py-3 text-xs text-amber-800 dark:text-amber-200 shadow-sm backdrop-blur-sm"
        >
          <div className="flex items-center gap-3">
            <div className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-amber-500/20 text-amber-600 dark:text-amber-400">
              <WifiOff className="size-4 animate-pulse" />
            </div>
            <span className="font-medium">
              Live stream disconnected. Reconnecting to deployment event stream... Displaying authoritative database state.
            </span>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              void stream.refetchSnapshot();
              onRefresh?.();
            }}
            className="h-7 px-3 text-xs border-amber-500/40 bg-amber-500/10 hover:bg-amber-500/20 text-amber-800 dark:text-amber-200 shadow-none font-medium"
          >
            <RefreshCw className="mr-1.5 size-3" />
            Refresh
          </Button>
        </div>
      )}

      {/* Stream Error Notice Banner */}
      {stream.error && !stream.isReconnecting && (
        <div
          data-testid="stream-error-banner"
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-destructive/30 bg-destructive/10 dark:bg-destructive/15 px-4 py-3 text-xs text-destructive shadow-sm"
        >
          <div className="flex items-center gap-3">
            <div className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-destructive/20 text-destructive">
              <AlertCircle className="size-4" />
            </div>
            <span className="font-medium">
              Stream connection error: {stream.error}. State synchronized from database.
            </span>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              void stream.refetchSnapshot();
              onRefresh?.();
            }}
            className="h-7 px-3 text-xs border-destructive/40 hover:bg-destructive/20 shadow-none font-medium"
          >
            <RefreshCw className="mr-1.5 size-3" />
            Sync
          </Button>
        </div>
      )}

      {/* Header Summary Bar */}
      <div
        data-testid="header-summary-bar"
        className="rounded-2xl border border-border/80 bg-card/95 p-6 shadow-sm backdrop-blur-sm space-y-6"
      >
        {/* Top Bar: Breadcrumb Navigation and Action Controls */}
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          {/* Breadcrumb Navigation */}
          <nav
            data-testid="breadcrumb"
            aria-label="Breadcrumb"
            className="flex items-center gap-1.5 text-xs text-muted-foreground"
          >
            <Link
              href="/projects"
              className="hover:text-foreground font-medium transition-colors"
            >
              Projects
            </Link>
            <span className="text-muted-foreground/40 font-mono">/</span>
            <Link
              href={`/projects/${projectId}`}
              className="hover:text-foreground font-medium transition-colors truncate max-w-[200px]"
              title={projectName}
            >
              {projectName}
            </Link>
            <span className="text-muted-foreground/40 font-mono">/</span>
            <span className="font-semibold text-foreground bg-muted/60 px-2 py-0.5 rounded text-[11px] font-mono">
              Run #{activeRun.attempt_number}
            </span>
          </nav>

          {/* Action Controls */}
          <div className="flex flex-wrap items-center gap-2">
            <Button data-testid="btn-back-project" variant="outline" size="sm" asChild className="h-8 text-xs font-medium">
              <Link href={`/projects/${projectId}`}>
                <ArrowLeft className="mr-1.5 size-3.5" />
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
                className="h-8 text-xs font-medium"
              >
                {isRetrying ? (
                  <Loader2 className="mr-1.5 size-3.5 animate-spin" />
                ) : (
                  <RotateCcw className="mr-1.5 size-3.5" />
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
                className="h-8 text-xs font-medium"
              >
                {isCancelling || isCancellingStatus ? (
                  <>
                    <Loader2 className="mr-1.5 size-3.5 animate-spin" />
                    Cancelling...
                  </>
                ) : (
                  <>
                    <Ban className="mr-1.5 size-3.5" />
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
                className="h-8 text-xs font-medium bg-primary text-primary-foreground hover:bg-primary/90"
              >
                {isStarting ? (
                  <Loader2 className="mr-1.5 size-3.5 animate-spin" />
                ) : (
                  <Play className="mr-1.5 size-3.5 fill-current" />
                )}
                Start Pipeline
              </Button>
            )}
          </div>
        </div>

        {/* Identity & Status Hero Panel */}
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between pt-1 border-t border-border/50">
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-3">
              <h2 className="text-xl sm:text-2xl font-bold tracking-tight text-foreground">
                Deployment #{activeRun.attempt_number}
              </h2>

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
                className="capitalize text-xs font-semibold px-2.5 py-0.5 tracking-wide shadow-none"
              >
                {isRunning && <span className="size-1.5 rounded-full bg-current animate-pulse mr-1.5 inline-block" />}
                {activeRun.status === "succeeded" && <CheckCircle2 className="size-3 mr-1 inline-block" />}
                {activeRun.status === "failed" && <AlertCircle className="size-3 mr-1 inline-block" />}
                {activeRun.status}
              </Badge>

              {stream.isConnected && isRunning && (
                <span className="inline-flex items-center gap-1.5 text-[11px] font-mono text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 border border-emerald-500/30 px-2.5 py-0.5 rounded-full font-medium">
                  <span className="size-1.5 rounded-full bg-emerald-500 animate-pulse" />
                  Live Stream
                </span>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <div className="flex items-center gap-1.5 bg-muted/50 border border-border/60 rounded-md px-2 py-0.5">
                <Badge
                  data-testid="run-id-badge"
                  variant="outline"
                  className="font-mono text-[11px] font-semibold border-0 p-0 text-foreground"
                >
                  {activeRun.id}
                </Badge>
                <button
                  type="button"
                  onClick={handleCopyRunId}
                  className="text-muted-foreground hover:text-foreground transition-colors p-0.5 rounded focus:outline-none"
                  title={copiedRunId ? "Copied!" : "Copy Run ID"}
                  aria-label="Copy Run ID"
                >
                  {copiedRunId ? (
                    <Check className="size-3 text-emerald-500" />
                  ) : (
                    <Copy className="size-3" />
                  )}
                </button>
              </div>

              <Badge
                data-testid="strategy-badge"
                variant="secondary"
                className="font-medium text-xs bg-secondary/70 border border-border/40 px-2.5 py-0.5"
              >
                {getStrategyLabel(activeRun.strategy)}
              </Badge>
            </div>
          </div>

          {/* Instrumentation Telemetry: Elapsed Timer */}
          <div
            data-testid="elapsed-timer"
            className="flex items-center gap-3 bg-muted/40 dark:bg-muted/20 border border-border/60 rounded-xl px-4 py-2.5 self-start lg:self-auto shrink-0 shadow-inner"
          >
            <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Clock className="size-4" />
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground">
                Elapsed Time
              </div>
              <div className="text-sm font-mono font-bold text-foreground">
                {formatElapsedTime(activeRun.started_at, activeRun.completed_at, nowMs)}
              </div>
            </div>
          </div>
        </div>

        {/* Progress & Current Execution State */}
        <div className="space-y-2.5 rounded-xl border border-border/60 bg-muted/20 p-4">
          <div className="flex flex-wrap justify-between items-center text-xs gap-2">
            <div className="flex items-center gap-2 truncate">
              <div
                className={cn(
                  "size-2 rounded-full shrink-0",
                  isRunning
                    ? "bg-primary animate-pulse"
                    : activeRun.status === "succeeded"
                      ? "bg-emerald-500"
                      : activeRun.status === "failed"
                        ? "bg-destructive"
                        : "bg-muted-foreground",
                )}
              />
              <span className="font-semibold text-foreground text-xs sm:text-sm truncate" title={progressInfo.label}>
                {progressInfo.label}
              </span>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <span className="text-[10px] text-muted-foreground uppercase tracking-wider font-semibold">
                Progress
              </span>
              <span className={cn("font-mono font-bold text-base", progressInfo.textColor)}>
                {progressInfo.pct}%
              </span>
            </div>
          </div>

          <div
            data-testid="progress-bar"
            role="progressbar"
            aria-valuenow={progressInfo.pct}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuetext={progressInfo.ariaText}
            className={cn("h-2.5 w-full overflow-hidden rounded-full p-0.5 border border-border/50 bg-secondary/60 shadow-inner transition-colors", progressInfo.trackColor)}
          >
            <div
              className={cn(
                "h-full rounded-full transition-all duration-300 ease-out motion-reduce:transition-none shadow-sm",
                progressInfo.fillColor,
                progressInfo.isIndeterminate && "animate-pulse",
              )}
              style={{ width: `${progressInfo.pct}%` }}
            />
          </div>
        </div>

        {/* Agent Pairing Status Banner */}
        <div data-testid="agent-status-banner">
          {isAgentConnected ? (
            <div className="flex items-center gap-2.5 rounded-xl border border-emerald-500/30 bg-emerald-500/10 dark:bg-emerald-950/20 px-4 py-2.5 text-xs text-emerald-700 dark:text-emerald-300">
              <Server className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
              <span className="font-medium">Paired Agent Connected: Ready for local orchestration execution.</span>
            </div>
          ) : (
            <div
              role="alert"
              className="flex items-center gap-2.5 rounded-xl border border-amber-500/30 bg-amber-500/10 dark:bg-amber-950/20 px-4 py-2.5 text-xs text-amber-700 dark:text-amber-300"
            >
              <AlertTriangle className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />
              <span className="font-medium">
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
            className="flex items-center gap-2.5 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-2.5 text-xs text-destructive"
          >
            <AlertCircle className="size-4 shrink-0" />
            <span className="font-medium">{actionError}</span>
          </div>
        )}
      </div>

      {/* Autonomous Deployment Pipeline Stage View */}
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold tracking-tight text-foreground flex items-center gap-2">
              <span className="inline-block size-2 rounded-full bg-primary" />
              Autonomous Deployment Pipeline
            </h3>
            <p className="text-xs text-muted-foreground">
              Live execution across deployment gates G1–G7
            </p>
          </div>
          <span className="text-xs text-muted-foreground font-mono">
            Click any stage node or sub-operation card to inspect verdicts &amp; logs
          </span>
        </div>

        {/* Source Preparation Preflight Indicator */}
        {sourcePrepStatus && (
          <div
            data-testid="source-preparation-banner"
            className={cn(
              "flex flex-wrap items-center justify-between gap-3 px-4 py-2.5 rounded-lg border text-xs",
              sourcePrepStatus === "failed" || sourcePrepError
                ? "bg-destructive/10 border-destructive/30 text-destructive dark:bg-destructive/15"
                : sourcePrepStatus === "source_ready"
                  ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-700 dark:text-emerald-400"
                  : "bg-blue-500/10 border-blue-500/30 text-blue-700 dark:text-blue-400",
            )}
          >
            <div className="flex items-center gap-2 min-w-0">
              {sourcePrepStatus === "failed" || sourcePrepError ? (
                <AlertCircle className="size-4 shrink-0" />
              ) : sourcePrepStatus === "source_ready" ? (
                <CheckCircle2 className="size-4 shrink-0" />
              ) : (
                <Loader2 className="size-4 shrink-0 animate-spin" />
              )}
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold uppercase tracking-wider text-[11px]">
                  Source Preparation:
                </span>
                <span className="font-medium">{sourcePrepLabel || "Preparing workspace"}</span>
                {sourcePrepRepo && (
                  <span className="font-mono text-[11px] opacity-80">
                    ({sourcePrepRepo}
                    {sourcePrepBranch ? ` @ ${sourcePrepBranch}` : ""})
                  </span>
                )}
              </div>
            </div>

            {sourcePrepError ? (
              <span className="text-xs font-mono truncate max-w-md" title={sourcePrepError}>
                {sourcePrepError}
              </span>
            ) : sourcePrepPath ? (
              <span
                className="font-mono text-[11px] opacity-75 truncate max-w-xs"
                title={sourcePrepPath}
              >
                {sourcePrepPath}
              </span>
            ) : null}
          </div>
        )}

        {/* R0 Baseline Readiness Assessment Card (Informational Checkpoint) */}
        {r0 && (
          <div
            data-testid="r0-baseline-card"
            className="rounded-xl border border-border bg-card p-4 shadow-sm space-y-3"
          >
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2.5">
                <Badge variant="outline" className="font-mono text-xs border-primary/40 text-primary">
                  R0 Baseline
                </Badge>
                <div className="flex items-center gap-2">
                  <h4 className="text-sm font-semibold text-foreground">
                    Pre-Flight Readiness Assessment
                  </h4>
                  <span className="text-[11px] text-muted-foreground hidden sm:inline">
                    (Informational workspace check before G1)
                  </span>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground font-mono">
                  Score:{" "}
                  <span className="font-bold text-foreground">
                    {r0.score !== null && r0.score !== undefined ? `${r0.score}/${r0.max_score || 100}` : "--"}
                  </span>
                </span>
                <Badge
                  variant={
                    r0.status === "ready"
                      ? "success"
                      : r0.status === "needs_remediation"
                        ? "warning"
                        : "destructive"
                  }
                  className="text-xs uppercase font-medium"
                >
                  {r0.status}
                </Badge>
              </div>
            </div>

            {r0.dimension_scores && (
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                {Object.entries(r0.dimension_scores).map(([dim, val]) => (
                  <div
                    key={dim}
                    className="rounded-lg border border-border/60 bg-muted/20 p-2 space-y-0.5"
                  >
                    <span className="text-[10px] uppercase font-mono text-muted-foreground">
                      {dim.replace("_", " ")}
                    </span>
                    <p className="font-mono font-semibold text-foreground">{val} pts</p>
                  </div>
                ))}
              </div>
            )}

            {r0.blockers && r0.blockers.length > 0 && (
              <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-2.5 text-xs text-destructive space-y-1">
                <span className="font-semibold flex items-center gap-1.5">
                  <ShieldAlert className="size-3.5 shrink-0" /> Blockers Detected ({r0.blockers.length})
                </span>
                <ul className="list-disc list-inside space-y-0.5 text-[11px] font-mono">
                  {r0.blockers.map((b, idx) => (
                    <li key={idx} className="truncate" title={b}>
                      {b}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {r0.explanation && (
              <p className="text-xs text-muted-foreground font-mono truncate" title={r0.explanation}>
                {r0.explanation}
              </p>
            )}
          </div>
        )}

        {/* Canonical G1–G7 Seven-Gate Grid */}
        <div
          data-testid="blue-ocean-graph"
          className="rounded-xl border border-border bg-card shadow-sm overflow-hidden"
        >
          <div className="overflow-x-auto [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden">
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
                    <div key={col.key} className="contents">
                      <div
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

                      {/* R1 Post-Generation Informational Checkpoint nested after G2 */}
                      {col.gateId === "G2" && (
                        <div
                          className="w-28 shrink-0 p-3 border-l border-border/60 bg-primary/5 flex flex-col justify-between items-center text-center"
                        >
                          <div className="flex items-center gap-1">
                            <Badge
                              variant="outline"
                              className="text-[10px] font-mono border-primary/40 text-primary"
                            >
                              R1
                            </Badge>
                            <span className="font-semibold text-xs text-foreground">Post-Gen</span>
                          </div>
                          <span className="text-[10px] text-muted-foreground">Readiness</span>
                        </div>
                      )}
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
                    <div key={col.key} className="contents">
                      <div
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

                      {/* R1 Post-Gen Indicator cell */}
                      {col.gateId === "G2" && (
                        <div
                          data-testid="r1-post-generation-indicator"
                          className="w-28 shrink-0 p-2.5 flex flex-col items-center justify-center border-l border-border/60 bg-primary/5 space-y-1.5 text-center"
                        >
                          <span className="text-[10px] uppercase font-mono tracking-wider text-muted-foreground">
                            R1 Score
                          </span>
                          <div className="text-sm font-bold font-mono text-primary">
                            {r1?.score !== undefined && r1?.score !== null ? `${r1.score}/100` : "--"}
                          </div>
                          {r1 ? (
                            <Badge
                              variant={
                                r1.status === "ready"
                                  ? "success"
                                  : r1.status === "needs_remediation"
                                    ? "warning"
                                    : "destructive"
                              }
                              className="text-[9px] uppercase px-1.5 py-0"
                            >
                              {r1.status}
                            </Badge>
                          ) : (
                            <span className="text-[10px] text-muted-foreground font-mono">Pending</span>
                          )}
                          {typeof scoreDelta === "number" && (
                            <span
                              className={cn(
                                "text-[9px] font-mono font-medium",
                                scoreDelta > 0
                                  ? "text-emerald-600 dark:text-emerald-400"
                                  : scoreDelta < 0
                                    ? "text-rose-600 dark:text-rose-400"
                                    : "text-muted-foreground",
                              )}
                            >
                              {scoreDelta >= 0 ? `+${scoreDelta}` : scoreDelta} vs R0
                            </span>
                          )}
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

      {/* G8 Conditional AI Recovery Panel */}
      {hasRecovery && (
        <div
          data-testid="g8-recovery-panel"
          className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-5 shadow-sm space-y-4"
        >
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-amber-500/20 pb-3">
            <div className="flex items-center gap-2.5">
              <Badge
                variant="outline"
                className="border-amber-500/50 text-amber-600 dark:text-amber-400 font-mono text-xs"
              >
                G8 Recovery Engine
              </Badge>
              <h4 className="text-sm font-semibold text-foreground">
                Autonomous AI Remediation &amp; Resiliency
              </h4>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant="secondary" className="font-mono text-xs">
                Iteration {recoveryAttempts}/3
              </Badge>
              <span className="text-[11px] text-muted-foreground font-mono">
                (Bounded 3-attempt limit)
              </span>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
            <div className="rounded-lg border border-border/60 bg-background/80 p-3 space-y-1">
              <span className="text-muted-foreground font-mono text-[11px]">Recovery Status</span>
              <p className="font-semibold text-foreground uppercase tracking-wider text-xs">
                {latestRecovery ? latestRecovery.status : "Attempted"}
              </p>
            </div>

            <div className="rounded-lg border border-border/60 bg-background/80 p-3 space-y-1">
              <span className="text-muted-foreground font-mono text-[11px]">Recovery Score (R_rec)</span>
              <p className="font-mono font-bold text-foreground">
                {latestRecovery?.score !== undefined && latestRecovery?.score !== null
                  ? `${latestRecovery.score}/100`
                  : "--"}
              </p>
            </div>

            <div className="rounded-lg border border-border/60 bg-background/80 p-3 space-y-1">
              <span className="text-muted-foreground font-mono text-[11px]">Target Gate</span>
              <p className="font-mono font-medium text-foreground">
                {activeRun.current_stage ||
                  (activeRun.primary_error?.stage_name as string | undefined) ||
                  "G8 Evaluation"}
              </p>
            </div>
          </div>

          {latestRecovery?.explanation && (
            <div className="rounded-lg border border-border/60 bg-background/80 p-3.5 text-xs space-y-1.5">
              <span className="text-muted-foreground font-medium flex items-center gap-1.5">
                <Sparkles className="size-3.5 text-amber-500" /> Remediation Diagnostics
              </span>
              <p className="text-foreground font-mono text-[11px] leading-relaxed">
                {latestRecovery.explanation}
              </p>
            </div>
          )}
        </div>
      )}

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
          {(containerId || hostPort || commitSha || deploymentUrl || prNumber || prUrl || isSimulated) && (
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

                {hostPort && (
                  <div className="rounded-lg border border-border bg-background p-3 space-y-1">
                    <div className="flex items-center justify-between text-muted-foreground">
                      <span className="flex items-center gap-1.5">
                        <Box className="size-3.5" /> Port Mapping
                      </span>
                      {portConflict && (
                        <Badge
                          variant="outline"
                          className="text-[9px] px-1 py-0 border-amber-500/40 text-amber-600 dark:text-amber-400 font-mono"
                        >
                          Conflict Resolved
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <span
                        data-testid="port-mapping"
                        className="font-mono font-medium block truncate"
                      >
                        {containerPort ? `${hostPort}:${containerPort}` : `${hostPort}`}
                      </span>
                      <a
                        data-testid="local-container-link"
                        href={`http://localhost:${hostPort}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-[11px] text-primary underline font-medium hover:opacity-80"
                      >
                        Open <ExternalLink className="size-3" />
                      </a>
                    </div>
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
