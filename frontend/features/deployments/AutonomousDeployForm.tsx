// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api, ApiProblemError, queryKeys } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { validateGitBranchName } from "@/lib/git-validation";
import { validateNewRepoName, type RepoValidationResult } from "@/lib/repo-validation";
import type { RepositoryItem, RepositoryPage } from "@/features/integrations/RepositoryPicker";
import {
  AlertTriangle,
  Box,
  CheckCircle2,
  ChevronDown,
  GitBranch,
  Globe,
  Loader2,
  RefreshCw,
  Search,
  Server,
} from "lucide-react";

export { validateNewRepoName, type RepoValidationResult };

export type AvailabilityStatus =
  "idle" | "checking" | "available" | "taken" | "unknown" | "invalid" | "error";

export interface RepositoryAvailabilityResponse {
  available: boolean;
  owner: string;
  name: string;
  full_name: string;
  status: "available" | "taken" | "unknown" | "invalid" | "error";
  message: string;
  reason?: string;
  detail?: string | null;
}

export interface GitHubStatusResponse {
  configured: boolean;
  connected: boolean;
  login?: string | null;
  avatar_url?: string | null;
}

export type DeploymentStrategy =
  "docker_only" | "docker_github_vercel" | "docker_github" | "github_only" | "vercel_only";

export interface StrategyOption {
  id: DeploymentStrategy;
  title: string;
  badge: string;
  description: string;
  hasDocker: boolean;
  hasGitHub: boolean;
  hasVercel: boolean;
}

export const STRATEGIES: StrategyOption[] = [
  {
    id: "docker_only",
    title: "Docker Only",
    badge: "Local Only",
    description:
      "Builds, executes, and verifies containers locally without publishing to GitHub or deploying to Vercel.",
    hasDocker: true,
    hasGitHub: false,
    hasVercel: false,
  },
  {
    id: "docker_github_vercel",
    title: "Full Stack (Docker + GitHub + Vercel)",
    badge: "Full Pipeline",
    description:
      "Builds Docker container, synchronizes codebase to GitHub, and deploys frontend to Vercel.",
    hasDocker: true,
    hasGitHub: true,
    hasVercel: true,
  },
  {
    id: "docker_github",
    title: "Docker + GitHub",
    badge: "Container & Git",
    description:
      "Builds and validates Docker container locally, then pushes synced repository to GitHub.",
    hasDocker: true,
    hasGitHub: true,
    hasVercel: false,
  },
  {
    id: "github_only",
    title: "GitHub Only",
    badge: "Git Sync",
    description:
      "Synchronizes project files and pushes commits directly to GitHub without containers or Vercel.",
    hasDocker: false,
    hasGitHub: true,
    hasVercel: false,
  },
  {
    id: "vercel_only",
    title: "Vercel Only",
    badge: "Frontend Cloud",
    description:
      "Deploys frontend directly to Vercel production or preview without Docker or GitHub push.",
    hasDocker: false,
    hasGitHub: false,
    hasVercel: true,
  },
];

interface DeviceRead {
  id: string;
  project_id: string;
  status: "pending" | "active" | "policy_stale" | "revoked" | "abandoned";
  agent_version: string;
  platform: string;
  last_seen: string | null;
  seconds_since_last_seen: number | null;
  heartbeat_fresh: boolean | null;
}

interface DevicePage {
  devices: DeviceRead[];
  next_cursor: string | null;
}

export interface AutonomousRunResponse {
  id: string;
  project_id: string;
  status: string;
  strategy: DeploymentStrategy;
  attempt_number: number;
  agent_connected?: boolean;
}

export interface AutonomousDeployFormProps {
  projectId: string;
  projectName: string;
  onCancel?: () => void;
  onSuccess?: (run: AutonomousRunResponse) => void;
}

