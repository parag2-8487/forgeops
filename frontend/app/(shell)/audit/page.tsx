// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";
import { AuditViewer, type AuditEventUI } from "@/features/audit/AuditViewer";
import { useCapability } from "@/hooks/use-role";

const PAGE_LIMIT = 50;

/** Mirrors `AuditPage`: a page plus the cursor that fetches the next one. */
interface AuditPage {
  events: AuditEventUI[];
  next_cursor: number | null;
}

/** Mirrors `ChainVerificationOut`. `ok` is derived from `divergence`, never a separate flag. */
interface ChainVerification {
  ok: boolean;
  tenant_id: string | null;
  from_seq: number;
  rows_checked: number;
  divergence: {
    seq: number;
    kind: string;
    detail: string;
    expected_hash: string;
    stored_hash: string;
  } | null;
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

      <ChainVerificationPanel />

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
          independently, and the panel above is that recomputation — run by the server over its own
          rows, which is why the verdict below is a starting point for an investigation rather than
          the end of one.
        </p>
        <p className="mt-2">
          <strong className="text-foreground">
            This log records authorisation decisions, not outcomes.
          </strong>{" "}
          Every action it can carry is something the platform itself decided at a named stage — a
          mutation refused, a policy deny, a blast radius blocked, a human approving or rejecting, a
          revert authorised. There is deliberately no <code>applied</code> action, so{" "}
          <em>was this change actually applied?</em> is not a question to ask here. Application is
          reported afterwards by the agent about a machine this server does not control, and
          recording that alongside the decisions would make &ldquo;the log says so&rdquo; ambiguous
          at exactly the point after an incident where it must not be. The authoritative answer is
          the change set&apos;s own status: see{" "}
          <Link href="/approvals" className="underline hover:no-underline">
            Approvals
          </Link>
          , which lists applied, pending, rejected and failed change sets as separate queues, and
          the change history on each project&apos;s page.
        </p>
      </aside>
    </div>
  );
}

/**
 * Verify the hash chain — `GET /api/v1/audit/verify`, ADMIN only.
 *
 * `main.py`'s own comment about this route says it is registered "rather than behind a feature flag:
 * `GET /verify` is what makes tamper evidence a product feature, and a feature nobody can reach is a
 * claim rather than a control". It was in exactly that state: served, tested, and called by nothing.
 * So the tamper-evidence property was an assertion in a document.
 *
 * ADMIN-ONLY, AND FOR A REASON THAT IS NOT SECRECY. The result is a hash comparison and reveals
 * nothing sensitive. The operation reads every row from `since_seq` onward, so an unbounded
 * recomputation available to any authenticated caller is a cheap way to make the database everybody's
 * problem. `since_seq` is what makes an incremental check possible on a large table, so it is exposed
 * as a control rather than hidden.
 *
 * A DIVERGENCE IS A 200. The endpoint answers `200 {ok: false}` rather than a 5xx, because a
 * divergence is a successful verification that found something — and returning 5xx would make "the
 * chain is broken" indistinguishable from "the verifier is broken", which need opposite responses.
 * This panel keeps that distinction: a divergence renders as a finding, and a transport failure
 * renders through the problem path.
 */
