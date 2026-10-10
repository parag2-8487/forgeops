// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertCircle, ArrowLeft } from "lucide-react";
import {
  JenkinsPipelineDashboard,
  type AutonomousRunPublicResponse,
} from "@/features/deployments/JenkinsPipelineDashboard";

interface ProjectBrief {
  id: string;
  name: string;
}

export default function AutonomousDeployRunPage() {
  const params = useParams<{ projectId: string; runId: string }>();
  const projectId = params?.projectId ?? "";
  const runId = params?.runId ?? "";

  // Query project name for breadcrumb
  const projectQuery = useQuery({
    queryKey: ["projects", projectId, "detail"],
    queryFn: () => api.get<ProjectBrief>(`/projects/${projectId}`),
    enabled: Boolean(projectId),
    retry: false,
  });

  // Query autonomous run snapshot and active stages
  const runQuery = useQuery({
    queryKey: ["projects", projectId, "autonomous-deploy", runId],
    queryFn: () =>
      api.get<AutonomousRunPublicResponse>(`/projects/${projectId}/autonomous-deploy/${runId}`),
    enabled: Boolean(projectId && runId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "running" || status === "cancelling" ? 2500 : false;
    },
    retry: false,
  });

  // Loading skeleton state
  if (runQuery.isLoading) {
    return (
      <div data-testid="pipeline-page-skeleton" className="space-y-6 w-full pb-16">
        {/* Header bar skeleton */}
        <div className="rounded-xl border border-border bg-card p-6 shadow-sm space-y-4">
          <div className="flex items-center justify-between">
            <Skeleton className="h-5 w-64" />
            <Skeleton className="h-9 w-32" />
          </div>
          <div className="flex items-center gap-3 pt-2">
            <Skeleton className="h-6 w-36" />
            <Skeleton className="h-6 w-48" />
            <Skeleton className="h-6 w-24" />
          </div>
          <Skeleton className="h-2.5 w-full rounded-full" />
          <Skeleton className="h-8 w-full rounded-lg" />
        </div>

        {/* Graph skeleton */}
        <div className="space-y-2">
          <Skeleton className="h-5 w-44" />
          <div className="rounded-xl border border-border bg-card p-6 shadow-sm flex items-center gap-6 overflow-x-auto">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="flex items-center gap-4 shrink-0">
                <Skeleton className="size-11 rounded-full" />
                {i < 5 && <Skeleton className="h-0.5 w-12" />}
              </div>
            ))}
          </div>
        </div>

        {/* Details card skeleton */}
        <div className="rounded-xl border border-border bg-card p-6 shadow-sm space-y-4">
          <Skeleton className="h-6 w-48" />
          <div className="grid grid-cols-3 gap-4">
            <Skeleton className="h-16 rounded-lg" />
            <Skeleton className="h-16 rounded-lg" />
            <Skeleton className="h-16 rounded-lg" />
          </div>
          <Skeleton className="h-20 rounded-lg" />
        </div>
      </div>
    );
  }

  // Error state
  if (runQuery.isError || !runQuery.data) {
    const errorMsg =
      runQuery.error instanceof Error
        ? runQuery.error.message
        : `Autonomous deployment run '${runId}' could not be loaded.`;

    return (
      <div data-testid="pipeline-page-error" className="space-y-6 w-full max-w-3xl py-12">
        <div className="rounded-xl border border-destructive/30 bg-destructive/10 p-6 space-y-4">
          <div className="flex items-center gap-3 text-destructive">
            <AlertCircle className="size-6 shrink-0" />
            <div>
              <h2 className="text-base font-semibold">Failed to Load Deployment Pipeline</h2>
              <p className="text-xs mt-1 text-destructive/90">{errorMsg}</p>
            </div>
          </div>

          <div className="pt-2">
            <Button variant="outline" size="sm" asChild>
              <Link href={`/projects/${projectId}`}>
                <ArrowLeft className="mr-1.5 size-4" />
                Return to Project Overview
              </Link>
            </Button>
          </div>
        </div>
      </div>
    );
  }

  const projectName = projectQuery.data?.name || "Project";

  return (
    <JenkinsPipelineDashboard
      projectId={projectId}
      runId={runId}
      projectName={projectName}
      run={runQuery.data}
      onRefresh={() => runQuery.refetch()}
    />
  );
}
