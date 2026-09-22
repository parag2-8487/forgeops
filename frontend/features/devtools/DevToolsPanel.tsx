"use client";

/**
 * §2.8's dev-tools panel.
 *
 * Five buttons, one per kind, and no free-text field — the same constraint the operation has: a panel that
 * could type a command would need the agent to accept one.
 *
 * Every run is a mutation and reports a GOVERNANCE OUTCOME, not a result. "Run the tests" sounds read-only
 * and is not: the command is code the repository controls. The panel says so, because an operator who thinks
 * a button is harmless will press it on production credentials.
 */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/lib/api";

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
    <section aria-label="Developer tools" data-testid="devtools-panel">
      <p data-testid="devtools-warning">
        Each of these runs a command this repository defines, on the machine your agent runs on.
        They are mutations and go through the same approval path as a deployment.
      </p>
      <ul>
        {KINDS.map((kind) => (
          <li key={kind}>
            <button
              type="button"
              data-testid={`devtools-run-${kind}`}
              disabled={run.isPending}
              onClick={() => run.mutate({ kind })}
            >
              {kind}
            </button>{" "}
            <span data-testid={`devtools-describe-${kind}`}>{KIND_DESCRIPTIONS[kind]}</span>{" "}
            {outcomes[kind] && (
              <span data-testid={`devtools-outcome-${kind}`}>
                {outcomes[kind] === "applying"
                  ? "sent to the agent"
                  : outcomes[kind] === "approval-required"
                    ? "waiting for a human to approve it — nothing has run yet"
                    : outcomes[kind]}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