export function AutonomousDeployForm({
  projectId,
  projectName,
  onCancel,
  onSuccess,
}: AutonomousDeployFormProps) {
  const router = useRouter();

  const [strategy, setStrategy] = useState<DeploymentStrategy>("docker_github_vercel");

  // GitHub config state
  const [githubMode, setGithubMode] = useState<"existing" | "new_private">("new_private");
  const [repositoryVisibility, setRepositoryVisibility] = useState<"private" | "public">("private");
  const [branchMode, setBranchMode] = useState<"existing" | "new">("existing");
  const [publishingMode, setPublishingMode] = useState<"direct_push" | "pull_request">(
    "direct_push",
  );
  const [githubRepoName, setGithubRepoName] = useState(() =>
    (projectName || "").toLowerCase().replace(/[^a-z0-9-]/g, "-"),
  );
  const [manualRepoEntry, setManualRepoEntry] = useState(false);
  const [repoSearchQuery, setRepoSearchQuery] = useState("");
  const [repoDropdownOpen, setRepoDropdownOpen] = useState(false);

  // Branch state & combobox
  const [githubBranch, setGithubBranch] = useState("main");
  const [branchSearchFilter, setBranchSearchFilter] = useState("");
  const [branchDropdownOpen, setBranchDropdownOpen] = useState(false);
  const [highlightedBranchIndex, setHighlightedBranchIndex] = useState(-1);
  const branchComboboxRef = useRef<HTMLDivElement>(null);
  const [githubCommitMessage, setGithubCommitMessage] = useState(
    "Automated deployment by ForgeOps",
  );

  // PR is only permitted when targeting an existing repo AND an existing base branch
  const isPrDisabled = githubMode === "new_private" || branchMode === "new";

  const handleSetGithubMode = (mode: "existing" | "new_private") => {
    setGithubMode(mode);
    if (mode === "new_private" && publishingMode === "pull_request") {
      setPublishingMode("direct_push");
    }
  };

  const handleSetBranchMode = (mode: "existing" | "new") => {
    setBranchMode(mode);
    if (mode === "new" && publishingMode === "pull_request") {
      setPublishingMode("direct_push");
    }
  };

  // Clean fallback: auto-switch to direct_push if PR becomes disabled
  useEffect(() => {
    if (isPrDisabled && publishingMode === "pull_request") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setPublishingMode("direct_push");
    }
  }, [isPrDisabled, publishingMode]);

  // Click-outside listener for branch combobox
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (branchComboboxRef.current && !branchComboboxRef.current.contains(e.target as Node)) {
        setBranchDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Vercel config state
  const [vercelProjectName, setVercelProjectName] = useState(() =>
    (projectName || "").toLowerCase().replace(/[^a-z0-9-]/g, "-"),
  );
  const [vercelProduction, setVercelProduction] = useState(true);

  // Docker config state
  const [hostPort, setHostPort] = useState("8080");
  const [containerPort, setContainerPort] = useState("8080");

  // Submission state
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Query paired agent status for this project
  const devicesQuery = useQuery({
    queryKey: ["agents", "devices", projectId],
    queryFn: () => api.get<DevicePage>(`/agents/devices?project_id=${projectId}`),
    refetchInterval: 10_000,
    retry: false,
  });

  const activeAgent = devicesQuery.data?.devices?.find(
    (d) =>
      d.status === "active" &&
      (d.heartbeat_fresh === true ||
        (d.seconds_since_last_seen !== null && d.seconds_since_last_seen <= 30)),
  );
  const isAgentConnected = Boolean(activeAgent);

  const selectedStrategyOption = STRATEGIES.find((s) => s.id === strategy) || STRATEGIES[0];

  // Query GitHub connection status to resolve authenticated owner
  const githubStatusQuery = useQuery<GitHubStatusResponse>({
    queryKey: queryKeys.integrations.github(),
    queryFn: () => api.get<GitHubStatusResponse>("/integrations/github"),
    enabled: selectedStrategyOption.hasGitHub,
    retry: 1,
    staleTime: 60_000,
  });

  const connectedUsername = githubStatusQuery.data?.login || null;
  const isGitHubConnected = Boolean(githubStatusQuery.data?.connected && connectedUsername);

  // New repository availability checking state
  const [availabilityStatus, setAvailabilityStatus] = useState<AvailabilityStatus>("idle");
  const [availabilityMessage, setAvailabilityMessage] = useState<string>("");
  const [availabilityDetail, setAvailabilityDetail] = useState<string | null>(null);
  const [retryTrigger, setRetryTrigger] = useState(0);
  const checkCounterRef = useRef<number>(0);
  const abortControllerRef = useRef<AbortController | null>(null);

  const repoValidation = useMemo(
    () => validateNewRepoName(githubRepoName, connectedUsername),
    [githubRepoName, connectedUsername],
  );

  useEffect(() => {
    if (githubMode !== "new_private" || !selectedStrategyOption.hasGitHub) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setAvailabilityStatus("idle");
      setAvailabilityMessage("");
      setAvailabilityDetail(null);
      return;
    }

    if (!isGitHubConnected || !connectedUsername) {
      setAvailabilityStatus("idle");
      setAvailabilityMessage("");
      setAvailabilityDetail(null);
      return;
    }

    if (repoValidation.error) {
      setAvailabilityStatus("invalid");
      setAvailabilityMessage(repoValidation.error);
      setAvailabilityDetail(null);
      return;
    }

    setAvailabilityStatus("checking");
    setAvailabilityMessage("");
    setAvailabilityDetail(null);

    const requestId = ++checkCounterRef.current;
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const controller = new AbortController();
    abortControllerRef.current = controller;

    const timer = setTimeout(async () => {
      try {
        const res = await api.get<RepositoryAvailabilityResponse>(
          `/integrations/github/repositories/check-availability?owner=${encodeURIComponent(
            connectedUsername,
          )}&name=${encodeURIComponent(repoValidation.parsedName)}`,
          { signal: controller.signal },
        );

        if (requestId !== checkCounterRef.current) return;

        if (res.status === "available") {
          setAvailabilityStatus("available");
          setAvailabilityMessage(res.message);
          setAvailabilityDetail(null);
        } else if (res.status === "taken") {
          setAvailabilityStatus("taken");
          setAvailabilityMessage(res.message);
          setAvailabilityDetail(null);
        } else if (res.status === "unknown") {
          setAvailabilityStatus("unknown");
          setAvailabilityMessage(res.message);
          setAvailabilityDetail(res.detail || null);
        } else if (res.status === "invalid") {
          setAvailabilityStatus("invalid");
          setAvailabilityMessage(res.message);
          setAvailabilityDetail(null);
        } else {
          setAvailabilityStatus("error");
          setAvailabilityMessage(res.message || "Failed to check availability.");
          setAvailabilityDetail(res.detail || null);
        }
      } catch (err: unknown) {
        if (requestId !== checkCounterRef.current) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setAvailabilityStatus("error");
        const errMsg =
          err instanceof ApiProblemError && err.problem.detail
            ? err.problem.detail
            : err instanceof Error
              ? err.message
              : "Network error checking availability.";
        setAvailabilityMessage("Unable to check availability");
        setAvailabilityDetail(errMsg);
      }
    }, 400);

    return () => {
      clearTimeout(timer);
    };
  }, [
    githubMode,
    selectedStrategyOption.hasGitHub,
    isGitHubConnected,
    connectedUsername,
    repoValidation.error,
    repoValidation.parsedName,
    retryTrigger,
  ]);

  // Query available repositories for existing repository mode
  const repositoriesQuery = useQuery<RepositoryPage>({
    queryKey: queryKeys.integrations.githubRepositories(repoSearchQuery, 1, 50),
    queryFn: () =>
      api.get<RepositoryPage>(
        `/integrations/github/repositories?query=${encodeURIComponent(repoSearchQuery)}&page=1&per_page=50`,
      ),
    enabled: selectedStrategyOption.hasGitHub && githubMode === "existing" && !manualRepoEntry,
    retry: 1,
    staleTime: 30_000,
  });

  // Parse owner and repo name from selected repository
  const repoParts = githubRepoName.trim().split("/");
  const hasValidRepoFormat =
    repoParts.length === 2 && repoParts[0].length > 0 && repoParts[1].length > 0;
  const ghOwner = repoParts[0] || "";
  const ghRepo = repoParts[1] || "";

  // Query branches for existing repository
  const branchesQuery = useQuery({
    queryKey: ["github", "repositories", ghOwner, ghRepo, "branches"],
    queryFn: () =>
      api.get<{
        owner: string;
        repo: string;
        default_branch: string;
        branches: string[];
        can_push: boolean;
        is_private: boolean;
        truncated: boolean;
      }>(
        `/integrations/github/repositories/${encodeURIComponent(ghOwner)}/${encodeURIComponent(ghRepo)}/branches`,
      ),
    enabled: selectedStrategyOption.hasGitHub && hasValidRepoFormat && githubMode === "existing",
    retry: 1,
    staleTime: 30_000,
  });

  // Stale async response rejection: verify response owner/repo matches active selection
  const isBranchDataCurrent = Boolean(
    branchesQuery.data &&
    ghOwner &&
    ghRepo &&
    branchesQuery.data.owner?.toLowerCase() === ghOwner.toLowerCase() &&
    branchesQuery.data.repo?.toLowerCase() === ghRepo.toLowerCase(),
  );

  const currentBranches = useMemo(
    () => (isBranchDataCurrent ? branchesQuery.data?.branches || [] : []),
    [isBranchDataCurrent, branchesQuery.data?.branches],
  );
  const currentDefaultBranch = isBranchDataCurrent ? branchesQuery.data?.default_branch || "" : "";

  // When branches data arrives for the current repo, sync default_branch
  useEffect(() => {
    if (isBranchDataCurrent && currentDefaultBranch) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setGithubBranch((prev) => {
        if (
          !prev ||
          prev === "main" ||
          (currentBranches.length > 0 && !currentBranches.includes(prev))
        ) {
          return currentDefaultBranch;
        }
        return prev;
      });
    }
  }, [isBranchDataCurrent, currentDefaultBranch, currentBranches]);

  // Branch validation
  const branchValidationError =
    selectedStrategyOption.hasGitHub && githubBranch.trim()
      ? validateGitBranchName(githubBranch.trim())
      : null;

  const handleSelectRepository = (repo: RepositoryItem) => {
    setGithubRepoName(repo.full_name);
    setGithubBranch("");
    setBranchDropdownOpen(false);
    setRepoDropdownOpen(false);
  };

  const handleCreateRun = async () => {
    setIsSubmitting(true);
    setSubmitError(null);

    const idempotencyKey =
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
            const r = (Math.random() * 16) | 0;
            return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
          });

    const payload: Record<string, unknown> = {
      strategy,
      idempotency_key: idempotencyKey,
    };

    if (selectedStrategyOption.hasGitHub) {
      if (
        publishingMode === "pull_request" &&
        (githubMode === "new_private" || branchMode === "new")
      ) {
        setSubmitError("Pull requests require an existing repository and an existing base branch.");
        setIsSubmitting(false);
        return;
      }

      let finalRepoName = githubRepoName.trim();
      if (githubMode === "new_private") {
        if (!isGitHubConnected || !connectedUsername) {
          setSubmitError(
            "No GitHub account connected. Please connect your account in Settings → Integrations.",
          );
          setIsSubmitting(false);
          return;
        }
        if (repoValidation.error) {
          setSubmitError(repoValidation.error);
          setIsSubmitting(false);
          return;
        }
        if (availabilityStatus !== "available" && availabilityStatus !== "unknown") {
          setSubmitError(
            availabilityStatus === "taken"
              ? `A repository named '${repoValidation.canonicalName}' already exists under your account.`
              : "Repository name availability must be verified before proceeding.",
          );
          setIsSubmitting(false);
          return;
        }
        finalRepoName = repoValidation.canonicalName!;
      }

      payload.github_config = {
        repository_mode: githubMode,
        repository_visibility: repositoryVisibility,
        branch_mode: branchMode,
        repository_name: finalRepoName,
        target_branch: githubBranch.trim() || "main",
        publishing_mode: publishingMode,
        commit_message: githubCommitMessage.trim() || "Automated deployment by ForgeOps",
      };
    }

    if (selectedStrategyOption.hasVercel) {
      payload.vercel_config = {
        project_name: vercelProjectName.trim(),
        production_deploy: vercelProduction,
      };
    }

    if (selectedStrategyOption.hasDocker) {
      const parsedHPort = parseInt(hostPort.trim() || "8080", 10);
      const parsedCPort = parseInt(containerPort.trim() || "8080", 10);
      const finalHPort = isNaN(parsedHPort) ? 8080 : parsedHPort;
      const finalCPort = isNaN(parsedCPort) ? 8080 : parsedCPort;
      payload.docker_config = {
        host_port: finalHPort,
        container_port: finalCPort,
        port_bindings: {
          [String(finalCPort)]: finalHPort,
        },
      };
    }

    try {
      const data = await api.post<AutonomousRunResponse>(
        `/projects/${projectId}/autonomous-deploy`,
        payload,
      );

      if (onSuccess) {
        onSuccess(data);
      } else {
        router.push(`/projects/${projectId}/autonomous-deploy/${data.id}`);
      }
    } catch (err: unknown) {
      const msg =
        err instanceof ApiProblemError && err.problem.detail
          ? err.problem.detail
          : err instanceof Error
            ? err.message
            : "Failed to initialize autonomous deployment run.";
      setSubmitError(msg);
      setIsSubmitting(false);
    }
  };

  const isPrModeInvalid =
    publishingMode === "pull_request" && (githubMode === "new_private" || branchMode === "new");

  const isNewRepoMode = selectedStrategyOption.hasGitHub && githubMode === "new_private";
  const isNewRepoValid =
    !isNewRepoMode ||
    (isGitHubConnected &&
      !repoValidation.error &&
      (availabilityStatus === "available" || availabilityStatus === "unknown"));

  const isBranchValid =
    !selectedStrategyOption.hasGitHub || (githubBranch.trim().length > 0 && !branchValidationError);

  const isExistingRepoValid =
    !selectedStrategyOption.hasGitHub ||
    githubMode !== "existing" ||
    githubRepoName.trim().length > 0;

  const isFormValid =
    !isSubmitting &&
    Boolean(projectId) &&
    !isPrModeInvalid &&
    isNewRepoValid &&
    isExistingRepoValid &&
    isBranchValid &&
    (!selectedStrategyOption.hasVercel || vercelProjectName.trim().length > 0);

  const displayedBranches = currentBranches.filter((b) =>
    branchSearchFilter.trim()
      ? b.toLowerCase().includes(branchSearchFilter.trim().toLowerCase())
      : true,
  );

  return (
    <div className="space-y-6 w-full">
      {/* Agent Pairing Banner */}
      <div
        data-testid="agent-pairing-banner"
        className={`rounded-xl border p-4 text-xs transition-colors ${
          devicesQuery.isLoading
            ? "border-border bg-muted/40 text-muted-foreground"
            : isAgentConnected
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200"
              : "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200"
        }`}
      >
        <div className="flex items-start gap-3">
          {devicesQuery.isLoading ? (
            <Loader2 className="h-4 w-4 animate-spin shrink-0 mt-0.5" />
          ) : isAgentConnected ? (
            <CheckCircle2 className="h-4 w-4 text-emerald-600 dark:text-emerald-400 shrink-0 mt-0.5" />
          ) : (
            <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
          )}
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-sm">
                {devicesQuery.isLoading
                  ? "Checking agent connection..."
                  : isAgentConnected
                    ? "Agent Connected"
                    : "Agent Disconnected"}
              </span>
              <Badge
                variant={isAgentConnected ? "default" : "outline"}
                className="text-[10px] px-2 py-0.5 font-medium"
              >
                {isAgentConnected ? "Active Pairing" : "Pairing Required"}
              </Badge>
            </div>
            <p className="text-xs leading-relaxed opacity-90">
              {isAgentConnected
                ? `Active agent detected (${activeAgent?.platform}, v${activeAgent?.agent_version}). Local project workspace is accessible for automated execution.`
                : "An active connected agent is required to access local project files during execution. You may configure and launch the pipeline run now; execution begins as soon as an agent is paired."}
            </p>
          </div>
        </div>
      </div>

      {/* Deployment Strategy Selection */}
      <div className="rounded-xl border border-border bg-card p-6 shadow-sm space-y-4">
        <div>
          <h2 className="text-base font-semibold text-foreground">1. Deployment Strategy</h2>
          <p className="text-xs text-muted-foreground mt-0.5">
            Choose the orchestration pipeline components to execute for this project.
          </p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {STRATEGIES.map((item) => {
            const isSelected = strategy === item.id;
            return (
              <button
                key={item.id}
                type="button"
                data-testid={`strategy-${item.id}`}
                onClick={() => setStrategy(item.id)}
                aria-pressed={isSelected}
                className={`flex flex-col items-start p-4 rounded-lg border text-left transition-all ${
                  isSelected
                    ? "border-primary bg-primary/5 ring-2 ring-primary text-foreground shadow-sm"
                    : "border-border hover:bg-muted/40 text-muted-foreground hover:text-foreground"
                }`}
              >
                <div className="flex items-center justify-between w-full">
                  <span className="font-semibold text-sm text-foreground">{item.title}</span>
                  <Badge variant={isSelected ? "default" : "secondary"}>{item.badge}</Badge>
                </div>
                <p className="text-xs text-muted-foreground mt-2 leading-relaxed">
                  {item.description}
                </p>
              </button>
            );
          })}
        </div>
      </div>

      {/* Error alert */}
      {submitError && (
        <div
          data-testid="autonomous-deploy-error"
          className="rounded-xl border border-destructive/40 bg-destructive/10 p-4 text-xs text-destructive flex items-start gap-2.5"
          role="alert"
        >
          <AlertTriangle className="size-4 shrink-0 mt-0.5" />
          <div>
            <div className="font-semibold">Deployment Submission Failed</div>
            <div className="mt-1 leading-relaxed">{submitError}</div>
          </div>
        </div>
      )}

      {/* Dynamic Strategy-Specific Sections */}
      <div className="space-y-6">
        {/* GitHub Configuration Section */}
        {selectedStrategyOption.hasGitHub && (
          <section
            data-testid="github-config-section"
            aria-labelledby="github-config-heading"
            className="rounded-xl border border-border bg-card p-6 shadow-sm space-y-5"
          >
            <div className="flex items-center gap-2 border-b border-border/60 pb-3">
              <GitBranch className="h-5 w-5 text-primary" />
              <div>
                <h3 id="github-config-heading" className="text-base font-semibold text-foreground">
                  2. GitHub Configuration
                </h3>
                <p className="text-xs text-muted-foreground mt-0.5">
                  Configure repository publishing, branch targets, and commit history.
                </p>
              </div>
            </div>

            {/* Repository Mode Selection */}
            <div className="space-y-2">
              <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Repository Mode
              </label>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <button
                  type="button"
                  data-testid="repo-mode-new-private"
                  onClick={() => {
                    handleSetGithubMode("new_private");
                    setGithubRepoName(projectName.toLowerCase().replace(/[^a-z0-9-]/g, "-"));
                  }}
                  className={`p-3.5 rounded-lg border text-xs font-medium text-left transition-colors ${
                    githubMode === "new_private"
                      ? "border-primary bg-primary/10 text-primary font-semibold ring-1 ring-primary"
                      : "border-border text-muted-foreground hover:bg-muted"
                  }`}
                >
                  <div className="font-semibold text-sm">New Private Repository</div>
                  <div className="text-[11px] opacity-80 mt-1">
                    ForgeOps creates a new private repository under your account
                  </div>
                </button>
                <button
                  type="button"
                  data-testid="repo-mode-existing"
                  onClick={() => handleSetGithubMode("existing")}
                  className={`p-3.5 rounded-lg border text-xs font-medium text-left transition-colors ${
                    githubMode === "existing"
                      ? "border-primary bg-primary/10 text-primary font-semibold ring-1 ring-primary"
                      : "border-border text-muted-foreground hover:bg-muted"
                  }`}
                >
                  <div className="font-semibold text-sm">Existing Repository</div>
                  <div className="text-[11px] opacity-80 mt-1">
                    Select an existing repository accessible to your linked account
                  </div>
                </button>
              </div>
            </div>

            {/* Publishing Mode Selection */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Publishing Mode
                </label>
                {isPrDisabled && (
                  <span
                    data-testid="pr-disabled-explanation"
                    className="text-[11px] text-amber-700 dark:text-amber-400 font-medium"
                  >
                    Pull requests require an existing repository and an existing base branch.
                  </span>
                )}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <button
                  type="button"
                  data-testid="publishing-mode-direct-push"
                  onClick={() => setPublishingMode("direct_push")}
                  className={`p-3.5 rounded-lg border text-xs font-medium text-left transition-colors ${
                    publishingMode === "direct_push"
                      ? "border-primary bg-primary/10 text-primary font-semibold ring-1 ring-primary"
                      : "border-border text-muted-foreground hover:bg-muted"
                  }`}
                >
                  <div className="font-semibold text-sm">Direct Push</div>
                  <div className="text-[11px] opacity-80 mt-1">
                    Pushes commits directly to the selected target branch
                  </div>
                </button>
                <button
                  type="button"
                  data-testid="publishing-mode-pull-request"
                  disabled={isPrDisabled}
                  aria-disabled={isPrDisabled}
                  onClick={() => {
                    if (!isPrDisabled) {
                      setPublishingMode("pull_request");
                    }
                  }}
                  className={`p-3.5 rounded-lg border text-xs font-medium text-left transition-colors ${
                    isPrDisabled
                      ? "border-border/50 bg-muted/30 text-muted-foreground/60 cursor-not-allowed opacity-60"
                      : publishingMode === "pull_request"
                        ? "border-primary bg-primary/10 text-primary font-semibold ring-1 ring-primary"
                        : "border-border text-muted-foreground hover:bg-muted"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <div className="font-semibold text-sm">Create Pull Request</div>
                    {isPrDisabled && (
                      <Badge variant="outline" className="text-[10px] text-muted-foreground py-0">
                        Disabled
                      </Badge>
                    )}
                  </div>
                  <div className="text-[11px] opacity-80 mt-1">
                    Pushes to a temporary branch and opens an automated PR
                  </div>
                </button>
              </div>
              {isPrDisabled ? (
                <p
                  data-testid="pr-disabled-message"
                  className="text-xs text-muted-foreground bg-muted/30 p-2.5 rounded-md border border-border/50 mt-1"
                >
                  Pull requests require an existing repository and an existing base branch.
                </p>
              ) : publishingMode === "pull_request" ? (
                <p className="text-xs text-muted-foreground bg-muted/40 p-2.5 rounded-md border border-border/40 mt-1">
                  Pushes changes to a dedicated temporary branch{" "}
                  <code className="font-mono text-[11px] bg-muted px-1.5 py-0.5 rounded font-medium">
                    forgeops/deploy-&lt;run_id&gt;
                  </code>{" "}
                  and opens a Pull Request targeting your selected base branch.
                </p>
              ) : null}
            </div>

            {/* Repository Selection */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label htmlFor="github-repo-name" className="text-xs font-medium text-foreground">
                  {githubMode === "new_private" ? "New Repository Name" : "GitHub Repository"}
                </label>
                {githubMode === "existing" && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-6 text-xs text-muted-foreground hover:text-foreground"
                    onClick={() => setManualRepoEntry((prev) => !prev)}
                  >
                    {manualRepoEntry ? "Pick from account list" : "Enter repository manually"}
                  </Button>
                )}
              </div>

              {githubMode === "new_private" ? (
                <div className="space-y-2">
                  {githubStatusQuery.isLoading ? (
                    <div
                      data-testid="repo-owner-resolving"
                      className="flex items-center gap-1.5 text-xs text-muted-foreground"
                    >
                      <Loader2 className="size-3.5 animate-spin" />
                      <span>Resolving connected GitHub account...</span>
                    </div>
                  ) : !isGitHubConnected ? (
                    <div
                      data-testid="github-not-connected-warning"
                      className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200 space-y-1"
                    >
                      <div className="font-semibold flex items-center gap-1.5">
                        <AlertTriangle className="size-4 text-amber-600 dark:text-amber-400" />
                        No GitHub account connected
                      </div>
                      <p>
                        Creating a repository requires an authenticated GitHub connection. Please
                        connect your GitHub account in{" "}
                        <Link
                          href="/settings/integrations"
                          className="underline font-semibold hover:text-foreground"
                        >
                          Settings → Integrations
                        </Link>
                        .
                      </p>
                    </div>
                  ) : (
                    <div
                      data-testid="repo-owner-indicator"
                      className="flex items-center gap-1.5 text-xs text-muted-foreground font-mono"
                    >
                      <span>Repository owner:</span>
                      <span className="font-semibold text-foreground">@{connectedUsername}</span>
                    </div>
                  )}

                  <Input
                    id="github-repo-name"
                    aria-label="Repository Name"
                    data-testid="github-repo-input"
                    value={githubRepoName}
                    onChange={(e) => {
                      setGithubRepoName(e.target.value);
                      setGithubBranch("main");
                    }}
                    placeholder="e.g. my-new-repo"
                    className="text-sm font-mono"
                  />

                  {/* Repository Visibility Selection */}
                  <div className="space-y-1.5" data-testid="github-visibility-container">
                    <label
                      htmlFor="repo-visibility-select"
                      className="text-xs font-medium text-foreground"
                    >
                      Repository Visibility
                    </label>
                    <select
                      id="repo-visibility-select"
                      data-testid="github-visibility-select"
                      value={repositoryVisibility}
                      onChange={(e) =>
                        setRepositoryVisibility(e.target.value as "private" | "public")
                      }
                      className="w-full h-9 rounded-md border border-input bg-background px-3 py-1 text-xs shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
                    >
                      <option value="private">Private (Default)</option>
                      <option value="public">Public</option>
                    </select>
                  </div>

                  {/* Availability and validation feedback */}
                  {isGitHubConnected && (
                    <div>
                      {availabilityStatus === "checking" && (
                        <div
                          data-testid="repo-availability-checking"
                          className="flex items-center gap-1.5 text-xs text-muted-foreground mt-1"
                        >
                          <Loader2 className="size-3.5 animate-spin text-primary" />
                          <span>Checking availability…</span>
                        </div>
                      )}

                      {availabilityStatus === "available" && (
                        <div className="space-y-1 mt-1">
                          <div
                            data-testid="repo-availability-available"
                            className="flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400 font-medium"
                          >
                            <CheckCircle2 className="size-3.5" />
                            <span>Available</span>
                          </div>
                          {repoValidation.canonicalName && (
                            <div
                              data-testid="canonical-repo-preview"
                              className="text-[11px] text-muted-foreground font-mono"
                            >
                              Repository to create:{" "}
                              <span className="font-semibold text-foreground">
                                {repoValidation.canonicalName}
                              </span>
                            </div>
                          )}
                        </div>
                      )}

                      {availabilityStatus === "taken" && (
                        <div className="space-y-1.5 mt-1" data-testid="repo-taken-message">
                          <div
                            data-testid="repo-availability-taken"
                            className="flex items-center gap-1.5 text-xs text-destructive font-medium"
                          >
                            <AlertTriangle className="size-3.5" />
                            <span>Already exists / Name unavailable</span>
                          </div>
                          <p className="text-xs text-muted-foreground bg-destructive/10 border border-destructive/30 rounded p-2">
                            A repository named{" "}
                            <code className="font-mono font-semibold">
                              {repoValidation.canonicalName || githubRepoName}
                            </code>{" "}
                            already exists under your account. A new repository cannot overwrite an
                            existing one. Please choose a different name or{" "}
                            <button
                              type="button"
                              data-testid="switch-to-existing-btn"
                              onClick={() => {
                                handleSetGithubMode("existing");
                                setGithubRepoName(repoValidation.canonicalName || githubRepoName);
                              }}
                              className="underline font-semibold text-foreground hover:text-primary"
                            >
                              switch to Existing Repository mode
                            </button>
                            .
                          </p>
                        </div>
                      )}

                      {availabilityStatus === "invalid" && (
                        <div className="space-y-1 mt-1">
                          <div
                            data-testid="repo-availability-invalid"
                            className="flex items-center gap-1.5 text-xs text-destructive font-medium"
                          >
                            <AlertTriangle className="size-3.5" />
                            <span>Invalid repository name</span>
                          </div>
                          {repoValidation.error && (
                            <p
                              data-testid="repo-name-validation-error"
                              className="text-[11px] text-destructive"
                            >
                              {repoValidation.error}
                            </p>
                          )}
                        </div>
                      )}

                      {availabilityStatus === "unknown" && (
                        <div className="space-y-1.5 mt-1" data-testid="repo-availability-unknown">
                          <div className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400 font-medium">
                            <AlertTriangle className="size-3.5" />
                            <span>Availability unconfirmed</span>
                          </div>
                          <p className="text-xs text-muted-foreground bg-muted/40 border border-border/40 rounded p-2">
                            {availabilityMessage ||
                              "GitHub could not confirm whether this name is available. ForgeOps will attempt creation during deployment."}
                            {availabilityDetail && (
                              <span className="block mt-1 font-mono text-[11px] text-muted-foreground">
                                {availabilityDetail}
                              </span>
                            )}
                          </p>
                          {repoValidation.canonicalName && (
                            <div
                              data-testid="canonical-repo-preview"
                              className="text-[11px] text-muted-foreground font-mono"
                            >
                              Repository to create:{" "}
                              <span className="font-semibold text-foreground">
                                {repoValidation.canonicalName}
                              </span>
                            </div>
                          )}
                        </div>
                      )}

                      {availabilityStatus === "error" && (
                        <div className="space-y-1.5 mt-1" data-testid="repo-availability-error">
                          <div className="flex items-center gap-2 text-xs text-amber-700 dark:text-amber-400">
                            <span>{availabilityMessage || "Unable to check availability"} —</span>
                            <button
                              type="button"
                              data-testid="retry-availability-button"
                              onClick={() => setRetryTrigger((c) => c + 1)}
                              className="underline font-semibold hover:text-foreground flex items-center gap-1"
                            >
                              <RefreshCw className="size-3" /> Retry
                            </button>
                          </div>
                          {availabilityDetail && (
                            <p className="text-[11px] text-muted-foreground font-mono bg-muted/30 border border-border/40 rounded px-2 py-1">
                              {availabilityDetail}
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  )}

                  <p className="text-[11px] text-muted-foreground mt-1">
                    Repository will be created as a private repo under your linked account.
                  </p>
                </div>
              ) : manualRepoEntry ? (
                <div>
                  <Input
                    id="github-repo-name"
                    aria-label="Repository Name"
                    data-testid="github-repo-input"
                    value={githubRepoName}
                    onChange={(e) => {
                      setGithubRepoName(e.target.value);
                      setGithubBranch("main");
                    }}
                    placeholder="e.g. owner/repo-name"
                    className="text-sm font-mono"
                  />
                  <p className="text-[11px] text-muted-foreground mt-1">
                    Specify the full owner/name format for your existing repository.
                  </p>
                </div>
              ) : (
                /* Auto-fetched repository dropdown/combobox */
                <div className="relative space-y-2" data-testid="repo-combobox-container">
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        id="github-repo-name"
                        aria-label="Repository Name"
                        data-testid="github-repo-combobox-input"
                        value={githubRepoName}
                        onChange={(e) => {
                          setGithubRepoName(e.target.value);
                          setRepoSearchQuery(e.target.value);
                          setRepoDropdownOpen(true);
                        }}
                        onFocus={() => setRepoDropdownOpen(true)}
                        placeholder="Search accessible repositories (e.g. owner/repo)..."
                        className="text-sm font-mono pr-8"
                      />
                      <button
                        type="button"
                        aria-label="Toggle repository dropdown"
                        onClick={() => setRepoDropdownOpen((prev) => !prev)}
                        className="absolute right-2 top-2.5 text-muted-foreground hover:text-foreground"
                      >
                        <ChevronDown className="size-4" />
                      </button>
                    </div>
                    {repositoriesQuery.isFetching && (
                      <div className="flex items-center px-2 text-muted-foreground text-xs">
                        <Loader2 className="size-4 animate-spin" />
                      </div>
                    )}
                  </div>

                  {/* Dropdown Menu */}
                  {repoDropdownOpen && (
                    <div
                      data-testid="repo-dropdown-menu"
                      className="absolute z-30 w-full mt-1 bg-popover text-popover-foreground border border-border rounded-lg shadow-lg overflow-hidden max-h-64 overflow-y-auto"
                    >
                      {repositoriesQuery.isLoading ? (
                        <div className="p-4 text-xs text-muted-foreground flex items-center gap-2">
                          <Loader2 className="size-3.5 animate-spin" />
                          <span>Reading accessible repositories from GitHub...</span>
                        </div>
                      ) : repositoriesQuery.isError ? (
                        <div className="p-4 text-xs space-y-2">
                          <div className="text-destructive font-medium">
                            {repositoriesQuery.error instanceof ApiProblemError
                              ? repositoriesQuery.error.problem.detail ||
                                repositoriesQuery.error.problem.title
                              : "Could not retrieve GitHub repositories."}
                          </div>
                          <div className="flex items-center gap-2 pt-1">
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              onClick={() => repositoriesQuery.refetch()}
                              className="h-7 text-xs"
                            >
                              <RefreshCw className="size-3 mr-1" /> Retry
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              onClick={() => setManualRepoEntry(true)}
                              className="h-7 text-xs"
                            >
                              Enter manually
                            </Button>
                          </div>
                        </div>
                      ) : !repositoriesQuery.data?.items ||
                        repositoriesQuery.data.items.length === 0 ? (
                        <div className="p-4 text-xs text-muted-foreground space-y-1">
                          <p>
                            {repoSearchQuery
                              ? `No repository matches "${repoSearchQuery}".`
                              : "No accessible repositories found on your linked GitHub account."}
                          </p>
                          <p className="text-[11px] opacity-80">
                            You can{" "}
                            <button
                              type="button"
                              className="text-primary underline font-medium"
                              onClick={() => setManualRepoEntry(true)}
                            >
                              type the repository manually
                            </button>
                            .
                          </p>
                        </div>
                      ) : (
                        <ul className="divide-y divide-border/50 text-xs" role="listbox">
                          {repositoriesQuery.data.items.map((repo) => {
                            const isChosen = githubRepoName.trim() === repo.full_name;
                            return (
                              <li key={repo.full_name} role="option" aria-selected={isChosen}>
                                <button
                                  type="button"
                                  data-testid={`repo-select-${repo.full_name}`}
                                  onClick={() => handleSelectRepository(repo)}
                                  className={`w-full text-left p-3 flex items-center justify-between hover:bg-muted/60 transition-colors ${
                                    isChosen ? "bg-accent text-accent-foreground font-semibold" : ""
                                  }`}
                                >
                                  <div>
                                    <div className="font-mono text-xs font-semibold">
                                      {repo.full_name}
                                    </div>
                                    <div className="flex items-center gap-2 mt-1 text-[11px] text-muted-foreground">
                                      <span>default: {repo.default_branch}</span>
                                      {repo.language && <span>• {repo.language}</span>}
                                    </div>
                                  </div>
                                  <Badge variant="outline" className="text-[10px] py-0">
                                    {repo.private ? "private" : "public"}
                                  </Badge>
                                </button>
                              </li>
                            );
                          })}
                        </ul>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Branch Mode Selection */}
            {githubMode === "existing" && (
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Branch Mode
                </label>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <button
                    type="button"
                    data-testid="branch-mode-existing"
                    onClick={() => handleSetBranchMode("existing")}
                    className={`p-3.5 rounded-lg border text-xs font-medium text-left transition-colors ${
                      branchMode === "existing"
                        ? "border-primary bg-primary/10 text-primary font-semibold ring-1 ring-primary"
                        : "border-border text-muted-foreground hover:bg-muted"
                    }`}
                  >
                    <div className="font-semibold text-sm">Existing Branch</div>
                    <div className="text-[11px] opacity-80 mt-1">
                      Publish against a branch that already exists on GitHub
                    </div>
                  </button>
                  <button
                    type="button"
                    data-testid="branch-mode-new"
                    onClick={() => handleSetBranchMode("new")}
                    className={`p-3.5 rounded-lg border text-xs font-medium text-left transition-colors ${
                      branchMode === "new"
                        ? "border-primary bg-primary/10 text-primary font-semibold ring-1 ring-primary"
                        : "border-border text-muted-foreground hover:bg-muted"
                    }`}
                  >
                    <div className="font-semibold text-sm">New Branch</div>
                    <div className="text-[11px] opacity-80 mt-1">
                      Create and push to a new target branch
                    </div>
                  </button>
                </div>
              </div>
            )}

            {/* Target / Base Branch Selection */}
            <div className="space-y-1.5" ref={branchComboboxRef}>
              <div className="flex items-center justify-between">
                <label
                  htmlFor="github-target-branch"
                  className="text-xs font-medium text-foreground"
                >
                  {publishingMode === "pull_request"
                    ? "Base Branch (Destination for PR)"
                    : branchMode === "new"
                      ? "New Target Branch Name"
                      : "Target Branch (Direct Push)"}
                </label>
                {currentDefaultBranch && currentDefaultBranch === githubBranch && (
                  <Badge
                    variant="outline"
                    data-testid="default-branch-badge"
                    className="text-[10px] py-0 px-2 font-medium"
                  >
                    Default Branch: {currentDefaultBranch}
                  </Badge>
                )}
              </div>

              {githubMode === "existing" && branchMode === "existing" ? (
                /* Searchable Keyboard-Accessible Branch Combobox */
                <div className="relative space-y-2" data-testid="branch-combobox-container">
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        id="github-target-branch"
                        role="combobox"
                        aria-expanded={branchDropdownOpen}
                        aria-autocomplete="list"
                        aria-controls="github-branches-listbox"
                        aria-activedescendant={
                          highlightedBranchIndex >= 0
                            ? `branch-opt-${highlightedBranchIndex}`
                            : undefined
                        }
                        aria-label="Target Branch"
                        data-testid="github-branch-input"
                        value={githubBranch}
                        onChange={(e) => {
                          setGithubBranch(e.target.value);
                          setBranchSearchFilter(e.target.value);
                          setBranchDropdownOpen(true);
                          setHighlightedBranchIndex(-1);
                        }}
                        onFocus={() => {
                          setBranchSearchFilter("");
                          setBranchDropdownOpen(true);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "ArrowDown") {
                            e.preventDefault();
                            if (!branchDropdownOpen) {
                              setBranchDropdownOpen(true);
                            } else if (displayedBranches.length > 0) {
                              setHighlightedBranchIndex((prev) =>
                                prev < displayedBranches.length - 1 ? prev + 1 : 0,
                              );
                            }
                          } else if (e.key === "ArrowUp") {
                            e.preventDefault();
                            if (branchDropdownOpen && displayedBranches.length > 0) {
                              setHighlightedBranchIndex((prev) =>
                                prev > 0 ? prev - 1 : displayedBranches.length - 1,
                              );
                            }
                          } else if (e.key === "Enter") {
                            if (
                              branchDropdownOpen &&
                              highlightedBranchIndex >= 0 &&
                              displayedBranches[highlightedBranchIndex]
                            ) {
                              e.preventDefault();
                              setGithubBranch(displayedBranches[highlightedBranchIndex]);
                              setBranchSearchFilter("");
                              setBranchDropdownOpen(false);
                            }
                          } else if (e.key === "Escape") {
                            e.preventDefault();
                            setBranchDropdownOpen(false);
                          } else if (e.key === "Tab") {
                            setBranchDropdownOpen(false);
                          }
                        }}
                        placeholder={currentDefaultBranch || "main"}
                        className={`text-sm font-mono pr-8 ${
                          branchValidationError
                            ? "border-destructive focus-visible:ring-destructive"
                            : ""
                        }`}
                      />
                      <button
                        type="button"
                        aria-label="Toggle branch dropdown"
                        onClick={() => {
                          setBranchSearchFilter("");
                          setBranchDropdownOpen((prev) => !prev);
                        }}
                        className="absolute right-2 top-2.5 text-muted-foreground hover:text-foreground"
                      >
                        <ChevronDown className="size-4" />
                      </button>
                    </div>
                    {branchesQuery.isFetching && (
                      <div className="flex items-center px-2 text-muted-foreground text-xs">
                        <Loader2 className="size-4 animate-spin" />
                      </div>
                    )}
                  </div>

                  {/* Branch Dropdown Menu */}
                  {branchDropdownOpen && (
                    <div
                      id="github-branches-listbox"
                      role="listbox"
                      data-testid="branch-dropdown-menu"
                      className="absolute z-40 w-full mt-1 bg-popover text-popover-foreground border border-border rounded-lg shadow-lg overflow-hidden max-h-56 overflow-y-auto"
                    >
                      {branchesQuery.isLoading ? (
                        <div
                          data-testid="branch-discovery-loading"
                          className="p-3 text-xs text-muted-foreground flex items-center gap-2"
                        >
                          <Loader2 className="size-3.5 animate-spin" />
                          <span>Discovering repository branches from GitHub...</span>
                        </div>
                      ) : branchesQuery.isError ? (
                        <div className="p-3 text-xs space-y-2">
                          <div className="text-destructive font-medium">
                            Could not discover branches for {githubRepoName}.
                          </div>
                          <div className="flex items-center gap-2">
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              onClick={() => branchesQuery.refetch()}
                              className="h-7 text-xs"
                            >
                              <RefreshCw className="size-3 mr-1" /> Retry
                            </Button>
                          </div>
                        </div>
                      ) : displayedBranches.length === 0 ? (
                        <div className="p-3 text-xs text-muted-foreground space-y-1">
                          <p>
                            {branchSearchFilter
                              ? `No branches match "${branchSearchFilter}".`
                              : "No remote branches discovered in this repository."}
                          </p>
                          <p className="text-[11px] opacity-80">
                            You can keep typing to specify a custom branch name.
                          </p>
                        </div>
                      ) : (
                        <ul className="divide-y divide-border/50 text-xs">
                          {displayedBranches.map((branch, idx) => {
                            const isChosen = githubBranch.trim() === branch;
                            const isHighlighted = idx === highlightedBranchIndex;
                            const isDefault = branch === currentDefaultBranch;
                            return (
                              <li
                                key={branch}
                                id={`branch-opt-${idx}`}
                                role="option"
                                aria-selected={isChosen}
                              >
                                <button
                                  type="button"
                                  data-testid={`branch-select-${branch}`}
                                  onClick={() => {
                                    setGithubBranch(branch);
                                    setBranchSearchFilter("");
                                    setBranchDropdownOpen(false);
                                  }}
                                  className={`w-full text-left p-2.5 flex items-center justify-between hover:bg-muted/60 transition-colors ${
                                    isChosen
                                      ? "bg-accent text-accent-foreground font-semibold"
                                      : isHighlighted
                                        ? "bg-muted/50"
                                        : ""
                                  }`}
                                >
                                  <div className="flex items-center gap-2 overflow-hidden">
                                    <GitBranch className="size-3.5 shrink-0 text-muted-foreground" />
                                    <span className="font-mono text-xs truncate">{branch}</span>
                                  </div>
                                  {isDefault && (
                                    <Badge
                                      variant="outline"
                                      className="text-[10px] py-0 px-1.5 shrink-0"
                                    >
                                      default
                                    </Badge>
                                  )}
                                </button>
                              </li>
                            );
                          })}
                        </ul>
                      )}

                      {branchesQuery.data?.truncated && (
                        <div
                          data-testid="branches-truncated-notice"
                          className="p-2 text-[11px] text-amber-600 dark:text-amber-400 border-t border-border/50 bg-amber-500/10"
                        >
                          Repository contains &gt;1,000 branches; listing capped at 1,000.
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ) : (
                /* Simple branch input for New Repository or New Branch mode */
                <div>
                  <Input
                    id="github-target-branch"
                    aria-label="Target Branch"
                    data-testid="github-branch-input"
                    value={githubBranch}
                    onChange={(e) => setGithubBranch(e.target.value)}
                    placeholder={branchMode === "new" ? "e.g. feature/my-feature" : "main"}
                    className={`text-sm font-mono ${
                      branchValidationError
                        ? "border-destructive focus-visible:ring-destructive"
                        : ""
                    }`}
                  />
                  <p className="text-[11px] text-muted-foreground mt-1">
                    {branchMode === "new"
                      ? "Specify the name of the new branch to create on GitHub."
                      : "Default branch name for the new repository."}
                  </p>
                </div>
              )}

              {/* Branch discovery status for simple input mode */}
              {(githubMode !== "existing" || branchMode !== "existing") &&
                branchesQuery.isLoading && (
                  <p
                    data-testid="branch-discovery-loading"
                    className="text-[11px] text-muted-foreground flex items-center gap-1.5 mt-1"
                  >
                    <Loader2 className="size-3 animate-spin" /> Discovering repository branches...
                  </p>
                )}

              {branchValidationError && (
                <p
                  data-testid="branch-validation-error"
                  className="text-[11px] text-destructive font-medium mt-1 flex items-center gap-1"
                  role="alert"
                >
                  <AlertTriangle className="size-3" />
                  {branchValidationError}
                </p>
              )}
            </div>

            {/* Commit Message */}
            <div className="space-y-1.5">
              <label
                htmlFor="github-commit-message"
                className="text-xs font-medium text-foreground"
              >
                Commit Message
              </label>
              <Input
                id="github-commit-message"
                aria-label="Commit Message"
                data-testid="github-commit-message-input"
                value={githubCommitMessage}
                onChange={(e) => setGithubCommitMessage(e.target.value)}
                placeholder="Automated deployment by ForgeOps"
                className="text-sm"
              />
            </div>
          </section>
        )}

        {/* Vercel Configuration Section */}
        {selectedStrategyOption.hasVercel && (
          <section
            data-testid="vercel-config-section"
            aria-labelledby="vercel-config-heading"
            className="rounded-xl border border-border bg-card p-6 shadow-sm space-y-4"
          >
            <div className="flex items-center gap-2 border-b border-border/60 pb-3">
              <Globe className="h-5 w-5 text-primary" />
              <div>
                <h3 id="vercel-config-heading" className="text-base font-semibold text-foreground">
                  3. Vercel Configuration
                </h3>
                <p className="text-xs text-muted-foreground mt-0.5">
                  Set up Vercel project target and deployment tier.
                </p>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-end">
              <div className="space-y-1.5">
                <label
                  htmlFor="vercel-project-name"
                  className="text-xs font-medium text-foreground"
                >
                  Vercel Project Name
                </label>
                <Input
                  id="vercel-project-name"
                  aria-label="Vercel Project Name"
                  data-testid="vercel-project-input"
                  value={vercelProjectName}
                  onChange={(e) => setVercelProjectName(e.target.value)}
                  placeholder="e.g. my-app-frontend"
                  className="text-sm font-mono"
                />
              </div>

              <div className="flex items-center gap-2.5 pb-2">
                <input
                  type="checkbox"
                  id="vercel-production-deploy"
                  aria-label="Deploy to Production"
                  data-testid="vercel-production-checkbox"
                  checked={vercelProduction}
                  onChange={(e) => setVercelProduction(e.target.checked)}
                  className="h-4 w-4 rounded border-border text-primary focus:ring-primary"
                />
                <label
                  htmlFor="vercel-production-deploy"
                  className="text-xs font-medium text-foreground cursor-pointer select-none"
                >
                  Deploy directly to Production (default: on)
                </label>
              </div>
            </div>
          </section>
        )}

        {/* Docker Configuration Section */}
        {selectedStrategyOption.hasDocker && (
          <section
            data-testid="docker-config-section"
            aria-labelledby="docker-config-heading"
            className="rounded-xl border border-border bg-card p-6 shadow-sm space-y-4"
          >
            <div className="flex items-center gap-2 border-b border-border/60 pb-3">
              <Box className="h-5 w-5 text-primary" />
              <div>
                <h3 id="docker-config-heading" className="text-base font-semibold text-foreground">
                  4. Docker Configuration
                </h3>
                <p className="text-xs text-muted-foreground mt-0.5">
                  Configure local container runtime and host machine port bindings.
                </p>
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-xs font-medium text-foreground">
                Port Bindings (Host Port : Container Port)
              </label>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div className="space-y-1">
                  <label htmlFor="docker-host-port" className="text-[11px] text-muted-foreground">
                    Host Port
                  </label>
                  <Input
                    id="docker-host-port"
                    aria-label="Host Port"
                    data-testid="docker-host-port-input"
                    value={hostPort}
                    onChange={(e) => setHostPort(e.target.value)}
                    placeholder="8080"
                    className="text-sm font-mono"
                  />
                </div>
                <div className="space-y-1">
                  <label
                    htmlFor="docker-container-port"
                    className="text-[11px] text-muted-foreground"
                  >
                    Container Port
                  </label>
                  <Input
                    id="docker-container-port"
                    aria-label="Container Port"
                    data-testid="docker-container-port-input"
                    value={containerPort}
                    onChange={(e) => setContainerPort(e.target.value)}
                    placeholder="8080"
                    className="text-sm font-mono"
                  />
                </div>
              </div>
              <p className="text-[11px] text-muted-foreground">
                Binds container runtime service port to target host machine port.
              </p>
            </div>
          </section>
        )}
      </div>

      {/* Form Submission Actions */}
      <div className="flex items-center justify-between border-t border-border pt-6 pb-12">
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            if (onCancel) {
              onCancel();
            } else {
              router.push(`/projects/${projectId}`);
            }
          }}
          disabled={isSubmitting}
        >
          Cancel
        </Button>

        <Button
          type="button"
          variant="default"
          data-testid="create-run-button"
          disabled={!isFormValid}
          onClick={handleCreateRun}
          className="font-medium shadow-sm bg-primary text-primary-foreground hover:bg-primary/90 px-6"
        >
          {isSubmitting ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
              Initializing Pipeline...
            </>
          ) : (
            "Start Autonomous Deployment"
          )}
        </Button>
      </div>
    </div>
  );
}
