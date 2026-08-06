// SPDX-License-Identifier: Apache-2.0
"use client";

import React from "react";

export function SecretVault() {
  return (
    <div className="border rounded-lg p-6 bg-background">
      <h3 className="text-xl font-bold mb-4">Secret Vault Management</h3>
      <div className="border rounded p-4 bg-muted text-sm space-y-2">
        <div className="flex justify-between items-center">
          <span className="font-semibold">DATABASE_PASSWORD</span>
          <span className="text-xs bg-background px-2 py-1 rounded">Vault Encrypted</span>
        </div>
      </div>
    </div>
  );
}
