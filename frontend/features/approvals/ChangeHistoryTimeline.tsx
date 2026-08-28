// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";

/**
 * Change sets for one project, over time, with what each status MEANS — phases.md §1.6.
 *
 * `GET /api/v1/approvals?project_id=…` already filters and keyset-pages server-side; nothing called
 * it with a project. The approvals screen lists the tenant's pending queue, which answers "what needs
 * me now" and not "what has happened to this project", and those are different questions: the second
 * one needs terminal states included and needs them ordered.
 *
 * THE STATUS VOCABULARY IS THE POINT. §3.6 defines thirteen states and revision `0010` put a CHECK
 * constraint on the column generated from them, so these are the only values that can appear. Naming
 * them here rather than colour-coding them is deliberate — `rolled_back` and `reverted` are two
 * different things that a red badge would render identically, and the difference matters: one is an
 * apply that failed midway and undid itself, the other is a deliberate reversal that went through
 * the chokepoint with its own fresh authority.
 */

/** Mirrors `ChangeSetSummary` in `backend/src/approvals/schemas.py`. */
export interface ChangeSetSummary {
  id: string;
  project_id: string;
  status: string;
  origin: string;
  blast_radius_score: number;
  blast_radius_verdict: string;
  version: number;
  generation_run_id: string | null;
  created_at: string;
  applied_at: string | null;
}

interface ChangeSetPage {
  change_sets: ChangeSetSummary[];
  next_cursor: string | null;
}

/**
 * §3.6's thirteen states, each in the terms a person reading a history needs.
 *
 * Written out in full rather than derived, because the whole value is the explanation. A state absent
 * from this map still renders — with the raw value and no invented gloss — rather than being hidden,
 * so a fourteenth state added to the backend shows up as an unexplained status instead of vanishing
 * from the timeline.
 */
export const CHANGE_SET_STATUS_MEANING: Readonly<Record<string, string>> = {
  draft: "Compiled but not yet submitted to the governance chokepoint.",
  validating: "Running the deterministic validation gate — syntax, schema and security scans.",
  validation_failed: "The deterministic gate refused it. Nothing was submitted for approval.",
  pending_approval: "Admitted and waiting for a human decision. Nothing has been written.",
  approved: "Approved. Authority was minted and the signed command handed to the agent.",
  rejected: "A reviewer refused it. Nothing was written, and the refusal is on the audit chain.",
  applying: "The agent is writing the files, with a backup taken first.",
  applied: "Written to the working tree. Reversible from here through Revert.",
  apply_failed: "The agent could not complete the write and reported the failure.",
  rolled_back:
    "An apply failed partway and undid itself from its backup. NOT the same as reverted — nobody chose this.",
  reverted:
    "Deliberately reversed after being applied. The reverse change set went through all six governance stages with its own authority.",
  expired: "Its approval window closed before it was applied.",
  superseded: "A later change set replaced it before it was applied.",
};

/** Which states mean "nothing further will happen to this", for the grouping below. */
const TERMINAL = new Set([
  "validation_failed",
  "rejected",
  "applied",
  "apply_failed",
  "rolled_back",
  "reverted",
  "expired",
  "superseded",
]);

const PAGE_LIMIT = 50;

export function ChangeHistoryTimeline({ projectId }: { projectId: string }) {
  const history = useQuery({
    queryKey: queryKeys.approvals.history(projectId),
    queryFn: () => api.get<ChangeSetPage>(`/approvals?project_id=${projectId}&limit=${PAGE_LIMIT}`),
    enabled: projectId !== "",
    retry: false,
  });

  return (
    <AsyncState
      isPending={history.isPending}
      error={history.error}
      isEmpty={history.data?.change_sets.length === 0}
      emptyMessage="No change set has ever been submitted for this project. Generation is what creates one."
      label="change history"
    >
      <ol className="relative space-y-4 border-l border-border pl-6" data-testid="change-history">
        {history.data?.change_sets.map((changeSet) => (
          <li key={changeSet.id} className="relative">
            {/* Decorative only — the status is stated in text below, so the marker carries no
                information a sighted user gets and a screen-reader user does not. */}
            <span
              aria-hidden="true"
              className={
                TERMINAL.has(changeSet.status)
                  ? "absolute -left-[1.6rem] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-border bg-background"
                  : "absolute -left-[1.6rem] top-1.5 h-2.5 w-2.5 rounded-full bg-primary"
              }
            />
            <div className="rounded-lg border border-border bg-background p-3 text-sm">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <p className="font-medium">
                  <span data-testid={`status-${changeSet.id}`}>{changeSet.status}</span>
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    from {changeSet.origin}
                  </span>
                </p>
                <time
                  className="font-mono text-xs text-muted-foreground"
                  dateTime={changeSet.created_at}
                >
                  {changeSet.created_at}
                </time>
              </div>

              <p className="mt-1 text-muted-foreground">
                {CHANGE_SET_STATUS_MEANING[changeSet.status] ??
                  "This status has no description in the UI, so only the stored value is shown rather than a guess at its meaning."}
              </p>

              <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
                <div className="flex gap-1.5">
                  <dt className="font-medium">Blast radius</dt>
                  <dd>
                    {changeSet.blast_radius_score} — {changeSet.blast_radius_verdict}
                  </dd>
                </div>
                {changeSet.applied_at ? (
                  <div className="flex gap-1.5">
                    <dt className="font-medium">Applied</dt>
                    <dd>
                      <time dateTime={changeSet.applied_at}>{changeSet.applied_at}</time>
                    </dd>
                  </div>
                ) : null}
                <div className="flex gap-1.5">
                  <dt className="font-medium">Version</dt>
                  <dd>{changeSet.version}</dd>
                </div>
              </dl>

              <p className="mt-2">
                <Link
                  href="/approvals"
                  className="text-xs underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Open in the approval centre
                </Link>
              </p>
            </div>
          </li>
        ))}
      </ol>
    </AsyncState>
  );
}