function ChainVerificationPanel() {
  const [sinceSeq, setSinceSeq] = useState(0);
  const [requested, setRequested] = useState<number | null>(null);
  const { allowed, reason } = useCapability("verify_audit_chain");

  const verification = useQuery({
    queryKey: queryKeys.audit.verify(requested ?? 0),
    queryFn: () => api.get<ChainVerification>(`/audit/verify?since_seq=${requested}`),
    enabled: requested !== null && allowed,
    // Never from cache: the point of pressing this button is to check the chain as it is NOW, and a
    // cached verdict from four minutes ago is precisely the wrong answer to that question.
    gcTime: 0,
    staleTime: 0,
    retry: false,
  });

  if (!allowed) {
    return (
      <section
        aria-labelledby="verify-heading"
        className="rounded-lg border border-border bg-background p-4"
      >
        <h2 id="verify-heading" className="text-sm font-semibold">
          Verify the hash chain
        </h2>
        <p className="mt-2 text-xs text-muted-foreground">
          {reason} The verification is not offered here rather than offered and refused.
        </p>
      </section>
    );
  }

  return (
    <section
      aria-labelledby="verify-heading"
      className="space-y-3 rounded-lg border border-border bg-background p-4"
    >
      <h2 id="verify-heading" className="text-sm font-semibold">
        Verify the hash chain
      </h2>
      <p className="text-xs text-muted-foreground">
        Recomputes every record&apos;s hash from its own fields and its predecessor&apos;s hash, and
        reports the first row where the stored value disagrees. This is what makes the log
        tamper-evident rather than merely append-only: the table also refuses UPDATE and DELETE from
        the application role, so a divergence means something reached the database another way.
      </p>

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          setRequested(sinceSeq);
        }}
      >
        <div>
          <label htmlFor="since-seq" className="block text-sm font-medium">
            From sequence
          </label>
          <input
            id="since-seq"
            type="number"
            min={0}
            value={sinceSeq}
            onChange={(event) => setSinceSeq(Number(event.target.value) || 0)}
            aria-describedby="since-seq-help"
            className="mt-1 w-32 rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <p id="since-seq-help" className="mt-1 text-xs text-muted-foreground">
            0 checks the whole chain. A higher value checks incrementally, which is what makes this
            usable on a large table.
          </p>
        </div>
        <button
          type="submit"
          disabled={verification.isFetching}
          data-testid="verify-chain"
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {verification.isFetching ? "Recomputing…" : "Verify"}
        </button>
      </form>

      {verification.data ? (
        verification.data.ok ? (
          <div
            role="status"
            data-testid="verification-ok"
            className="rounded-md border border-emerald-500/40 bg-emerald-500/5 p-3 text-sm"
          >
            <p className="font-semibold">The chain is intact.</p>
            <p className="mt-1 text-muted-foreground">
              {verification.data.rows_checked} record
              {verification.data.rows_checked === 1 ? "" : "s"} recomputed from sequence{" "}
              {verification.data.from_seq} onward, and every stored hash matched the value derived
              from its own fields and its predecessor.
            </p>
            {verification.data.rows_checked === 0 ? (
              <p className="mt-1 text-muted-foreground">
                <strong>Nothing was checked.</strong> Zero rows in range is not evidence of
                integrity — it means the chain is empty or `since_seq` is past its end.
              </p>
            ) : null}
          </div>
        ) : (
          <div
            role="alert"
            data-testid="verification-divergence"
            className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"
          >
            <p className="font-semibold">
              The chain diverges at sequence {verification.data.divergence?.seq}.
            </p>
            <p className="mt-1 text-muted-foreground">
              A record&apos;s stored hash does not match the value recomputed from its contents. The
              application role cannot UPDATE or DELETE this table and a trigger raises on either, so
              this was not written through the API — it means something modified the database
              directly, or the row was written by a build whose hashing differed. Treat it as an
              incident.
            </p>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
              <dt className="font-medium">Kind</dt>
              <dd>{verification.data.divergence?.kind}</dd>
              <dt className="font-medium">Detail</dt>
              <dd>{verification.data.divergence?.detail}</dd>
              <dt className="font-medium">Expected</dt>
              <dd>
                <code className="break-all">{verification.data.divergence?.expected_hash}</code>
              </dd>
              <dt className="font-medium">Stored</dt>
              <dd>
                <code className="break-all">{verification.data.divergence?.stored_hash}</code>
              </dd>
              <dt className="font-medium">Rows checked</dt>
              <dd>{verification.data.rows_checked}</dd>
            </dl>
          </div>
        )
      ) : null}

      <GovernanceRefusal error={verification.error} action="verify the audit chain" />
    </section>
  );
}
