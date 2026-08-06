import { ProjectList } from "@/features/projects/ProjectList";

export default function HomePage() {
  const sampleProjects = [
    { id: "proj-1", name: "ForgeOps Platform", repository: "parag8487/ForgeOps", readinessScore: 95 }
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">ForgeOps Dashboard</h1>
        <p className="mt-1 text-muted-foreground">
          AI-powered DevOps automation platform.
        </p>
      </div>
      <ProjectList projects={sampleProjects} />
    </div>
  );
}

