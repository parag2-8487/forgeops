// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";

/** Mirrors `PolicyTemplateRead` in `backend/src/policies/routes.py`. */
interface PolicyTemplate {
  id: string;
  name: string;
  description: string;
  rego_rules: string;
  parameters: Record<string, unknown>;
}

export default function PoliciesPage() {
  const templates = useQuery({
    queryKey: queryKeys.policies.templates(),
    queryFn: () => api.get<PolicyTemplate[]>("/policies/templates"),
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Policies</h1>
        <p className="mt-1 text-muted-foreground">
          Governance templates, read from <code>GET /api/v1/policies/templates</code>.
        </p>
      </div>

      <AsyncState
        isPending={templates.isPending}
        error={templates.error}
        isEmpty={templates.data?.length === 0}
        emptyMessage="No policy templates are registered."
        label="policy templates"
      >
        <ul className="space-y-4">
          {templates.data?.map((t) => (
            <li key={t.id} className="rounded-lg border border-border bg-background p-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-sm font-semibold">{t.name}</h2>
                <code className="text-xs text-muted-foreground">{t.id}</code>
              </div>
              <p className="mt-1 text-sm text-muted-foreground">{t.description}</p>
              <pre className="mt-3 overflow-x-auto rounded bg-muted p-3 text-xs">
                <code>{t.rego_rules}</code>
              </pre>
            </li>
          ))}
        </ul>
      </AsyncState>

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">Read-only, on purpose.</p>
        <p className="mt-2">
          The backend does serve policy create, update, delete and a dry-run test endpoint, so an
          editor is buildable. It is not built here: <code>features/policies/PolicyEditor.tsx</code>{" "}
          exists but its &ldquo;Validate &amp; Save&rdquo; control was never connected to anything,
          and shipping a button that silently discards a policy edit would be worse than not
          shipping the screen. Wiring the write path — with the dry-run gate in front of it, which
          is the whole point of that endpoint — is its own piece of work.
        </p>
      </aside>
    </div>
  );
}
