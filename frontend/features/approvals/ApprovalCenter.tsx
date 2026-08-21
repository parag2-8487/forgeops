// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiProblemError, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { Button } from "@/components/ui/button";

/**
 * The Change Approval Center (design.md §3.6, §12.6 steps 7–9).
 *
 * What this replaces: a component taking `changeSets: {id, summary, status, diff}[]`, where `diff`
 * was one pre-flattened string and nothing ever passed it any data. That shape could not express
 * either of the two view modes §12.6 step 8 asks for, because a change set's diff IS its
 * `change_items` — each with a path, an action, and content before and after.
 *
 * THE APPROVER IS NOT AN INPUT ON THIS SCREEN. It is taken from the verified principal on the
 * server. The defect that kept this router unmounted was `approver: str = "admin"` as a query
 * parameter — the caller naming the identity the audit record would attribute the decision to, and
 * defaulting it to an administrator. Reintroducing that as a form field would be the same defect in
 * a nicer coat, so the decision body carries exactly a comment and the version.
 */

interface ChangeItemRead {
  id: string;
  file_path: string;
  action: "create" | "update" | "delete";
  old_content: string | null;
  new_content: string | null;
  old_hash: string | null;
  new_hash: string | null;
  ordinal: number;
}

interface ApprovalRead {
  id: string;
  approver_id: string;
  status: "approved" | "rejected";
  comment: string | null;
  created_at: string;
}

interface ChangeSetSummary {
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

interface ChangeSetDetail extends ChangeSetSummary {
  items: ChangeItemRead[];
  approvals: ApprovalRead[];
}

interface ChangeSetPage {
  change_sets: ChangeSetSummary[];
  next_cursor: string | null;
}

type ViewMode = "unified" | "split";

/** A decision is only offered in the one state §3.6 permits it from. */
const DECIDABLE = "pending_approval";

// ── diffing ─────────────────────────────────────────────────────────────────

export interface DiffRow {
  kind: "context" | "added" | "removed";
  oldLine: number | null;
  newLine: number | null;
  text: string;
}

/**
 * A line diff over the two contents the backend recorded.
 *
 * Deliberately a longest-common-subsequence diff rather than a naive index-by-index comparison: the
 * latter reports every line after an insertion as changed, which for a Dockerfile with one line
 * added at the top means the reviewer is shown the whole file as modified and learns nothing. The
 * point of a review screen is that the reviewer can see the change, so the diff has to be a real
 * one.
 */
export function diffLines(oldText: string, newText: string): DiffRow[] {
  const a = oldText === "" ? [] : oldText.replace(/\n$/, "").split("\n");
  const b = newText === "" ? [] : newText.replace(/\n$/, "").split("\n");

  // LCS lengths table. Bounded by the content the API returns; a change set is a review artifact,
  // not a bulk import.
  const lcs: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }

  const rows: DiffRow[] = [];
  let i = 0;
  let j = 0;
  let oldLine = 1;
  let newLine = 1;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      rows.push({ kind: "context", oldLine: oldLine++, newLine: newLine++, text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      rows.push({ kind: "removed", oldLine: oldLine++, newLine: null, text: a[i] });
      i++;
    } else {
      rows.push({ kind: "added", oldLine: null, newLine: newLine++, text: b[j] });
      j++;
    }
  }
  while (i < a.length)
    rows.push({ kind: "removed", oldLine: oldLine++, newLine: null, text: a[i++] });
  while (j < b.length)
    rows.push({ kind: "added", oldLine: null, newLine: newLine++, text: b[j++] });
  return rows;
}

const ROW_STYLE: Record<DiffRow["kind"], string> = {
  context: "",
  added: "bg-emerald-500/10",
  removed: "bg-destructive/10",
};

const ROW_SIGIL: Record<DiffRow["kind"], string> = { context: " ", added: "+", removed: "-" };

