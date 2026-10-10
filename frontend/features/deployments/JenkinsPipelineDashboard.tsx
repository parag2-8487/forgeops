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
import {
  useAutonomousDeployStream,
  type AutonomousLogEntry,
} from "./useAutonomousDeployStream";

export type DeploymentStrategy =
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
    return { title: "G1 Blueprint", subtitle: "Project & Config Spec", badge: "G1", isGate: true };
  }
  if (gateId === "G2" || normName.includes("g2") || normName.includes("artifact")) {
    return { title: "G2 Artifacts", subtitle: "Dockerfile / Compose", badge: "G2", isGate: true };
  }
  if (gateId === "G3" || normName.includes("g3") || normName.includes("consistency")) {
    return { title: "G3 Consistency", subtitle: "Ports, Branch & Config", badge: "G3", isGate: true };
  }
  if (gateId === "G4" || normName.includes("g4") || normName === "docker_build" || normName.includes("build")) {
    return { title: "Docker Build", subtitle: "Container Image Build", badge: "G4", isGate: true };
  }
  if (gateId === "G5" || normName.includes("g5") || normName === "docker_apply" || normName.includes("apply")) {
    return { title: "Docker Apply", subtitle: "Container Startup", badge: "G5", isGate: true };
  }
  if (gateId === "G6" || normName.includes("g6") || normName === "docker_workload" || normName.includes("workload")) {
    return { title: "G6 Workload", subtitle: "HTTP Health Probe", badge: "G6", isGate: true };
  }
  if (normName === "github_release" || normName.includes("github")) {
    return { title: "GitHub Release", subtitle: "Code Sync & Release", badge: "Git", isGate: false };
  }
  if (normName === "vercel_deploy" || normName.includes("vercel")) {
    return { title: "Vercel Deploy", subtitle: "Cloud Edge Deployment", badge: "Cloud", isGate: false };
  }
  if (gateId === "G7" || normName.includes("g7") || normName.includes("verification")) {
    return { title: "G7 Verification", subtitle: "Multi-Target E2E Probe", badge: "G7", isGate: true };
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
      | Record<string, string>
      | undefined);

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
  const [nowMs, setNowMs] = useState<number>(Date.now());
  const [actionError, setActionError] = useState<string | null>(null);

  // Button loading states
  const [isStarting, setIsStarting] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);

  // Sync prop changes
  useEffect(() => {
    if (propRun) {
      setLocalRun(propRun);
    }
  }, [propRun]);

  const targetRunId = localRun?.id ?? runId;

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

  // Sync streaming run updates
  useEffect(() => {
    if (stream.run) {
      setLocalRun(stream.run);
      onRunChange?.(stream.run);
    }
  }, [stream.run, onRunChange]);

  const activeRun = localRun ?? stream.run ?? runQuery.data ?? initialRun ?? null;

  // Live timer tick for running executions
  useEffect(() => {
    if (activeRun?.status === "running" || activeRun?.status === "cancelling") {
      const interval = setInterval(() => {
        setNowMs(Date.now());
      }, 1000);
      return () => clearInterval(interval);
    }
  }, [activeRun?.status]);

  // Stage list sorted by position with live stream priority
  const stages = useMemo(() => {
    if (stream.stages && stream.stages.length > 0) {
      return [...stream.stages].sort((a, b) => a.position - b.position);
    }
    if (!activeRun?.stages) return [];
    return [...activeRun.stages].sort((a, b) => a.position - b.position);
  }, [stream.stages, activeRun?.stages]);

  const activeLogs = stream.logs.length > 0 ? stream.logs : (propLogs ?? initialLogs ?? []);

  // Automatically select the active or failed stage if not manually selected
  useEffect(() => {
    if (stages.length === 0) return;
    if (!selectedStageId || !stages.some((s) => s.id === selectedStageId)) {
      const failed = stages.find((s) => s.status === "failed");
      const running = stages.find((s) => s.status === "running" || s.status === "cancelling");
      const g7 = stages.find(
        (s) =>
          s.gate_id === "G7" ||
          s.stage_name === "G7_verification" ||
          s.stage_name.toLowerCase().includes("g7"),
      );
      if (failed) {
        setSelectedStageId(failed.id);
      } else if (running) {
        setSelectedStageId(running.id);
      } else if (activeRun?.status === "succeeded" && g7) {
        setSelectedStageId(g7.id);
      } else {
        setSelectedStageId(stages[0].id);
      }
    }
  }, [stages, selectedStageId, activeRun?.status]);

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

  const selectedStage = stages.find((s) => s.id === selectedStageId) || stages[0];
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
  const stageMeta = ((selectedStage?.stage_metadata || selectedStage?.metadata || {}) as Record<
    string,
    unknown
  >) || {};
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
    (stageMeta.pr_number as number | undefined) ||
    (metaDetails.pr_number as number | undefined);
  const prUrl =
    (stageMeta.pr_url as string | undefined) ||
    (metaDetails.pr_url as string | undefined);
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
            <span className="font-semibold text-foreground">
              Run #{activeRun.attempt_number}
            </span>
          </nav>

          {/* Action Controls */}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              data-testid="btn-back-project"
              variant="outline"
              size="sm"
              asChild
            >
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

            <Badge
              data-testid="strategy-badge"
              variant="secondary"
              className="font-medium text-xs"
            >
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

      {/* Horizontal Blue Ocean Graph */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold tracking-tight text-foreground">
            Pipeline Execution Graph
          </h3>
          <span className="text-xs text-muted-foreground">
            Click any stage node to inspect gate verdicts and details
          </span>
        </div>

        <div
          data-testid="blue-ocean-graph"
          className="rounded-xl border border-border bg-card p-6 shadow-sm overflow-x-auto"
        >
          <div className="flex items-center min-w-max gap-0 py-4">
            {stages.map((stage, idx) => {
              const info = getStageDisplayInfo(stage);
              const isSelected = stage.id === selectedStageId;

              // Node visual style classes
              let nodeCircleClasses = "border-2 bg-muted text-muted-foreground border-muted-foreground/30";
              let iconComponent = <Clock className="size-4" />;

              if (stage.status === "succeeded") {
                nodeCircleClasses = "border-emerald-500 bg-emerald-500 text-white shadow-sm";
                iconComponent = <CheckCircle2 className="size-4" />;
              } else if (stage.status === "failed" || stage.status === "rolled_back") {
                nodeCircleClasses = "border-rose-500 bg-rose-500 text-white shadow-sm";
                iconComponent = <XCircle className="size-4" />;
              } else if (stage.status === "running" || stage.status === "cancelling") {
                nodeCircleClasses = "border-blue-500 bg-blue-500 text-white animate-pulse shadow-sm";
                iconComponent = <Loader2 className="size-4 animate-spin" />;
              } else if (stage.status === "waiting") {
                nodeCircleClasses = "border-amber-500 bg-amber-500 text-white animate-pulse shadow-sm";
                iconComponent = <Clock className="size-4" />;
              } else if (stage.status === "cancelled") {
                nodeCircleClasses = "border-amber-500 bg-amber-500 text-white shadow-sm";
                iconComponent = <Ban className="size-4" />;
              } else if (stage.status === "skipped") {
                nodeCircleClasses =
                  "border-2 border-dashed border-muted-foreground/60 bg-transparent text-muted-foreground";
                iconComponent = <Ban className="size-4" />;
              }

              // Connecting line classes to subsequent node
              const isLast = idx === stages.length - 1;
              let connectorClasses = "bg-border";
              if (stage.status === "succeeded") {
                connectorClasses = "bg-emerald-500";
              } else if (stage.status === "running") {
                connectorClasses = "bg-blue-500 animate-pulse";
              } else if (stage.status === "failed") {
                connectorClasses = "bg-rose-500";
              }

              return (
                <div key={stage.id} className="flex items-center">
                  {/* Node Button */}
                  <button
                    type="button"
                    data-testid={`stage-node-${stage.stage_name}`}
                    onClick={() => setSelectedStageId(stage.id)}
                    aria-label={`Select stage ${info.title}`}
                    aria-pressed={isSelected}
                    className={cn(
                      "group flex flex-col items-center gap-2 p-2 rounded-xl transition-all focus:outline-none",
                      isSelected && "ring-2 ring-primary ring-offset-2 ring-offset-background bg-accent/40",
                      !isSelected && "hover:bg-accent/20",
                    )}
                  >
                    <div
                      className={cn(
                        "flex size-11 items-center justify-center rounded-full transition-transform group-hover:scale-105",
                        nodeCircleClasses,
                      )}
                    >
                      {iconComponent}
                    </div>

                    <div className="flex flex-col items-center text-center max-w-[110px]">
                      <span className="font-semibold text-xs text-foreground truncate w-full">
                        {info.title}
                      </span>
                      <span className="text-[11px] text-muted-foreground capitalize">
                        {stage.status}
                      </span>
                    </div>
                  </button>

                  {/* Connecting Line */}
                  {!isLast && (
                    <div
                      data-testid={`stage-connector-${stage.stage_name}`}
                      className={cn("h-0.5 w-10 sm:w-16 transition-colors mx-1", connectorClasses)}
                    />
                  )}
                </div>
              );
            })}
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
                <h3 className="text-lg font-semibold text-foreground">
                  {selectedInfo.title}
                </h3>
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
                      : selectedStage.status === "cancelled" || selectedStage.status === "cancelling"
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
          {(containerId || commitSha || deploymentUrl || prNumber || prUrl) && (
            <div className="space-y-2 border-t border-border/60 pt-4">
              <h4 className="text-xs font-semibold text-foreground uppercase tracking-wider">
                Operational Artifacts
              </h4>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 text-xs">
                {(prNumber || prUrl) && (
                  <div
                    data-testid="github-pr-card"
                    className="rounded-lg border border-border bg-background p-3.5 space-y-2 sm:col-span-2 lg:col-span-3"
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <GitBranch className="size-4 text-primary" />
                        <span className="font-semibold text-sm">
                          Pull Request #{prNumber}
                        </span>
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
                          Commit: <span data-testid="pr-commit-sha" className="text-foreground">{commitSha.slice(0, 8)}</span>
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
                    <span data-testid="container-id" className="font-mono font-medium block truncate">
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
