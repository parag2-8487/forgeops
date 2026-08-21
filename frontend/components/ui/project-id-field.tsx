// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import React from "react";

/**
 * A lookup by project id, for screens that act on ONE project rather than browse them.
 *
 * This comment used to say Phase 1 served no `GET /api/v1/projects` list endpoint, which is why a
 * field existed instead of a picker. That is no longer true — the list endpoint was added with real
 * persistence, and `/projects` now enumerates. The field remains because the generation screen acts
 * on a single project supplied by the operator, not because nothing can be listed.
 *
 * A valid UUID, because `project_id: uuid.UUID` on the route means anything else is a 422 from
 * FastAPI's validation rather than a reply from the handler. `isProjectId` is exported so a caller
 * can decline to offer an action that is certain to fail.
 */
export const DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000001";

/** The shape FastAPI will accept for a `uuid.UUID` path or body field. */
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isProjectId(value: string): boolean {
  return UUID_PATTERN.test(value.trim());
}

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
