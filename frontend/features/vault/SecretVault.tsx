// SPDX-License-Identifier: Apache-2.0
"use client";

import React from "react";

/**
 * A secret REFERENCE. Deliberately no value field: `SecretResponse` on the backend returns the
 * key, the environment and where the material lives, never the material. Typing it without a
 * value keeps that property visible in the client too, so a future edit cannot casually add one.
 */
export interface SecretRefUI {
  id: string;
  key: string;
  environment: string;
  infisical_path: string | null;
  is_local: boolean;
}

/**
 * The vault list, rendered from whatever the caller fetched.
 *
 * This used to hardcode a single row reading `DATABASE_PASSWORD — Vault Encrypted`, with no props
 * and no fetch: a secret that did not exist, described as encrypted by a component that had never
 * spoken to a vault. It takes real references now.
 */
export function SecretVault({ secrets }: { secrets: SecretRefUI[] }) {
  return (
    <div className="rounded-lg border border-border bg-background p-6">
      <h2 className="mb-4 text-xl font-bold">Secret references</h2>
      <ul className="divide-y divide-border">
        {secrets.map((s) => (
          <li key={s.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
            <div>
              <p className="font-mono text-sm font-semibold">{s.key}</p>
              <p className="text-xs text-muted-foreground">
                {s.environment}
                {s.infisical_path ? ` · ${s.infisical_path}` : ""}
              </p>
            </div>
            <span className="rounded bg-muted px-2 py-1 text-xs">
              {s.is_local ? "local" : "Infisical"}
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-4 text-xs text-muted-foreground">
        References only. The API returns the key, the environment and the storage path — never the
        secret material — so there is nothing here to reveal.
      </p>
    </div>
  );
}
