// SPDX-License-Identifier: Apache-2.0
"use client";

import React from "react";

export interface ChangeSetUI {
  id: string;
  summary: string;
  status: string;
  diff: string;
}

export function ApprovalCenter({ changeSets }: { changeSets: ChangeSetUI[] }) {
  return (
    <div className="border rounded-lg p-6 bg-background">
      <h3 className="text-xl font-bold mb-4">Change Approval Center</h3>
      <div className="space-y-4">
        {changeSets.map((cs) => (
          <div key={cs.id} className="border p-4 rounded space-y-2">
            <div className="flex justify-between items-center">
              <span className="font-bold text-sm">{cs.id}</span>
              <span className="text-xs font-semibold px-2 py-1 bg-muted rounded">{cs.status}</span>
            </div>
            <p className="text-sm">{cs.summary}</p>
            <pre className="p-2 bg-muted rounded text-xs overflow-x-auto">{cs.diff}</pre>
          </div>
        ))}
      </div>
    </div>
  );
}
