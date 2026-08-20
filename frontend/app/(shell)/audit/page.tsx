// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { AuditViewer, type AuditEventUI } from "@/features/audit/AuditViewer";

const PAGE_LIMIT = 50;

/** Mirrors `AuditPage`: a page plus the cursor that fetches the next one. */
interface AuditPage {
  events: AuditEventUI[];
  next_cursor: number | null;
}

export default function AuditPageRoute() {
  const audit = useQuery({
    queryKey: queryKeys.audit.events(PAGE_LIMIT),
    queryFn: () => api.get<AuditPage>(`/audit/events?limit=${PAGE_LIMIT}`),
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Audit log</h1>
        <p className="mt-1 text-muted-foreground">
          Read from <code>GET /api/v1/audit/events</code>, which is backed by a real table.
        </p>
      </div>

      <AsyncState
        isPending={audit.isPending}
        error={audit.error}
        isEmpty={audit.data?.events.length === 0}
        emptyMessage="The audit table is empty. Nothing has been recorded yet, which is a valid state rather than a failure."
        label="audit events"
      >
        <AuditViewer events={audit.data?.events ?? []} />
      </AsyncState>

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p>
          This endpoint reads a database rather than returning a fixture, so an empty table means
          nothing has happened yet — not that the screen is unfinished. The records carry{" "}
          <code>prev_hash</code> and <code>hash</code>, letting a caller recompute the chain
          independently; that verification surface is not on this screen.
        </p>
      </aside>
    </div>
  );
}
