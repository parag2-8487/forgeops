"use client";

import { CommandBlock } from "@/features/agent/CommandBlock";
import type { ReadinessCheck } from "./types";
import { suggestGenerationPrompt } from "./generation-prompt";

/**
 * What generation can do about this project's score, stated honestly.
 *
 * THE FAILURE THIS AVOIDS. A user reading a readiness report reasonably asks "can I paste this into
 * the generator?". Pasting the recommendations produces a Dockerfile that addresses none of them,
 * because generation emits four files and most recommendations name something else entirely — a CI
 * workflow, a `.env.example`, a Terraform backend. The run takes minutes, the score does not move,
 * and nothing explains why. Offering a prompt that cannot work is worse than offering none.
 *
 * So this offers a prompt only when a failing check is one generation can satisfy, and otherwise says
 * which artifacts the remaining failures need. Either way the user knows where they stand before
 * spending a generation run.
 */
export function GenerationPromptSuggestion({ checks }: { checks: ReadonlyArray<ReadinessCheck> }) {
  const suggestion = suggestGenerationPrompt(checks);

  // Nothing failing at all: no advice to give, and saying "generation cannot help" would read as a
  // limitation rather than as a full score.
  if (suggestion.prompt === null && suggestion.outOfScope.length === 0) return null;

  return (
    <div
      className="rounded-lg border border-border bg-background p-4"
      data-testid="generation-prompt-suggestion"
    >
      <h2 className="text-sm font-semibold">Improving this score with generation</h2>

      {suggestion.prompt === null ? (
        <p className="mt-2 text-sm text-muted-foreground" data-testid="generation-cannot-help">
          Generation cannot raise this score. It writes a Dockerfile and Kubernetes manifests, and
          every check still failing here needs something else: {suggestion.outOfScope.join(", ")}.
          Those are edits to make yourself — a change set from the generator would not touch them.
        </p>
      ) : (
        <>
          <p className="mt-2 text-sm text-muted-foreground">
            Paste this into the generator. It asks for exactly the {suggestion.addresses.length}{" "}
            failing check{suggestion.addresses.length === 1 ? "" : "s"} that a Dockerfile and
            Kubernetes manifests can satisfy, so the run has something to change.
          </p>
          <div className="mt-3">
            <CommandBlock
              command={suggestion.prompt}
              caption="Suggested generation prompt"
              testId="suggested-prompt"
            />
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            It will address: <span className="font-mono">{suggestion.addresses.join(", ")}</span>.
          </p>
          {suggestion.outOfScope.length > 0 ? (
            <p className="mt-1 text-xs text-muted-foreground" data-testid="generation-out-of-scope">
              Not covered, because generation does not emit them: {suggestion.outOfScope.join(", ")}
              .
            </p>
          ) : null}
          <p className="mt-2 text-xs text-muted-foreground">
            Leave the environment field empty. Omitted, policy requires a human to approve, so you
            see the diff before anything is written to disk.
          </p>
        </>
      )}
    </div>
  );
}
