"use client";

/**
 * The exact instruction a generation run gave the model.
 *
 * WHY THIS SCREEN EXISTS. A run could be judged only by its output. The record held the tier, the
 * endpoint, the token counts and the outcome — everything except what the model was asked to do. So when
 * a run wrote a file to the wrong path, or missed a check the user had selected, there was no way to tell
 * whether the model had disobeyed a correct instruction or obeyed a bad one. Those two faults have
 * opposite fixes, and without the prompt a user cannot tell which one they are looking at.
 *
 * The column was written by the compiler and read by NOTHING, which is the shape of a dead column: the
 * write can rot indefinitely and every test still passes. This is the reader that makes it evidence.
 *
 * COLLAPSED BY DEFAULT. The compiled prompt runs to thousands of tokens and quotes the repository's own
 * files; unfurling that over the artifacts a user came to review would bury them.
 */

import { useCallback, useState } from "react";

import { Button } from "@/components/ui/button";
import { api, ApiProblemError } from "@/lib/api";

export interface GenerationRunRecord {
  id: string;
  status: string;
  compiled_prompt: string | null;
  prompt_token_estimate: number | null;
  prompt_token_budget: number | null;
  addressed_checks: string[];
  deferred_checks: string[];
}

export function CompiledPromptPanel({ runId }: { runId: string }) {
  const [record, setRecord] = useState<GenerationRunRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    // Fetched once and then toggled. Re-reading on every click would spend a request to redisplay text
    // that cannot have changed: a run's prompt is written before the stream and never updated.
    if (record !== null) {
      setOpen((previous) => !previous);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setRecord(await api.get<GenerationRunRecord>(`/api/v1/generation/runs/${runId}`));
      setOpen(true);
    } catch (caught) {
      if (caught instanceof ApiProblemError) {
        // The route answers 403 both for another tenant's run AND for one that does not exist, on
        // purpose — distinguishing them would enumerate run ids. So this cannot say which it was, and
        // must not guess: the server's own detail is the most specific honest thing available.
        setError(caught.problem.detail ?? caught.problem.title);
      } else {
        setError("Could not reach the server to read this run.");
      }
    } finally {
      setLoading(false);
    }
  }, [record, runId]);

  return (
    <section aria-label="Instruction given to the model" className="space-y-2">
      <Button
        variant="outline"
        size="sm"
        disabled={loading}
        onClick={() => void load()}
        data-testid="show-compiled-prompt"
      >
        {loading
          ? "Loading the instruction…"
          : open
            ? "Hide the instruction given to the model"
            : "Show the instruction given to the model"}
      </Button>

      {error !== null ? (
        <p className="text-sm text-destructive" data-testid="compiled-prompt-error">
          {error}
        </p>
      ) : null}

      {open && record !== null ? (
        <div className="space-y-2">
          {record.compiled_prompt === null ? (
            /* NOT the same as an empty prompt. A run made before the compiler existed, or one driven by
               free text with no readiness findings behind it, recorded nothing here — and saying so is
               honest, where an empty box would claim the model was sent nothing. */
            <p className="text-sm text-muted-foreground" data-testid="compiled-prompt-absent">
              This run did not record a compiled instruction. Runs driven by a free-text prompt, and
              runs made before the compiler existed, have nothing stored here.
            </p>
          ) : (
            <>
              <p className="text-xs text-muted-foreground" data-testid="prompt-token-summary">
                {record.prompt_token_estimate ?? "an unrecorded number of"} estimated tokens of a{" "}
                {record.prompt_token_budget ?? "unrecorded"} budget
              </p>

              {record.addressed_checks.length > 0 ? (
                <p className="text-xs text-muted-foreground" data-testid="addressed-checks">
                  Set out to fix: {record.addressed_checks.join(", ")}
                </p>
              ) : null}

              {/* DEFERRED IS THE LOAD-BEARING ONE. A user looking at a score that did not move needs to
                  know a check was never attempted, rather than attempted and rejected — only the first is
                  fixed by narrowing the request or raising the budget. */}
              {record.deferred_checks.length > 0 ? (
                <p className="text-xs text-muted-foreground" data-testid="deferred-checks">
                  Left out because the prompt budget could not hold them:{" "}
                  {record.deferred_checks.join(", ")}
                </p>
              ) : null}

              <pre
                className="max-h-96 overflow-auto rounded-md border border-border p-3 text-xs"
                data-testid="compiled-prompt"
              >
                {record.compiled_prompt}
              </pre>
            </>
          )}
        </div>
      ) : null}
    </section>
  );
}
