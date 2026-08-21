// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { GeneratorWizard } from "@/features/generation/GeneratorWizard";
import { ProjectPicker } from "@/components/ui/project-picker";

export default function GenerationPage() {
  const [projectId, setProjectId] = useState("");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Generation</h1>
        <p className="mt-1 text-muted-foreground">
          Streams a real run from <code>POST /api/v1/generation/runs</code> over the six §7.4 server
          -sent event types. Artifacts that pass the deterministic validation gate are submitted to
          the governance chokepoint as a change set rather than written anywhere directly.
        </p>
      </div>

      <ProjectPicker value={projectId} onChange={setProjectId} id="generation-project" />

      {projectId !== "" ? (
        <GeneratorWizard projectId={projectId} />
      ) : (
        <p className="text-sm text-muted-foreground">
          Enter the project the run belongs to. The generator is not offered before then, because a
          run has to be attributed to a project to be submitted as a change set — and a button that
          cannot succeed is worse than one that is not there.
        </p>
      )}
    </div>
  );
}
