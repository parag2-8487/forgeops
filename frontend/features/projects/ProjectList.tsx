// SPDX-License-Identifier: Apache-2.0
"use client";

import React, { useState } from "react";

export interface ProjectItem {
  id: string;
  name: string;
  repository: string;
  readinessScore: number;
}

export function ProjectList({ projects }: { projects: ProjectItem[] }) {
  const [selectedId, setSelectedId] = useState<string | null>(projects[0]?.id || null);

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-6 p-6">
      <div className="md:col-span-1 border rounded-lg p-4 bg-background">
        <h2 className="text-xl font-bold mb-4">Projects</h2>
        <ul className="space-y-2">
          {projects.map((p) => (
            <li
              key={p.id}
              onClick={() => setSelectedId(p.id)}
              className={`p-3 rounded cursor-pointer border ${
                selectedId === p.id ? "border-primary bg-muted" : "border-border"
              }`}
            >
              <div className="font-semibold">{p.name}</div>
              <div className="text-xs text-muted-foreground">{p.repository}</div>
            </li>
          ))}
        </ul>
      </div>

      <div className="md:col-span-2 border rounded-lg p-4 bg-background">
        <h2 className="text-xl font-bold mb-4">Project Detail</h2>
        {selectedId ? (
          <div>
            <p className="text-sm font-medium">Selected ID: {selectedId}</p>
            <p className="text-sm text-muted-foreground mt-2">
              Viewing details for {projects.find((p) => p.id === selectedId)?.name}
            </p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Select a project from the list.</p>
        )}
      </div>
    </div>
  );
}
