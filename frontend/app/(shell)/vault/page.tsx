// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { SecretVault, type SecretRefUI } from "@/features/vault/SecretVault";

export default function VaultPage() {
  const secrets = useQuery({
    queryKey: queryKeys.secrets.list(),
    queryFn: () => api.get<SecretRefUI[]>("/secrets"),
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Vault</h1>
        <p className="mt-1 text-muted-foreground">
          Secret references, read from <code>GET /api/v1/secrets</code>.
        </p>
      </div>

      <AsyncState
        isPending={secrets.isPending}
        error={secrets.error}
        isEmpty={secrets.data?.length === 0}
        emptyMessage="No secret references are registered for this tenant."
        label="secret references"
      >
        <SecretVault secrets={secrets.data ?? []} />
      </AsyncState>

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
