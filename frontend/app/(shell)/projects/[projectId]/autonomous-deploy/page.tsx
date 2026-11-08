// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertCircle, ArrowLeft } from "lucide-react";
import { AutonomousDeployForm } from "@/features/deployments/AutonomousDeployForm";

interface ProjectBrief {
  id: string;
  name: string;
}

export default function AutonomousDeployPage() {
  const params = useParams<{ projectId: string }>();
  const router = useRouter();
  const projectId = params?.projectId ?? "";

  // Query project name for breadcrumb and header
  const projectQuery = useQuery({
    queryKey: ["projects", projectId, "detail"],
    queryFn: () => api.get<ProjectBrief>(`/projects/${projectId}`),
    enabled: Boolean(projectId),
    retry: false,
  });

  if (projectQuery.isLoading) {
    return (
      <div
        data-testid="autonomous-deploy-page-skeleton"
        className="space-y-6 w-full max-w-4xl mx-auto pb-16"
      >
        <div className="flex items-center gap-3">
          <Skeleton className="h-4 w-32" />
        </div>
        <div className="rounded-xl border border-border bg-card p-6 shadow-sm space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-4 w-96" />
        </div>
        <div className="rounded-xl border border-border bg-card p-6 shadow-sm space-y-4">
          <Skeleton className="h-6 w-48" />
          <div className="grid grid-cols-2 gap-3">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        </div>
      </div>
    );
  }

  if (projectQuery.isError || !projectQuery.data) {
    return (
      <div className="max-w-4xl mx-auto py-12 space-y-4">
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 p-6 text-destructive space-y-3">
          <div className="flex items-center gap-2 font-semibold text-base">
            <AlertCircle className="h-5 w-5" />
            <span>Could not load project</span>
          </div>
          <p className="text-sm opacity-90">
            The project could not be found or you may not have permission to access it.
          </p>
          <div className="pt-2">
            <Button variant="outline" asChild size="sm">
              <Link href="/projects" className="inline-flex items-center gap-1.5">
                <ArrowLeft className="h-4 w-4" /> Back to projects
              </Link>
            </Button>
          </div>
        </div>
      </div>
    );
  }

  const projectName = projectQuery.data.name;

  return (
    <div className="w-full max-w-4xl mx-auto space-y-6 pb-16">
      {/* Breadcrumb Navigation */}
      <nav
        aria-label="Breadcrumb"
        className="flex items-center gap-2 text-xs text-muted-foreground"
      >
        <Link
          href="/projects"
          className="hover:text-foreground transition-colors underline-offset-4 hover:underline"
        >
          Projects
        </Link>
        <span>/</span>
        <Link
          href={`/projects/${projectId}`}
          className="hover:text-foreground transition-colors underline-offset-4 hover:underline font-medium"
        >
          {projectName}
        </Link>
        <span>/</span>
        <span className="text-foreground font-semibold">Autonomous Deployment</span>
      </nav>

      {/* Page Header */}
      <div className="border-b border-border pb-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl md:text-3xl font-bold tracking-tight text-foreground">
              Autonomous Deployment
            </h1>
            <p className="text-sm text-muted-foreground mt-1 max-w-2xl">
              Configure automated end-to-end deployment orchestration, repository synchronization,
              and cloud hosting for project{" "}
              <span className="font-semibold text-foreground">{projectName}</span>.
            </p>
          </div>
          <Button variant="outline" size="sm" asChild>
            <Link
              href={`/projects/${projectId}`}
              className="inline-flex items-center gap-1.5 text-xs"
            >
              <ArrowLeft className="h-3.5 w-3.5" /> Return to project
            </Link>
          </Button>
        </div>
      </div>

      {/* Main Deployment Form */}
      <AutonomousDeployForm
        projectId={projectId}
        projectName={projectName}
        onCancel={() => router.push(`/projects/${projectId}`)}
        onSuccess={(run) => router.push(`/projects/${projectId}/autonomous-deploy/${run.id}`)}
      />
    </div>
  );
}
