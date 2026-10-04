// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * §2.8's dev-tools panel.
 */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const KINDS = ["tests", "lint", "build", "compose", "migrations"] as const;
export type DevToolKind = (typeof KINDS)[number];

/** What each kind does, in the words the panel shows. */
export const KIND_DESCRIPTIONS: Record<DevToolKind, string> = {
  tests: "runs the project's own test suite on your machine",
  lint: "runs the project's own linters",
  build: "runs the project's own build, which writes artifacts",
  compose: "starts the local compose stack (it never stops one or removes a volume)",
  migrations: "applies the project's database migrations",
};

type ActionAccepted = { change_set_id: string; status: string; outcome: string };

export function DevToolsPanel({ projectId }: { projectId: string }) {
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});

  const run = useMutation<ActionAccepted, Error, { kind: DevToolKind }>({
    mutationFn: (body) => api.post<ActionAccepted>(`/projects/${projectId}/devtools/run`, body),
    onSuccess: (accepted, variables) => {
      setOutcomes((previous) => ({ ...previous, [variables.kind]: accepted.outcome }));
    },
    onError: (error, variables) => {
      setOutcomes((previous) => ({ ...previous, [variables.kind]: `refused: ${error.message}` }));
    },
  });

  return (
    <Card
      aria-label="Developer tools"
      data-testid="devtools-panel"
      className="border border-border"
    >
      <CardHeader>
        <CardTitle className="text-base font-semibold">Repository developer tools</CardTitle>
        <CardDescription data-testid="devtools-warning">
          Each of these runs a command this repository defines, on the machine your agent runs on.
          They are mutations and go through the same approval path as a deployment.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="space-y-3">
          {KINDS.map((kind) => (
            <li
              key={kind}
              className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-border bg-card p-3 shadow-sm"
            >
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  data-testid={`devtools-run-${kind}`}
                  disabled={run.isPending}
                  onClick={() => run.mutate({ kind })}
                  className="font-mono font-semibold min-w-28 uppercase text-xs"
                >
                  {kind}
                </Button>
                <span
                  data-testid={`devtools-describe-${kind}`}
                  className="text-xs text-muted-foreground"
                >
                  {KIND_DESCRIPTIONS[kind]}
                </span>
              </div>

              {outcomes[kind] && (
                <div data-testid={`devtools-outcome-${kind}`}>
                  <Badge
                    variant={
                      outcomes[kind] === "applying"
                        ? "success"
                        : outcomes[kind] === "approval-required"
                          ? "warning"
                          : "outline"
                    }
                  >
                    {outcomes[kind] === "applying"
                      ? "sent to the agent"
                      : outcomes[kind] === "approval-required"
                        ? "waiting for a human to approve it — nothing has run yet"
                        : outcomes[kind]}
                  </Badge>
                </div>
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
