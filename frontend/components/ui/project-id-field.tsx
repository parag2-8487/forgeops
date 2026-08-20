// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import React from "react";

/**
 * Phase 1 serves no `GET /api/v1/projects` list endpoint — only create, get-by-id, activity and
 * readiness. So a projects screen cannot enumerate anything, and the honest interface is a
 * lookup by id rather than a list that quietly renders fixtures.
 *
 * A valid UUID, because `project_id: uuid.UUID` on the route means anything else is a 422 from
 * FastAPI's validation rather than a reply from the handler.
 */
export const DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000001";

export function ProjectIdField({
  value,
  onChange,
  id = "project-id",
}: {
  value: string;
  onChange: (next: string) => void;
  id?: string;
}) {
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
      <div className="flex-1">
        <label htmlFor={id} className="block text-sm font-medium">
          Project ID
        </label>
        <input
          id={id}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          spellCheck={false}
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
    </div>
  );
}
