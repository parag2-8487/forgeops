// SPDX-License-Identifier: Apache-2.0
"use client";

import React, { useState } from "react";

export function PolicyEditor() {
  const [regoCode, setRegoCode] = useState("package devops.security\n\ndefault allow = false\n");

  return (
    <div className="border rounded-lg p-6 bg-background space-y-4">
      <h3 className="text-xl font-bold">OPA Policy Editor</h3>
      <textarea
        value={regoCode}
        onChange={(e) => setRegoCode(e.target.value)}
        className="w-full h-40 p-2 font-mono text-xs border rounded bg-muted"
      />
      <button className="px-4 py-2 bg-primary text-primary-foreground rounded text-sm font-medium">
        Validate & Save Policy
      </button>
    </div>
  );
}
