// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { ProjectPicker } from "@/components/ui/project-picker";
import { SecretVault, type SecretRefUI } from "@/features/vault/SecretVault";

/**
 * Secret references for one project, now writable.
 *
 * THIS SCREEN HAD NEVER WORKED, and then only listed. It first called `GET /api/v1/secrets` with no
 * query string, and that endpoint takes `project_id` as a REQUIRED parameter -- so every visit
 * produced `422 Request validation failed`, and the panel dutifully reported it as an error loading
 * secret references. Once that was fixed it could list and nothing else: `POST`, `PATCH` and `DELETE`
 * were all served and all uncalled.
 *
 * Worth recording why the original went unnoticed: the page's own test supplied a list of references
 * directly to `SecretVault`, so the component was covered and the REQUEST was not. A test that hands a
 * component its data proves the component renders; it proves nothing about whether anything can fetch
 * that data. The tests for the write path assert on the request, for that reason.
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
          Secret references for one project. Values can be written and rotated; the API exposes
          metadata only, so no value on this screen can be read back — including one you just
          stored.
        </p>
      </div>

      <ProjectPicker value={projectId} onChange={setProjectId} id="vault-project" />

      {projectId === "" ? (
        <p className="text-sm text-muted-foreground">
          Choose a project. <code>project_id</code> is a required parameter on this endpoint, so the
          request is not made without one rather than made and refused.
        </p>
      ) : (
        <AsyncState isPending={secrets.isPending} error={secrets.error} label="secret references">
          <SecretVault secrets={secrets.data ?? []} projectId={projectId} />
        </AsyncState>
      )}

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          How write-only is enforced, not merely intended.
        </p>
        <p className="mt-2">
          The value inputs are <strong>uncontrolled</strong>: there is no React state holding a
          secret, so one cannot appear in a state snapshot, a DevTools inspection or an error
          boundary. The DOM node is cleared before the request is awaited, so the material is gone
          from the page while the write is still in flight. And the response shape carries no value
          field, so the query cache has nothing to store even if it wanted to.
        </p>
        <p className="mt-2">
          Deleting a reference removes the metadata record. For an Infisical-backed secret the
          material in Infisical is not removed: this platform does not own that store, and reaching
          into it would be acting outside what it manages.
        </p>
      </aside>
    </div>
  );
}
