// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { ProjectPicker } from "@/components/ui/project-picker";
import { SecretVault, type SecretRefUI } from "@/features/vault/SecretVault";

/**
 * Secret references for one project.
 *
 * THIS SCREEN HAD NEVER WORKED. It called `GET /api/v1/secrets` with no query string, and that
 * endpoint takes `project_id` as a REQUIRED parameter -- so every visit produced
 * `422 Request validation failed`, and the panel dutifully reported it as an error loading secret
 * references. The request was malformed, not the response.
 *
 * Worth recording why it went unnoticed: the page's own test supplied a list of references directly to
 * `SecretVault`, so the component was covered and the REQUEST was not. A test that hands a component
 * its data proves the component renders; it proves nothing about whether anything can fetch that data.
 */
export default function VaultPage() {
  const [projectId, setProjectId] = useState("");

  const secrets = useQuery({
    queryKey: queryKeys.secrets.list(projectId),
    queryFn: () => api.get<SecretRefUI[]>(`/secrets?project_id=${projectId}`),
    // Not fired until a project is chosen: without one the request is guaranteed to 422.
    enabled: projectId !== "",
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Vault</h1>
        <p className="mt-1 text-muted-foreground">
          Secret references for one project, read from <code>GET /api/v1/secrets</code>. References
          only — the response never carries secret material.
        </p>
      </div>

      <ProjectPicker value={projectId} onChange={setProjectId} id="vault-project" />

      {projectId === "" ? (
        <p className="text-sm text-muted-foreground">
          Choose a project. <code>project_id</code> is a required parameter on this endpoint, so the
          request is not made without one rather than made and refused.
        </p>
      ) : (
        <AsyncState
          isPending={secrets.isPending}
          error={secrets.error}
          isEmpty={secrets.data?.length === 0}
          emptyMessage="No secret references are registered for this project."
          label="secret references"
        >
          <SecretVault secrets={secrets.data ?? []} />
        </AsyncState>
      )}

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p>
          The response carries the key, the environment and the storage path and never the secret
          material, so there is no value on this screen to leak. Creating and rotating references is
          served by the API but not surfaced here.
        </p>
      </aside>
    </div>
  );
}
