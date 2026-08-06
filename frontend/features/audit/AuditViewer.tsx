// SPDX-License-Identifier: Apache-2.0
"use client";

import React from "react";

export function AuditViewer() {
  return (
    <div className="border rounded-lg p-6 bg-background">
      <h3 className="text-xl font-bold mb-4">Audit Event Log Viewer</h3>
      <div className="border rounded p-4 font-mono text-xs bg-muted space-y-1">
        <div>[2026-08-06T18:00:00Z] USER_APPROVE cs-101 by alice</div>
        <div>[2026-08-06T18:05:00Z] POLICY_EVAL pass policy-sec-01</div>
      </div>
    </div>
  );
}
