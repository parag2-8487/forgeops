"use client";

/**
 * The AI Command Center. Phase 2 §2.5.
 *
 * THE UI ENFORCES THE CONFIRMATION STEP, and that is its most important property. Typing a sentence calls
 * `interpret`, which changes nothing and returns a plan. A mutating plan renders a confirmation with the
 * resolved operation and arguments VISIBLE, and only pressing that button calls `execute`. There is no code
 * path here that sends a sentence straight to execution.
 *
 * WHAT THE PANEL SHOWS THE USER, and why each matters:
 *
 *   - the resolved OPERATION and ARGUMENTS, verbatim. A user confirming "deploy to staging" should see
 *     `deployments.create {environment: staging}`, because the whole risk of natural language is the gap
 *     between what was said and what was understood, and this closes it before anything runs.
 *   - the CONFIDENCE, when it is low. A guess presented identically to a certainty invites the user to
 *     confirm without reading.
 *   - a MISSING SLOT as a question, not a default. "Deploy" with no environment asks which one.
 *   - a REFUSAL with its reason, in the history, so "nothing happened" is never the whole story.
 *
 * Autocomplete comes from the server's published catalogue rather than a local list, so what the input
 * suggests is exactly what the system can do -- a local list would drift and start suggesting commands that
 * no longer resolve.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/lib/api/client";

export interface CommandSpecRow {
  intent: string;
  category: string;
  mutating: boolean;
  describes: string;
  examples: string[];
  required_slots: string[];
}

export interface InterpretedPlan {
  intent: string;
  category: string;
  operation: string;
  arguments: Record<string, unknown>;
  mutating: boolean;
  ready: boolean;
  confidence: number;
  describes: string;
  explanation: string;
  executed: boolean;
}

export interface HistoryRow {
  id: string;
  utterance: string;
  intent: string;
  outcome: "interpreted" | "dispatched" | "read" | "refused";
  detail: string;
  session_key: string;
  created_at: string;
}

/** One sentence per outcome. `refused` is a received-and-declined, not a silence. */
const OUTCOME_SENTENCE: Record<HistoryRow["outcome"], string> = {
  interpreted: "understood, and shown to you for confirmation",
  dispatched: "dispatched to the domain that owns it, where it faces policy and audit",
  read: "answered as a read; nothing was changed",
  refused: "received and declined",
};

