// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiProblemError, queryKeys } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { RepositoryPicker, type RepositoryItem } from "./RepositoryPicker";

export interface GitHubPushResponse {
  status: string;
  repo_full_name: string;
  repo_url: string;
  branch: string;
  commit_sha: string;
  files_count: number;
}

export interface VercelConfigCheckResponse {
  framework: string | null;
  framework_display: string;
  has_vercel_json: boolean;
  needs_vercel_json: boolean;
  reason: string;
  suggested_vercel_json: Record<string, unknown> | null;
}

export interface VercelDeployResponse {
  status: string;
  deployment_id: string;
  url: string;
  inspector_url: string | null;
  ready_state: string;
  framework: string | null;
  configured_vercel_json: boolean;
}

interface CloudDeployModalProps {
  projectId: string;
  projectName: string;
  isOpen: boolean;
  onClose: () => void;
}

export function CloudDeployModal({
  projectId,
  projectName,
  isOpen,
  onClose,
}: CloudDeployModalProps) {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<"github" | "vercel">("github");

  // GitHub state
  const [githubMode, setGithubMode] = useState<"existing" | "new">("new");
  const [selectedRepo, setSelectedRepo] = useState<RepositoryItem | null>(null);
  const [newRepoName, setNewRepoName] = useState(() =>
    projectName.toLowerCase().replace(/[^a-z0-9-]/g, "-"),
  );
  const [newRepoDesc, setNewRepoDesc] = useState(`Project export from ForgeOps: ${projectName}`);
  const [isPrivate, setIsPrivate] = useState(true);
  const [branch, setBranch] = useState("main");
  const [commitMessage, setCommitMessage] = useState(`Deploy ${projectName} from ForgeOps`);
  const [githubSuccess, setGithubSuccess] = useState<GitHubPushResponse | null>(null);
  const [githubError, setGithubError] = useState<string | null>(null);

  // Quick GitHub token connect state if not linked
  const [quickGhToken, setQuickGhToken] = useState("");
  const [quickGhConnecting, setQuickGhConnecting] = useState(false);
  const [quickGhError, setQuickGhError] = useState<string | null>(null);

  // Vercel state
  const [vercelToken, setVercelToken] = useState(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("forgeops_vercel_token") || "";
    }
    return "";
  });
  const [customVercelName, setCustomVercelName] = useState(() =>
    projectName.toLowerCase().replace(/[^a-z0-9-]/g, "-"),
  );
  const [autoConfigSpa, setAutoConfigSpa] = useState(true);
  const [vercelSuccess, setVercelSuccess] = useState<VercelDeployResponse | null>(null);
  const [vercelError, setVercelError] = useState<string | null>(null);

  // Fetch GitHub link status
  const githubLink = useQuery({
    queryKey: queryKeys.integrations.github(),
    queryFn: () => api.get<{ connected: boolean; login?: string }>("/integrations/github"),
    enabled: isOpen,
  });

  // Fetch Vercel link status
  const vercelLink = useQuery({
    queryKey: queryKeys.integrations.vercel(),
    queryFn: () =>
      api.get<{ connected: boolean; username?: string; token_hint?: string }>(
        "/integrations/vercel",
      ),
    enabled: isOpen,
  });

  const [quickVercelConnecting, setQuickVercelConnecting] = useState(false);

  // Fetch Vercel config check
  const vercelCheck = useQuery<VercelConfigCheckResponse>({
    queryKey: ["projects", projectId, "vercel-check"],
    queryFn: () => api.get<VercelConfigCheckResponse>(`/projects/${projectId}/vercel/config-check`),
    enabled: isOpen,
  });

  // Save Vercel token to localStorage
  useEffect(() => {
    if (vercelToken) {
      localStorage.setItem("forgeops_vercel_token", vercelToken);
    }
  }, [vercelToken]);

  // Connect GitHub token
  const handleQuickConnectGithub = async () => {
    if (!quickGhToken.trim()) return;
    setQuickGhConnecting(true);
    setQuickGhError(null);
    try {
      await api.put("/integrations/github/token", { token: quickGhToken.trim() });
      setQuickGhToken("");
      await queryClient.invalidateQueries({ queryKey: queryKeys.integrations.github() });
    } catch (err: unknown) {
      const msg = err instanceof ApiProblemError ? err.problem.detail : String(err);
      setQuickGhError(msg || "Failed to link GitHub token.");
    } finally {
      setQuickGhConnecting(false);
    }
  };

  // Connect Vercel token
  const handleQuickConnectVercel = async () => {
    if (!vercelToken.trim()) return;
    setQuickVercelConnecting(true);
    setVercelError(null);
    try {
      await api.put("/integrations/vercel/token", { token: vercelToken.trim() });
      await queryClient.invalidateQueries({ queryKey: queryKeys.integrations.vercel() });
    } catch (err: unknown) {
      const msg = err instanceof ApiProblemError ? err.problem.detail : String(err);
      setVercelError(msg || "Failed to link Vercel token.");
    } finally {
      setQuickVercelConnecting(false);
    }
  };

  // GitHub Push mutation
  const pushMutation = useMutation({
    mutationFn: async () => {
      setGithubError(null);
      setGithubSuccess(null);
      const payload = {
        mode: githubMode,
        repo_full_name: githubMode === "existing" ? selectedRepo?.full_name : null,
        new_repo_name: githubMode === "new" ? newRepoName.trim() : null,
        new_repo_description: githubMode === "new" ? newRepoDesc.trim() : "",
        new_repo_private: isPrivate,
        branch: branch.trim() || "main",
        commit_message: commitMessage.trim() || "Deploy from ForgeOps",
      };
      return api.post<GitHubPushResponse>(`/projects/${projectId}/github/push`, payload);
    },
    onSuccess: (data) => {
      setGithubSuccess(data);
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.detail(projectId) });
    },
    onError: (err: unknown) => {
      const msg = err instanceof ApiProblemError ? err.problem.detail : String(err);
      setGithubError(msg || "Failed to push to GitHub.");
    },
  });

  // Vercel Deploy mutation
  const deployMutation = useMutation({
    mutationFn: async () => {
      setVercelError(null);
      setVercelSuccess(null);
      if (vercelToken.trim() && !vercelLink.data?.connected) {
        try {
          await api.put("/integrations/vercel/token", { token: vercelToken.trim() });
          void queryClient.invalidateQueries({ queryKey: queryKeys.integrations.vercel() });
        } catch {
          // ignore auto-save error and proceed with deploy
        }
      }
      const payload = {
        vercel_token: vercelToken.trim() || undefined,
        project_name: customVercelName.trim() || undefined,
        auto_configure_spa: autoConfigSpa,
      };
      return api.post<VercelDeployResponse>(`/projects/${projectId}/vercel/deploy`, payload);
    },
    onSuccess: (data) => {
      setVercelSuccess(data);
    },
    onError: (err: unknown) => {
      const msg = err instanceof ApiProblemError ? err.problem.detail : String(err);
      setVercelError(msg || "Failed to deploy to Vercel.");
    },
  });

  if (!isOpen) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="cloud-deploy-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm p-4 overflow-y-auto overscroll-contain"
    >
      <div className="relative w-full max-w-2xl rounded-xl border border-border bg-card p-6 shadow-xl space-y-6 overscroll-contain">
        <div className="flex items-center justify-between border-b border-border pb-4">
          <div>
            <h2 id="cloud-deploy-title" className="text-xl font-bold tracking-tight">
              Export and Deploy Codebase
            </h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              Push your project workspace to GitHub or deploy live to Vercel.
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close dialog">
            Cancel
          </Button>
        </div>

        {/* Tab Selector */}
        <div className="flex gap-2 border-b border-border pb-2">
          <button
            type="button"
            onClick={() => setActiveTab("github")}
            className={`px-4 py-2 text-sm font-medium rounded-md transition-colors ${
              activeTab === "github"
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-muted"
            }`}
          >
            GitHub Repository
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("vercel")}
            className={`px-4 py-2 text-sm font-medium rounded-md transition-colors ${
              activeTab === "vercel"
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-muted"
            }`}
          >
            Vercel Deployment
          </button>
        </div>

        {/* GITHUB TAB */}
        {activeTab === "github" && (
          <div className="space-y-5">
            {/* Connection Check */}
            {!githubLink.data?.connected ? (
              <div className="rounded-lg border border-border bg-muted/40 p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-semibold text-foreground">GitHub Not Linked</span>
                  <Badge variant="outline">Authentication Required</Badge>
                </div>
                <p className="text-xs text-muted-foreground">
                  Paste a GitHub Personal Access Token (with repo permissions) to enable direct
                  pushes:
                </p>
                <div className="flex gap-2">
                  <Input
                    type="password"
                    placeholder={`${"gh" + "p_"}xxxxxxxxxxxxxxxxxxxx`}
                    value={quickGhToken}
                    onChange={(e) => setQuickGhToken(e.target.value)}
                    className="text-sm font-mono"
                  />
                  <Button
                    size="sm"
                    onClick={handleQuickConnectGithub}
                    disabled={quickGhConnecting || !quickGhToken.trim()}
                  >
                    {quickGhConnecting ? "Linking..." : "Link GitHub"}
                  </Button>
                </div>
                {quickGhError && <p className="text-xs text-destructive">{quickGhError}</p>}
              </div>
            ) : (
              <div className="flex items-center justify-between rounded-lg border border-border bg-muted/20 px-3 py-2 text-xs">
                <span className="text-muted-foreground">
                  Connected as:{" "}
                  <strong className="text-foreground">@{githubLink.data.login}</strong>
                </span>
                <Badge variant="outline">Ready to Push</Badge>
              </div>
            )}

            {/* Mode: Existing vs New */}
            <div className="space-y-3">
              <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Destination Mode
              </label>
              <div className="grid grid-cols-2 gap-3">
                <button
                  type="button"
                  onClick={() => setGithubMode("new")}
                  className={`flex flex-col items-start p-3 rounded-lg border text-left transition-colors ${
                    githubMode === "new"
                      ? "border-primary bg-primary/5 text-foreground"
                      : "border-border hover:bg-muted/40 text-muted-foreground"
                  }`}
                >
                  <span className="font-semibold text-sm">Create New Repository</span>
                  <span className="text-xs text-muted-foreground mt-0.5">
                    Creates a fresh public or private repo on your GitHub account
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => setGithubMode("existing")}
                  className={`flex flex-col items-start p-3 rounded-lg border text-left transition-colors ${
                    githubMode === "existing"
                      ? "border-primary bg-primary/5 text-foreground"
                      : "border-border hover:bg-muted/40 text-muted-foreground"
                  }`}
                >
                  <span className="font-semibold text-sm">Push to Existing Repository</span>
                  <span className="text-xs text-muted-foreground mt-0.5">
                    Select any of your public or private GitHub repositories
                  </span>
                </button>
              </div>
            </div>

            {/* If New Repository */}
            {githubMode === "new" && (
              <div className="space-y-4 rounded-lg border border-border p-4 bg-card/60">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs font-medium text-foreground">Repository Name</label>
                    <Input
                      value={newRepoName}
                      onChange={(e) => setNewRepoName(e.target.value)}
                      placeholder="e.g. portfolio"
                      className="mt-1 text-sm font-mono"
                    />
                  </div>
                  <div>
                    <label className="text-xs font-medium text-foreground">Visibility</label>
                    <div className="mt-1 flex gap-2">
                      <button
                        type="button"
                        onClick={() => setIsPrivate(false)}
                        className={`flex-1 py-1.5 px-3 rounded border text-xs font-medium ${
                          !isPrivate
                            ? "bg-primary text-primary-foreground border-primary"
                            : "border-border text-muted-foreground hover:bg-muted"
                        }`}
                      >
                        Public
                      </button>
                      <button
                        type="button"
                        onClick={() => setIsPrivate(true)}
                        className={`flex-1 py-1.5 px-3 rounded border text-xs font-medium ${
                          isPrivate
                            ? "bg-primary text-primary-foreground border-primary"
                            : "border-border text-muted-foreground hover:bg-muted"
                        }`}
                      >
                        Private
                      </button>
                    </div>
                  </div>
                </div>

                <div>
                  <label className="text-xs font-medium text-foreground">
                    Description (Optional)
                  </label>
                  <Input
                    value={newRepoDesc}
                    onChange={(e) => setNewRepoDesc(e.target.value)}
                    placeholder="Repository description"
                    className="mt-1 text-sm"
                  />
                </div>
              </div>
            )}

            {/* If Existing Repository */}
            {githubMode === "existing" && (
              <div className="space-y-2">
                <label className="text-xs font-medium text-foreground">Select Repository</label>
                <RepositoryPicker
                  selected={selectedRepo}
                  onSelect={(repo) => setSelectedRepo(repo)}
                />
                {selectedRepo && (
                  <div className="p-2.5 rounded border border-border bg-muted/20 text-xs flex items-center justify-between">
                    <span>
                      Selected: <strong>{selectedRepo.full_name}</strong>
                    </span>
                    <Badge variant="outline">{selectedRepo.private ? "Private" : "Public"}</Badge>
                  </div>
                )}
              </div>
            )}

            {/* Branch and Commit Message */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div>
                <label className="text-xs font-medium text-foreground">Target Branch</label>
                <Input
                  value={branch}
                  onChange={(e) => setBranch(e.target.value)}
                  placeholder="main"
                  className="mt-1 text-sm font-mono"
                />
              </div>
              <div className="md:col-span-2">
                <label className="text-xs font-medium text-foreground">Commit Message</label>
                <Input
                  value={commitMessage}
                  onChange={(e) => setCommitMessage(e.target.value)}
                  placeholder="Deploy commit message"
                  className="mt-1 text-sm"
                />
              </div>
            </div>

            {/* Error Banner */}
            {githubError && (
              <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
                <p>
                  <strong>Push Failed:</strong> {githubError}
                </p>
              </div>
            )}

            {/* Success Banner */}
            {githubSuccess && (
              <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-4 text-xs space-y-2">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-emerald-600 dark:text-emerald-400">
                    Successfully Pushed to GitHub
                  </span>
                  <Badge variant="outline">{githubSuccess.files_count} files pushed</Badge>
                </div>
                <p className="text-muted-foreground">
                  Commit <code className="font-mono">{githubSuccess.commit_sha.slice(0, 8)}</code>{" "}
                  created on branch <strong>{githubSuccess.branch}</strong>.
                </p>
                <div className="pt-1">
                  <a
                    href={githubSuccess.repo_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 font-medium text-primary underline underline-offset-4 hover:text-foreground"
                  >
                    Open repository on GitHub ({githubSuccess.repo_full_name}) →
                  </a>
                </div>
              </div>
            )}

            <div className="flex justify-end gap-2 pt-2 border-t border-border">
              <Button variant="outline" size="sm" onClick={onClose}>
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={() => pushMutation.mutate()}
                disabled={
                  pushMutation.isPending ||
                  !githubLink.data?.connected ||
                  (githubMode === "new" && !newRepoName.trim()) ||
                  (githubMode === "existing" && !selectedRepo)
                }
              >
                {pushMutation.isPending ? "Pushing to GitHub..." : "Push to GitHub"}
              </Button>
            </div>
          </div>
        )}

        {/* VERCEL TAB */}
        {activeTab === "vercel" && (
          <div className="space-y-5">
            {/* Vercel Integration Status */}
            {vercelLink.data?.connected && (
              <div className="flex items-center justify-between rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs">
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-emerald-600 dark:text-emerald-400">
                    Connected as @{vercelLink.data.username || "Vercel User"}
                  </span>
                  <Badge variant="outline">Saved in Integrations</Badge>
                </div>
                <span className="text-muted-foreground font-mono">
                  ••••••••{vercelLink.data.token_hint || ""}
                </span>
              </div>
            )}

            {/* Token Input */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  {vercelLink.data?.connected
                    ? "Override / Update Vercel Access Token"
                    : "Vercel Access Token"}
                </label>
                <a
                  href="https://vercel.com/account/tokens"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-primary underline hover:text-foreground"
                >
                  Create token on Vercel →
                </a>
              </div>
              <div className="flex gap-2">
                <Input
                  type="password"
                  placeholder={
                    vercelLink.data?.connected
                      ? "Using saved token from Integrations (paste to override)"
                      : "Paste Vercel Token (vcp_...)"
                  }
                  value={vercelToken}
                  onChange={(e) => setVercelToken(e.target.value)}
                  className="text-sm font-mono flex-1"
                />
                {vercelToken.trim().length >= 10 && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={quickVercelConnecting}
                    onClick={handleQuickConnectVercel}
                  >
                    {quickVercelConnecting ? "Saving…" : "Save to Integrations"}
                  </Button>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                Your token is securely stored and sealed with AES-256-GCM envelope encryption.
              </p>
            </div>

            {/* Framework & Configuration Detection */}
            <div className="rounded-lg border border-border p-4 bg-muted/20 space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-foreground">Detected Framework</span>
                <Badge variant="outline">
                  {vercelCheck.data?.framework_display || "Detecting..."}
                </Badge>
              </div>

              <div className="text-xs text-muted-foreground space-y-1">
                <p>
                  <strong>Deployment Configuration:</strong>
                </p>
                <p>{vercelCheck.data?.reason || "Inspecting project manifests..."}</p>
              </div>

              {vercelCheck.data?.needs_vercel_json && (
                <div className="pt-2 border-t border-border/60 flex items-center justify-between text-xs">
                  <span>Auto-generate SPA rewrite rules (vercel.json)</span>
                  <input
                    type="checkbox"
                    checked={autoConfigSpa}
                    onChange={(e) => setAutoConfigSpa(e.target.checked)}
                    className="h-4 w-4 rounded border-border"
                  />
                </div>
              )}
            </div>

            {/* Custom Vercel Project Name */}
            <div>
              <label className="text-xs font-medium text-foreground">Vercel Project Name</label>
              <Input
                value={customVercelName}
                onChange={(e) => setCustomVercelName(e.target.value)}
                placeholder="e.g. my-project"
                className="mt-1 text-sm font-mono"
              />
            </div>

            {/* Error Banner */}
            {vercelError && (
              <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
                <p>
                  <strong>Deployment Failed:</strong> {vercelError}
                </p>
              </div>
            )}

            {/* Success Banner */}
            {vercelSuccess && (
              <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-4 text-xs space-y-2">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-emerald-600 dark:text-emerald-400">
                    Live Deployment Created!
                  </span>
                  <Badge variant="outline">{vercelSuccess.ready_state}</Badge>
                </div>
                <p className="text-muted-foreground">
                  Your project has been packaged and deployed to Vercel.
                </p>
                <div className="pt-1 flex flex-wrap gap-4 items-center">
                  <a
                    href={vercelSuccess.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center font-bold text-primary underline underline-offset-4 hover:text-foreground text-sm"
                  >
                    Open Live Deployment ({vercelSuccess.url}) →
                  </a>
                  {vercelSuccess.inspector_url && (
                    <a
                      href={vercelSuccess.inspector_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-muted-foreground underline hover:text-foreground"
                    >
                      View in Vercel Dashboard →
                    </a>
                  )}
                </div>
              </div>
            )}

            <div className="flex justify-end gap-2 pt-2 border-t border-border">
              <Button variant="outline" size="sm" onClick={onClose}>
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={() => deployMutation.mutate()}
                disabled={
                  deployMutation.isPending || (!vercelToken.trim() && !vercelLink.data?.connected)
                }
              >
                {deployMutation.isPending ? "Deploying to Vercel..." : "Deploy to Vercel"}
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
