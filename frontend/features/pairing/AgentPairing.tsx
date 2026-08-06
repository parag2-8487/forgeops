// SPDX-License-Identifier: Apache-2.0
"use client";

import React from "react";

export function AgentPairing() {
  return (
    <div className="border rounded-lg p-6 bg-background space-y-4">
      <h3 className="text-xl font-bold">Agent Pairing & Workload Attestation</h3>
      <div className="p-4 border rounded bg-muted text-sm space-y-1">
        <p className="font-semibold">SPIFFE Trust Domain: spiffe://cluster.local</p>
        <p className="text-xs text-muted-foreground">Status: Connected & Attested</p>
      </div>
    </div>
  );
}