function UnifiedDiff({ rows }: { rows: DiffRow[] }) {
  return (
    <table className="w-full border-collapse font-mono text-xs">
      <caption className="sr-only">Unified diff</caption>
      <thead className="sr-only">
        <tr>
          <th scope="col">Old line</th>
          <th scope="col">New line</th>
          <th scope="col">Change</th>
          <th scope="col">Content</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={index} className={ROW_STYLE[row.kind]}>
            <td className="select-none px-2 text-right text-muted-foreground">
              {row.oldLine ?? ""}
            </td>
            <td className="select-none px-2 text-right text-muted-foreground">
              {row.newLine ?? ""}
            </td>
            {/* The sigil is text, not colour alone: colour is not available to every reader, and a
                diff whose meaning is carried only by a background is unreadable without it. */}
            <td className="select-none px-1">{ROW_SIGIL[row.kind]}</td>
            <td className="whitespace-pre-wrap px-2">{row.text}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SplitDiff({ rows }: { rows: DiffRow[] }) {
  return (
    <table className="w-full table-fixed border-collapse font-mono text-xs">
      <caption className="sr-only">Side-by-side diff</caption>
      <thead>
        <tr className="text-left text-muted-foreground">
          <th scope="col" colSpan={2} className="px-2 font-medium">
            Before
          </th>
          <th scope="col" colSpan={2} className="px-2 font-medium">
            After
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={index}>
            <td className="select-none px-2 text-right text-muted-foreground">
              {row.oldLine ?? ""}
            </td>
            <td
              className={`whitespace-pre-wrap px-2 ${row.kind === "added" ? "" : ROW_STYLE[row.kind]}`}
            >
              {row.kind === "added" ? "" : row.text}
            </td>
            <td className="select-none px-2 text-right text-muted-foreground">
              {row.newLine ?? ""}
            </td>
            <td
              className={`whitespace-pre-wrap px-2 ${row.kind === "removed" ? "" : ROW_STYLE[row.kind]}`}
            >
              {row.kind === "removed" ? "" : row.text}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ItemDiff({ item, mode }: { item: ChangeItemRead; mode: ViewMode }) {
  const rows = useMemo(
    () => diffLines(item.old_content ?? "", item.new_content ?? ""),
    [item.old_content, item.new_content],
  );

  return (
    <section className="rounded-md border border-border" aria-label={`Diff for ${item.file_path}`}>
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border px-3 py-2">
        <code className="text-sm font-semibold">{item.file_path}</code>
        <span className="text-xs uppercase tracking-wide text-muted-foreground">{item.action}</span>
      </header>
      <div className="overflow-x-auto">
        {mode === "unified" ? <UnifiedDiff rows={rows} /> : <SplitDiff rows={rows} />}
      </div>
      {/* The hash the backend recorded, shown rather than recomputed here. §12.6 step 10 verifies
          the file on disk against this value; a hash computed in the browser would only be checking
          the browser against itself. */}
      {item.new_hash ? (
        <footer className="border-t border-border px-3 py-2 text-xs text-muted-foreground">
          Recorded new content hash: <code className="break-all">{item.new_hash}</code>
        </footer>
      ) : null}
    </section>
  );
}

// ── the screen ──────────────────────────────────────────────────────────────

export function ApprovalCenter() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [mode, setMode] = useState<ViewMode>("unified");
  const [comment, setComment] = useState("");
  const [decisionError, setDecisionError] = useState<string | null>(null);

  const list = useQuery({
    queryKey: queryKeys.approvals.list(DECIDABLE),
    queryFn: () => api.get<ChangeSetPage>(`/approvals?status=${DECIDABLE}&limit=50`),
    retry: false,
  });

  const detail = useQuery({
    queryKey: queryKeys.approvals.detail(selected ?? ""),
    queryFn: () => api.get<ChangeSetDetail>(`/approvals/${selected}`),
    enabled: selected !== null,
    retry: false,
  });

  const decide = useMutation({
    mutationFn: async ({
      id,
      action,
      version,
    }: {
      id: string;
      action: "approve" | "reject";
      version: number;
    }) =>
      api.post<unknown>(`/approvals/${id}/${action}`, {
        comment: comment.trim() === "" ? null : comment.trim(),
        // The version THIS screen displayed. A stale tab therefore gets a 409 rather than deciding
        // on state it never showed the reviewer.
        expected_version: version,
      }),
    onSuccess: async () => {
      setComment("");
      setDecisionError(null);
      setSelected(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.approvals.all });
    },
    onError: (error: unknown) => {
      const problem = error instanceof ApiProblemError ? error.problem : null;
      if (problem?.status === 409) {
        setDecisionError(
          "This change set moved since it was displayed, so the decision was refused rather than " +
            "applied to state you did not review. Reloading will show its current form.",
        );
        return;
      }
      setDecisionError(problem?.detail ?? problem?.title ?? "The decision could not be recorded.");
    },
  });

  const current = detail.data;

  return (
    <div className="space-y-6">
      <AsyncState
        isPending={list.isPending}
        error={list.error}
        isEmpty={list.data?.change_sets.length === 0}
        emptyMessage="No change sets are awaiting a decision. A generation run that passes validation and trips the approval rule will appear here."
        label="change sets awaiting decision"
      >
        <div className="space-y-2">
          <h2 className="text-lg font-semibold">Awaiting decision</h2>
          <ul className="space-y-2">
            {list.data?.change_sets.map((cs) => (
              <li key={cs.id}>
                <button
                  type="button"
                  aria-pressed={selected === cs.id}
                  onClick={() => {
                    setSelected(cs.id);
                    setDecisionError(null);
                  }}
                  className={`w-full rounded-md border p-3 text-left text-sm ${
                    selected === cs.id ? "border-primary bg-primary/5" : "border-border"
                  }`}
                >
                  <span className="flex flex-wrap items-baseline justify-between gap-2">
                    <code className="font-semibold">{cs.id}</code>
                    <span className="text-xs text-muted-foreground">
                      {cs.origin} · blast radius {cs.blast_radius_score}
                      {cs.blast_radius_verdict ? ` (${cs.blast_radius_verdict})` : ""} · v
                      {cs.version}
                    </span>
                  </span>
                  <span className="mt-1 block text-xs text-muted-foreground">{cs.status}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </AsyncState>

      {selected !== null ? (
        <AsyncState
          isPending={detail.isPending}
          error={detail.error}
          label={`change set ${selected}`}
        >
          {current ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h2 className="text-lg font-semibold">Proposed changes</h2>
                <div role="group" aria-label="Diff view mode" className="flex gap-1">
                  {(["unified", "split"] as const).map((option) => (
                    <Button
                      key={option}
                      variant={mode === option ? "default" : "outline"}
                      size="sm"
                      aria-pressed={mode === option}
                      onClick={() => setMode(option)}
                    >
                      {option === "unified" ? "Unified" : "Side by side"}
                    </Button>
                  ))}
                </div>
              </div>

              <div className="space-y-4">
                {current.items
                  .slice()
                  .sort((x, y) => x.ordinal - y.ordinal)
                  .map((item) => (
                    <ItemDiff key={item.id} item={item} mode={mode} />
                  ))}
              </div>

              {current.approvals.length > 0 ? (
                <section aria-label="Decisions already recorded" className="text-sm">
                  <h3 className="font-semibold">Decisions recorded</h3>
                  <ul className="mt-2 space-y-1 text-muted-foreground">
                    {current.approvals.map((a) => (
                      <li key={a.id}>
                        {a.status} by <code>{a.approver_id}</code> on <time>{a.created_at}</time>
                        {a.comment ? ` — ${a.comment}` : ""}
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}

              {current.status === DECIDABLE ? (
                <section
                  aria-label="Record a decision"
                  className="space-y-3 rounded-md border border-border p-4"
                >
                  <label className="block text-sm font-medium" htmlFor="decision-comment">
                    Reason
                  </label>
                  <textarea
                    id="decision-comment"
                    value={comment}
                    onChange={(event) => setComment(event.target.value)}
                    rows={3}
                    maxLength={4096}
                    className="w-full rounded-md border border-border bg-background p-2 text-sm"
                    placeholder="Why this change set is being approved or rejected."
                  />
                  <p className="text-xs text-muted-foreground">
                    Recorded against your authenticated identity. There is no field for the
                    approver: the server takes it from your session, so a decision cannot be
                    attributed to anyone else.
                  </p>

                  {decisionError ? (
                    <p role="alert" className="text-sm text-destructive">
                      {decisionError}
                    </p>
                  ) : null}

                  <div className="flex gap-2">
                    <Button
                      disabled={decide.isPending}
                      onClick={() =>
                        decide.mutate({
                          id: current.id,
                          action: "approve",
                          version: current.version,
                        })
                      }
                    >
                      {decide.isPending ? "Recording…" : "Approve"}
                    </Button>
                    <Button
                      variant="destructive"
                      disabled={decide.isPending}
                      onClick={() =>
                        decide.mutate({
                          id: current.id,
                          action: "reject",
                          version: current.version,
                        })
                      }
                    >
                      Reject
                    </Button>
                  </div>
                </section>
              ) : (
                <p className="text-sm text-muted-foreground">
                  This change set is <code>{current.status}</code>, and §3.6 only permits a decision
                  from <code>{DECIDABLE}</code>. No decision controls are offered, rather than
                  offered and refused by the server.
                </p>
              )}
            </div>
          ) : null}
        </AsyncState>
      ) : (
        <p className="text-sm text-muted-foreground">
          Select a change set to review its diff. Nothing is selected by default, so one change
          set&apos;s diff is never shown under another&apos;s heading.
        </p>
      )}
    </div>
  );
}
