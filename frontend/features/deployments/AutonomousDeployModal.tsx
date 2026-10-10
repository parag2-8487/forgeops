// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useEffect, useId, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api, ApiProblemError } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  AlertTriangle,
  Box,
  CheckCircle2,
  GitBranch,
  Globe,
  Loader2,
  Server,
  X,
} from "lucide-react";

export type DeploymentStrategy =
  "docker_github_vercel" | "docker_github" | "github_only" | "vercel_only";

interface StrategyOption {
  id: DeploymentStrategy;
  title: string;
  badge: string;
  description: string;
  hasDocker: boolean;
  hasGitHub: boolean;
  hasVercel: boolean;
}

const STRATEGIES: StrategyOption[] = [
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

export interface AutonomousDeployModalProps {
  projectId: string;
  projectName: string;
  isOpen: boolean;
  onClose: () => void;
}

export function AutonomousDeployModal({
  projectId,
  projectName,
  isOpen,
  onClose,
}: AutonomousDeployModalProps) {
  const router = useRouter();

  const [strategy, setStrategy] = useState<DeploymentStrategy>("docker_github_vercel");

  // GitHub config state
  const [githubMode, setGithubMode] = useState<"existing" | "new_private">("new_private");
  const [publishingMode, setPublishingMode] = useState<"direct_push" | "pull_request">(
    "direct_push",
  );
  const [githubRepoName, setGithubRepoName] = useState(() =>
    projectName.toLowerCase().replace(/[^a-z0-9-]/g, "-"),
  );
  const [githubBranch, setGithubBranch] = useState("main");
  const [githubCommitMessage, setGithubCommitMessage] = useState(
    "Automated deployment by ForgeOps",
  );

  // Vercel config state
  const [vercelProjectName, setVercelProjectName] = useState(() =>
    projectName.toLowerCase().replace(/[^a-z0-9-]/g, "-"),
  );
  const [vercelProduction, setVercelProduction] = useState(true);

  // Docker config state
  const [hostPort, setHostPort] = useState("8080");
  const [containerPort, setContainerPort] = useState("8080");

  // Submission state
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Reset form when project changes or opened
  useEffect(() => {
    if (isOpen) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSubmitError(null);
      setIsSubmitting(false);
    }
  }, [isOpen, projectId]);

  // Query paired agent status for this project
  const devicesQuery = useQuery({
    queryKey: ["agents", "devices", projectId],
    queryFn: () => api.get<DevicePage>(`/agents/devices?project_id=${projectId}`),
    enabled: isOpen,
    refetchInterval: isOpen ? 10_000 : false,
    retry: false,
  });

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
    enabled: isOpen && strategy !== "vercel_only" && hasValidRepoFormat,
    retry: 1,
  });

  // When branches data arrives, sync default_branch if branch is default main or empty
  useEffect(() => {
    if (branchesQuery.data?.default_branch) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setGithubBranch((prev) =>
        !prev || prev === "main" ? branchesQuery.data.default_branch : prev,
      );
    }
  }, [branchesQuery.data?.default_branch]);

  const activeAgent = devicesQuery.data?.devices?.find(
    (d) =>
      d.status === "active" &&
      (d.heartbeat_fresh === true ||
        (d.seconds_since_last_seen !== null && d.seconds_since_last_seen <= 30)),
  );
  const isAgentConnected = Boolean(activeAgent);

  const selectedStrategyOption = STRATEGIES.find((s) => s.id === strategy) || STRATEGIES[0];

  const handleCreateRun = async () => {
    setIsSubmitting(true);
    setSubmitError(null);

    // Generate random UUID idempotency key
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
      payload.github_config = {
        repository_mode: githubMode,
        repository_name: githubRepoName.trim(),
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
      const hPort = hostPort.trim() || "8080";
      const parsedCPort = parseInt(containerPort.trim() || "8080", 10);
      payload.docker_config = {
        port_bindings: {
          [hPort]: isNaN(parsedCPort) ? 8080 : parsedCPort,
        },
      };
    }

    try {
      // POST /api/v1/projects/${projectId}/autonomous-deploy
      const data = await api.post<AutonomousRunResponse>(
        `/projects/${projectId}/autonomous-deploy`,
        payload,
      );

      // Handle both 201 Created and idempotent 200 OK responses by routing to dedicated pipeline page
      onClose();
      router.push(`/projects/${projectId}/autonomous-deploy/${data.id}`);
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

  if (!isOpen) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="autonomous-deploy-modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm p-4 overflow-y-auto overscroll-contain"
    >
      <div className="relative w-full max-w-3xl rounded-xl border border-border bg-card p-6 shadow-xl space-y-6 overscroll-contain my-8">
        {/* Header */}
        <div className="flex items-start justify-between border-b border-border pb-4">
          <div>
            <h2 id="autonomous-deploy-modal-title" className="text-xl font-bold tracking-tight">
              Autonomous Deployment Orchestrator
            </h2>
            <p className="text-xs text-muted-foreground mt-1">
              Select an orchestration strategy and configure automated deployment for project{" "}
              <span className="font-semibold text-foreground">{projectName}</span>.
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={onClose}
            aria-label="Close dialog"
            className="h-8 w-8 p-0"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>

        {/* Agent Pairing Banner */}
        <div
          data-testid="agent-pairing-banner"
          className={`rounded-lg border p-3.5 text-xs transition-colors ${
            devicesQuery.isLoading
              ? "border-border bg-muted/40 text-muted-foreground"
              : isAgentConnected
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
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
                  className="text-[10px] px-1.5 py-0"
                >
                  {isAgentConnected ? "Active Pairing" : "Pairing Required"}
                </Badge>
              </div>
              <p className="text-xs leading-relaxed opacity-90">
                {isAgentConnected
                  ? `Active agent detected (${activeAgent?.platform}, v${activeAgent?.agent_version}). Local project workspace is accessible for execution.`
                  : "An active connected agent is required to access local project files during execution. You may create the pipeline run now, but starting execution requires an active connected agent."}
              </p>
            </div>
          </div>
        </div>

        {/* Strategy Selection Cards */}
        <div className="space-y-3">
          <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Select Deployment Strategy
          </label>
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
                      ? "border-primary bg-primary/5 ring-1 ring-primary text-foreground shadow-sm"
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

        {/* Error message */}
        {submitError && (
          <div
            data-testid="autonomous-deploy-error"
            className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive"
          >
            {submitError}
          </div>
        )}

        {/* Dynamic Context-Aware Config Sections */}
        <div className="space-y-5 border-t border-border pt-4">
          {/* GitHub Config Section */}
          {selectedStrategyOption.hasGitHub && (
            <section
              data-testid="github-config-section"
              aria-labelledby="github-config-heading"
              className="rounded-lg border border-border bg-card/60 p-4 space-y-4"
            >
              <div className="flex items-center gap-2 border-b border-border/60 pb-2">
                <GitBranch className="h-4 w-4 text-primary" />
                <h3 id="github-config-heading" className="text-sm font-semibold text-foreground">
                  GitHub Configuration
                </h3>
              </div>

              {/* Repository Mode */}
              <div className="space-y-2">
                <label className="text-xs font-medium text-foreground">Repository Mode</label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => setGithubMode("new_private")}
                    className={`p-2.5 rounded border text-xs font-medium text-left transition-colors ${
                      githubMode === "new_private"
                        ? "border-primary bg-primary/10 text-primary"
                        : "border-border text-muted-foreground hover:bg-muted"
                    }`}
                  >
                    New Private Repository
                  </button>
                  <button
                    type="button"
                    onClick={() => setGithubMode("existing")}
                    className={`p-2.5 rounded border text-xs font-medium text-left transition-colors ${
                      githubMode === "existing"
                        ? "border-primary bg-primary/10 text-primary"
                        : "border-border text-muted-foreground hover:bg-muted"
                    }`}
                  >
                    Existing Repository
                  </button>
                </div>
              </div>

              {/* Publishing Mode */}
              <div className="space-y-2">
                <label className="text-xs font-medium text-foreground">Publishing Mode</label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    data-testid="publishing-mode-direct-push"
                    onClick={() => setPublishingMode("direct_push")}
                    className={`p-2.5 rounded border text-xs font-medium text-left transition-colors ${
                      publishingMode === "direct_push"
                        ? "border-primary bg-primary/10 text-primary"
                        : "border-border text-muted-foreground hover:bg-muted"
                    }`}
                  >
                    <div className="font-semibold">Direct Push</div>
                    <div className="text-[11px] opacity-80 mt-0.5">
                      Pushes directly to the target branch
                    </div>
                  </button>
                  <button
                    type="button"
                    data-testid="publishing-mode-pull-request"
                    onClick={() => setPublishingMode("pull_request")}
                    className={`p-2.5 rounded border text-xs font-medium text-left transition-colors ${
                      publishingMode === "pull_request"
                        ? "border-primary bg-primary/10 text-primary"
                        : "border-border text-muted-foreground hover:bg-muted"
                    }`}
                  >
                    <div className="font-semibold">Pull Request</div>
                    <div className="text-[11px] opacity-80 mt-0.5">
                      Creates an automated PR for review
                    </div>
                  </button>
                </div>
                {publishingMode === "pull_request" && (
                  <p className="text-[11px] text-muted-foreground mt-1">
                    Pushes to a dedicated source branch (
                    <code className="font-mono text-[10px] bg-muted px-1 py-0.5 rounded">
                      forgeops/deploy-&lt;run_id&gt;
                    </code>
                    ) and opens an automated PR targeting the base branch without modifying it
                    directly.
                  </p>
                )}
              </div>

              {/* Repository Name & Branch */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <label htmlFor="github-repo-name" className="text-xs font-medium text-foreground">
                    Repository Name
                  </label>
                  <Input
                    id="github-repo-name"
                    aria-label="Repository Name"
                    value={githubRepoName}
                    onChange={(e) => {
                      setGithubRepoName(e.target.value);
                      setGithubBranch("main");
                    }}
                    placeholder="e.g. org/repo-name"
                    className="text-sm font-mono"
                  />
                </div>
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <label
                      htmlFor="github-target-branch"
                      className="text-xs font-medium text-foreground"
                    >
                      {publishingMode === "pull_request"
                        ? "Base Branch (Destination for PR)"
                        : "Target Branch"}
                    </label>
                    {branchesQuery.data?.default_branch &&
                      branchesQuery.data.default_branch === githubBranch && (
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5">
                          Default Branch
                        </Badge>
                      )}
                  </div>
                  <Input
                    id="github-target-branch"
                    aria-label="Target Branch"
                    value={githubBranch}
                    onChange={(e) => setGithubBranch(e.target.value)}
                    placeholder={branchesQuery.data?.default_branch || "main"}
                    className="text-sm font-mono"
                    list={branchesQuery.data?.branches ? "github-branches-datalist" : undefined}
                  />
                  {branchesQuery.data?.branches && (
                    <datalist id="github-branches-datalist">
                      {branchesQuery.data.branches.map((b) => (
                        <option key={b} value={b} />
                      ))}
                    </datalist>
                  )}
                  {branchesQuery.isLoading && (
                    <p className="text-[11px] text-muted-foreground flex items-center gap-1.5 mt-1">
                      <Loader2 className="size-3 animate-spin" /> Discovering repository branches...
                    </p>
                  )}
                  {branchesQuery.data?.truncated && (
                    <p className="text-[11px] text-amber-600 dark:text-amber-400 mt-1">
                      Repository contains &gt;1,000 branches; listing capped at 1,000.
                    </p>
                  )}
                  {branchesQuery.isError && (
                    <p className="text-[11px] text-destructive mt-1">
                      Could not discover branches for {githubRepoName}. You can type the branch
                      manually.
                    </p>
                  )}
                </div>
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
                  value={githubCommitMessage}
                  onChange={(e) => setGithubCommitMessage(e.target.value)}
                  placeholder="Automated deployment by ForgeOps"
                  className="text-sm"
                />
              </div>
            </section>
          )}

          {/* Vercel Config Section */}
          {selectedStrategyOption.hasVercel && (
            <section
              data-testid="vercel-config-section"
              aria-labelledby="vercel-config-heading"
              className="rounded-lg border border-border bg-card/60 p-4 space-y-4"
            >
              <div className="flex items-center gap-2 border-b border-border/60 pb-2">
                <Globe className="h-4 w-4 text-primary" />
                <h3 id="vercel-config-heading" className="text-sm font-semibold text-foreground">
                  Vercel Configuration
                </h3>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 items-end">
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
                    value={vercelProjectName}
                    onChange={(e) => setVercelProjectName(e.target.value)}
                    placeholder="e.g. my-app-frontend"
                    className="text-sm font-mono"
                  />
                </div>

                <div className="flex items-center gap-2 pb-2">
                  <input
                    type="checkbox"
                    id="vercel-production-deploy"
                    aria-label="Deploy to Production"
                    checked={vercelProduction}
                    onChange={(e) => setVercelProduction(e.target.checked)}
                    className="h-4 w-4 rounded border-border text-primary focus:ring-primary"
                  />
                  <label
                    htmlFor="vercel-production-deploy"
                    className="text-xs font-medium text-foreground cursor-pointer select-none"
                  >
                    Deploy to Production
                  </label>
                </div>
              </div>
            </section>
          )}

          {/* Docker Config Section */}
          {selectedStrategyOption.hasDocker && (
            <section
              data-testid="docker-config-section"
              aria-labelledby="docker-config-heading"
              className="rounded-lg border border-border bg-card/60 p-4 space-y-4"
            >
              <div className="flex items-center gap-2 border-b border-border/60 pb-2">
                <Box className="h-4 w-4 text-primary" />
                <h3 id="docker-config-heading" className="text-sm font-semibold text-foreground">
                  Docker Configuration
                </h3>
              </div>

              <div className="space-y-2">
                <label className="text-xs font-medium text-foreground">
                  Port Bindings (Host Port : Container Port)
                </label>
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1">
                    <label htmlFor="docker-host-port" className="text-[11px] text-muted-foreground">
                      Host Port
                    </label>
                    <Input
                      id="docker-host-port"
                      aria-label="Host Port"
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

        {/* Footer Actions */}
        <div className="flex items-center justify-end gap-3 border-t border-border pt-4">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onClose}
            disabled={isSubmitting}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="default"
            size="sm"
            data-testid="create-run-button"
            disabled={
              isSubmitting ||
              (selectedStrategyOption.hasGitHub && !githubRepoName.trim()) ||
              (selectedStrategyOption.hasVercel && !vercelProjectName.trim())
            }
            onClick={handleCreateRun}
            className="font-medium shadow-sm bg-primary text-primary-foreground hover:bg-primary/90"
          >
            {isSubmitting ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                Creating Run...
              </>
            ) : (
              "Create Run & Open Pipeline"
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
