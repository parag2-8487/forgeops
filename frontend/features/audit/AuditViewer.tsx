// SPDX-License-Identifier: Apache-2.0
"use client";

import React from "react";

/**
 * One audit record as the API renders it. A subset of `AuditEventOut`: the fields this view
 * shows, rather than all of them.
 */
export interface AuditEventUI {
  seq: number;
  id: string;
  action: string;
  actor_kind: string;
  resource_kind: string;
  resource_id: string | null;
  outcome: string;
  reason: string;
}

/**
 * The event log, rendered from whatever the caller fetched.
 *
 * This component used to hardcode two lines — `USER_APPROVE cs-101 by alice` and a policy
 * evaluation — with no props and no fetch. They were invented, and in an audit viewer of all
 * places that is the worst possible thing to invent: the feature's entire claim is that the log
 * is tamper-evident, and a fabricated row undermines it more thoroughly than a missing screen
 * would. It takes real records now, and `seq` is shown because the sequence number is what the
 * hash chain is over.
 */
export function AuditViewer({ events }: { events: AuditEventUI[] }) {
  return (
    <div className="rounded-lg border border-border bg-background p-6">
      <h2 className="mb-4 text-xl font-bold">Audit event log</h2>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <caption className="sr-only">Audit events, newest first</caption>
          <thead className="text-muted-foreground">
            <tr className="border-b border-border">
              <th scope="col" className="py-2 pr-3 font-medium">
                Seq
              </th>
              <th scope="col" className="py-2 pr-3 font-medium">
                Action
              </th>
              <th scope="col" className="py-2 pr-3 font-medium">
                Actor
              </th>
              <th scope="col" className="py-2 pr-3 font-medium">
                Resource
              </th>
              <th scope="col" className="py-2 pr-3 font-medium">
                Outcome
              </th>
              <th scope="col" className="py-2 font-medium">
                Reason
              </th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {events.map((e) => (
              <tr key={e.id} className="border-b border-border/50 last:border-0">
                <td className="py-2 pr-3">{e.seq}</td>
                <td className="py-2 pr-3">{e.action}</td>
                <td className="py-2 pr-3">{e.actor_kind}</td>
                <td className="py-2 pr-3">
                  {e.resource_kind}
                  {e.resource_id ? `/${e.resource_id}` : ""}
                </td>
                <td className="py-2 pr-3">{e.outcome}</td>
                <td className="py-2 font-sans">{e.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