export function CommandInput({ projectId, sessionKey }: { projectId: string; sessionKey: string }) {
  const [utterance, setUtterance] = useState("");
  const [plan, setPlan] = useState<InterpretedPlan | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);

  const catalogue = useQuery<{ commands: CommandSpecRow[]; explanation: string }>({
    queryKey: ["commands", "catalogue"],
    queryFn: async () => api.get("/commands/catalogue"),
  });

  const interpret = useMutation({
    mutationFn: async (text: string) =>
      api.post<InterpretedPlan>("/commands/interpret", {
        utterance: text,
        session_key: sessionKey,
      }),
    onSuccess: (result) => {
      setPlan(result);
      setRefusal(null);
      setOutcome(null);
    },
    onError: (error: unknown) => {
      setPlan(null);
      setRefusal(
        error instanceof Error && error.message
          ? error.message
          : "That command was not understood, and nothing was done.",
      );
    },
  });

  const run = useMutation({
    mutationFn: async (confirmed: InterpretedPlan) =>
      api.post<{ explanation: string; dispatched: boolean }>("/commands/execute", {
        // THE INTENT AND SLOTS, never the operation. The server rebuilds the operation from its own table,
        // so there is nothing here a tampered client could redirect.
        intent: confirmed.intent,
        slots: confirmed.arguments,
        project_id: projectId,
        session_key: sessionKey,
      }),
    onSuccess: (result) => {
      setOutcome(result.explanation);
      setPlan(null);
      setUtterance("");
    },
  });

  return (
    <section aria-labelledby="command-heading" data-testid="command-center">
      <h2 id="command-heading">Command</h2>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (utterance.trim()) interpret.mutate(utterance);
        }}
      >
        <label>
          Say what you want
          <input
            value={utterance}
            onChange={(event) => setUtterance(event.target.value)}
            list="command-suggestions"
            data-testid="command-input"
            placeholder="deploy to staging"
          />
        </label>
        {/* From the SERVER's catalogue, so suggestions cannot drift from what actually resolves. */}
        <datalist id="command-suggestions" data-testid="command-suggestions">
          {(catalogue.data?.commands ?? []).flatMap((spec) =>
            spec.examples.map((example) => (
              <option key={`${spec.intent}-${example}`} value={example} />
            )),
          )}
        </datalist>
        <button type="submit">Interpret</button>
      </form>

      {catalogue.data ? (
        <p data-testid="command-catalogue-note">{catalogue.data.explanation}</p>
      ) : null}

      {refusal ? (
        <p role="alert" data-testid="command-refusal">
          {refusal} Nothing was done.
        </p>
      ) : null}

      {plan ? (
        <div data-testid="command-plan">
          <h3>Before anything happens</h3>
          <p data-testid="command-plan-explanation">{plan.explanation}</p>

          {/* THE RESOLVED OPERATION AND ARGUMENTS, verbatim. The gap between what was said and what was
              understood is the whole risk of natural language, and this closes it before anything runs. */}
          <dl>
            <dt>This will run</dt>
            <dd data-testid="command-plan-operation">
              <code>{plan.operation || "nothing — it is answered without an operation"}</code>
            </dd>
            <dt>With exactly these values</dt>
            <dd data-testid="command-plan-arguments">
              {Object.keys(plan.arguments).length === 0 ? (
                "no values"
              ) : (
                <code>{JSON.stringify(plan.arguments)}</code>
              )}
            </dd>
          </dl>

          {plan.confidence < 0.6 ? (
            <p role="alert" data-testid="command-low-confidence">
              This was a weak match ({Math.round(plan.confidence * 100)}% confidence). Read the
              operation above before confirming: a guess shown the same way as a certainty invites
              confirming without looking.
            </p>
          ) : null}

          {plan.ready ? (
            <button type="button" data-testid="command-confirm" onClick={() => run.mutate(plan)}>
              {plan.mutating ? "Confirm and dispatch" : "Run this read"}
            </button>
          ) : (
            <p data-testid="command-incomplete">
              Tell me the missing value and try again. Nothing has been assumed on your behalf.
            </p>
          )}
        </div>
      ) : null}

      {outcome ? (
        <p role="status" data-testid="command-outcome">
          {outcome}
        </p>
      ) : null}
    </section>
  );
}

export function CommandHistory({ sessionKey }: { sessionKey: string }) {
  const query = useQuery<{ history: HistoryRow[]; explanation: string }>({
    queryKey: ["commands", "history", sessionKey],
    queryFn: async () => api.get(`/commands/history?session_key=${encodeURIComponent(sessionKey)}`),
    refetchInterval: 20_000,
  });

  return (
    <section aria-labelledby="command-history-heading" data-testid="command-history">
      <h2 id="command-history-heading">This session&apos;s commands</h2>

      {query.isLoading ? (
        <p role="status">Reading the history.</p>
      ) : query.error ? (
        <p role="alert" data-testid="command-history-error">
          The command history could not be loaded, so it is not known what has been asked. An empty
          history is not being shown in its place.
        </p>
      ) : (
        <>
          <p data-testid="command-history-explanation">{query.data?.explanation}</p>

          {query.data && query.data.history.length > 0 ? (
            <ol>
              {query.data.history.map((row) => (
                <li key={row.id} data-testid={`command-history-${row.id}`}>
                  {row.utterance ? <q>{row.utterance}</q> : <em>{row.intent}</em>}
                  {" — "}
                  <span data-testid={`command-outcome-${row.id}`}>
                    {OUTCOME_SENTENCE[row.outcome]}
                  </span>
                  {row.detail ? <p>{row.detail}</p> : null}
                </li>
              ))}
            </ol>
          ) : null}
        </>
      )}
    </section>
  );
}
