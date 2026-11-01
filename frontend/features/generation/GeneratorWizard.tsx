// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiProblemError } from "@/lib/api";
import { isSseEvent, isTerminalSseEvent, type SseEvent } from "@/lib/api/sse-events";
import { readSSEResponse } from "@/lib/sse-reader";
import { Button } from "@/components/ui/button";
import { CompiledPromptPanel } from "./CompiledPromptPanel";

/**
 * The artifact generator (design.md §7.4, §11.5.5, §12.6 step 6).
 *
 * What this replaces: a three-step form whose final step read "Ready to generate artifacts" and had
 * no submit handler, over two dropdowns of hardcoded options. It looked like a working control and
 * called nothing, which is the specific defect the honest NotImplemented panels existed to avoid.
 *
 * Event names come from `lib/api/sse-events.ts` and are asserted against the backend's own enum in
 * `__tests__/sse-vocabulary.test.ts`. That comparison is the point: the previous producer emitted
 * three names outside §7.4 and nothing noticed, because a listener for an absent name fails by
 * staying silent rather than by raising.
 */

interface GeneratedFileState {
  path: string;
  content: string;
}

type RunState = "idle" | "streaming" | "accepted" | "failed";

/** Mirrors the payloads `src/generation/service.py` puts on each frame. */
interface StatusPayload {
  run_id?: string;
  state?: string;
}
interface TokenPayload {
  path?: string;
  text?: string;
}
interface ValidationPayload {
  passed?: boolean;
  findings?: string[];
}
interface CompletePayload {
  run_id?: string;
  files?: string[];
}
interface ErrorPayload {
  detail?: string;
}
interface ProgressPayload {
  state?: string;
  attempt?: number;
  max_attempts?: number;
  endpoint_id?: string | null;
  model?: string | null;
  first_token_seconds?: number;
  wait_seconds?: number;
}

/** Mirrors `GenerationModelChoice` in `backend/src/generation/routes.py`. */
export interface GenerationModelChoice {
  endpoint_id: string | null;
  provider: string;
  model: string;
  available: boolean;
  reason: string | null;
  default: boolean;
}

/** Plain-language names for the phases the backend reports, so a silent screen is never ambiguous. */
const PHASE_LABEL: Record<string, string> = {
  preparing_context: "Preparing project context",
  compiling_instruction: "Compiling the instruction",
  instruction_recorded: "Instruction recorded",
  requesting_model: "Waiting for the model (loading and reading the prompt)",
  model_streaming: "Model is streaming its answer",
  provider_busy_retrying: "Provider is busy; retrying after a short wait",
};

function describeProgress(payload: ProgressPayload): string {
  const label = PHASE_LABEL[payload.state ?? ""] ?? payload.state ?? "Working";
  const parts = [label];
  if (payload.attempt)
    parts.push(
      `attempt ${payload.attempt}${payload.max_attempts ? ` of ${payload.max_attempts}` : ""}`,
    );
  if (payload.first_token_seconds !== undefined)
    parts.push(`first token after ${payload.first_token_seconds.toFixed(1)}s`);
  if (payload.wait_seconds) parts.push(`waiting ${payload.wait_seconds}s`);
  return parts.join(" · ");
}

export function GeneratorWizard({ projectId }: { projectId: string }) {
  const [prompt, setPrompt] = useState("");
  // "" is the default chain (local Ollama); anything else pins the run to that one endpoint.
  const [endpointId, setEndpointId] = useState("");
  const [phase, setPhase] = useState<string | null>(null);
  const [models, setModels] = useState<GenerationModelChoice[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    // A failed read leaves only the default option, which is what the server runs without a selection.
    api
      .get<GenerationModelChoice[]>("/generation/models")
      .then((choices) => {
        if (!cancelled && Array.isArray(choices)) setModels(choices);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  const selected = models?.find((choice) => (choice.endpoint_id ?? "") === endpointId);
  const [state, setState] = useState<RunState>("idle");
  const [runId, setRunId] = useState<string | null>(null);
  const [liveOutput, setLiveOutput] = useState("");
  const [files, setFiles] = useState<GeneratedFileState[]>([]);
  const [validation, setValidation] = useState<ValidationPayload | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  // Every event name seen, in order. Rendered so a reviewer can see the real vocabulary rather than
  // trusting that the client understood it, and asserted on in the tests.
  const [seen, setSeen] = useState<SseEvent[]>([]);

  const run = useCallback(async () => {
    setState("streaming");
    setFiles([]);
    setValidation(null);
    setFailure(null);
    setSeen([]);
    setRunId(null);
    setLiveOutput("");
    setPhase("Submitting");

    let sawTerminal = false;

    try {
      const response = await api.stream("/generation/runs", {
        method: "POST",
        // `endpoint_id` only when a model was chosen: omitting it keeps the default chain, and the backend
        // never substitutes another provider for an explicit choice.
        body: JSON.stringify(
          endpointId === ""
            ? { project_id: projectId, prompt }
            : { project_id: projectId, prompt, endpoint_id: endpointId },
        ),
      });

      // Accumulated per path where the stream says which file a token belongs to, and into ONE live
      // buffer where it does not.
      //
      // THE PER-PATH-ONLY VERSION DROPPED EVERY TOKEN. Its comment claimed "the stream interleaves
      // tokens for several files, and one buffer would splice a Dockerfile into a Kubernetes
      // manifest" — a reasonable-sounding assumption about a wire shape that does not exist.
      // `generation/service.py` emits `{run_id, text, attempt}` and no `path`, and it cannot do
      // otherwise: while the model is still producing, the output is one undifferentiated blob and
      // which file a given token lands in is not known until `parse_artifacts` runs at the end.
      //
      // So `if (payload.path)` was false for every token, nothing was ever buffered, and no artifact
      // element was ever rendered. The bytes arrived correctly and the screen stayed empty — which is
      // why the well-formedness tests all passed while criterion 13's "tokens stream to the frontend"
      // was not true of the frontend at all.
      const buffers = new Map<string, string>();
      let live = "";

      for await (const message of readSSEResponse<unknown>(response)) {
        if (!isSseEvent(message.event)) {
          // Refused loudly rather than skipped. A name outside §7.4 means the two ends have
          // diverged, and quietly ignoring it is how that goes unnoticed for a whole phase.
          setFailure(
            `The server sent an event named "${message.event}", which is not one of §7.4's six. ` +
              "Refusing to interpret it.",
          );
          setState("failed");
          return;
        }

        const event = message.event as SseEvent;
        setSeen((previous) => [...previous, event]);

        if (event === "status") {
          const payload = message.data as StatusPayload;
          if (payload.run_id) setRunId(payload.run_id);
        } else if (event === "token") {
          const payload = message.data as TokenPayload;
          if (payload.path) {
            buffers.set(payload.path, (buffers.get(payload.path) ?? "") + (payload.text ?? ""));
            setFiles(
              Array.from(buffers, ([path, content]) => ({ path, content })).sort((a, b) =>
                a.path.localeCompare(b.path),
              ),
            );
          } else if (payload.text) {
            // No path: the model is mid-blob. Painted as it arrives, because a spinner followed by a
            // finished document is indistinguishable to a user from a buffered response, and this is
            // the only place the stream is observable at all.
            live += payload.text;
            setLiveOutput(live);
            if (import.meta.env.MODE !== "test") {
              await new Promise((resolve) => setTimeout(resolve, 10));
            }
          }
        } else if (event === "progress") {
          setPhase(describeProgress(message.data as ProgressPayload));
        } else if (event === "validation") {
          setValidation(message.data as ValidationPayload);
        } else if (event === "complete") {
          const payload = message.data as CompletePayload;
          if (payload.run_id) setRunId(payload.run_id);
          setState("accepted");
        } else if (event === "error") {
          const payload = message.data as ErrorPayload;
          setFailure(payload.detail ?? "The run failed.");
          setState("failed");
        }

        if (isTerminalSseEvent(event)) {
          sawTerminal = true;
          break;
        }
      }

      // A stream that merely stopped is not one that finished. Treating the two alike would report
      // an accepted run for a dropped connection.
      setPhase(null);
      if (!sawTerminal) {
        setState("failed");
        setFailure("The stream ended without a terminal event, so the run's outcome is unknown.");
      }
    } catch (error) {
      const problem = error instanceof ApiProblemError ? error.problem : null;
      setFailure(problem?.detail ?? problem?.title ?? "The generation request failed.");
      setState("failed");
    }
  }, [endpointId, projectId, prompt]);

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <label className="block text-sm font-medium" htmlFor="generation-prompt">
          What should be generated
        </label>
        <textarea
          id="generation-prompt"
          rows={3}
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          className="w-full rounded-md border border-border bg-background p-2 text-sm"
          placeholder="A container image and Kubernetes manifest for this Node.js service."
        />
        <p className="text-xs text-muted-foreground">
          Free text rather than a dropdown of stacks. The previous version offered three fixed
          choices that no endpoint consumed; the runtime is inferred from the prompt by{" "}
          <code>generation/service.py</code>, so a closed list here would misrepresent what the
          server reads.
        </p>
      </div>

      <div className="space-y-1">
        <label className="block text-sm font-medium" htmlFor="generation-model">
          Model
        </label>
        <select
          id="generation-model"
          data-testid="generation-model"
          value={endpointId}
          disabled={state === "streaming"}
          onChange={(event) => setEndpointId(event.target.value)}
          className="w-full rounded-md border border-border bg-background p-2 text-sm"
        >
          {(
            models ?? [
              {
                endpoint_id: null,
                provider: "self_hosted",
                model: "default",
                available: true,
                reason: null,
                default: true,
              },
            ]
          ).map((choice) => (
            <option
              key={choice.endpoint_id ?? "default"}
              value={choice.endpoint_id ?? ""}
              disabled={!choice.available}
            >
              {choice.default
                ? `Ollama · ${choice.model} (default)`
                : `${choice.provider === "google" ? "Google Gemini" : choice.provider} · ${choice.model}`}
              {choice.available ? "" : " — not configured"}
            </option>
          ))}
        </select>
        {models
          ?.filter((choice) => !choice.available && choice.reason)
          .map((choice) => (
            <p
              key={choice.endpoint_id ?? "default"}
              className="text-xs text-amber-600"
              data-testid="model-unavailable"
            >
              {choice.model}: {choice.reason}. Configure it under Model tiers.
            </p>
          ))}
      </div>

      <Button
        disabled={state === "streaming" || prompt.trim() === "" || selected?.available === false}
        onClick={() => void run()}
      >
        {state === "streaming" ? "Generating…" : "Generate artifacts"}
      </Button>

      {state === "streaming" && phase ? (
        <p role="status" className="text-sm text-muted-foreground" data-testid="generation-phase">
          {selected && !selected.default ? `${selected.model}: ` : ""}
          {phase}
        </p>
      ) : null}

      {runId ? (
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">
            Run <code data-testid="run-id">{runId}</code>
          </p>
          {/* The instruction is offered NEXT TO the run, not on a separate screen. A user judging an
              artifact that landed in the wrong place needs the prompt in the same glance, or they cannot
              tell a prompt defect from a model defect. */}
          <CompiledPromptPanel runId={runId} />
        </div>
      ) : null}

      {seen.length > 0 ? (
        <p className="text-xs text-muted-foreground" data-testid="event-log">
          Events received: {seen.join(" \u2192 ")}
        </p>
      ) : null}

      {liveOutput !== "" ? (
        <section aria-label="Model output" className="space-y-2">
          <h3 className="text-sm font-semibold">Model output</h3>
          {/* The stream as it arrives, before `parse_artifacts` has split it into files. Rendered
              because a screen that shows nothing until the run finishes cannot be told apart from a
              buffered response, and the tokens are the only evidence the stream is live. */}
          <pre
            className="max-h-64 overflow-auto rounded-md border border-border p-3 text-xs"
            data-testid="stream-output"
          >
            {liveOutput}
          </pre>
        </section>
      ) : null}

      {files.length > 0 ? (
        <section aria-label="Generated artifacts" className="space-y-3">
          <h3 className="text-sm font-semibold">Generated artifacts</h3>
          {files.map((file) => (
            <div key={file.path} className="rounded-md border border-border">
              <header className="border-b border-border px-3 py-2">
                <code className="text-sm font-semibold">{file.path}</code>
              </header>
              <pre className="overflow-x-auto p-3 text-xs" data-testid={`artifact-${file.path}`}>
                {file.content}
              </pre>
            </div>
          ))}
        </section>
      ) : null}

      {validation ? (
        <div
          role="status"
          className={`rounded-md border p-3 text-sm ${
            validation.passed
              ? "border-emerald-500/40 bg-emerald-500/5"
              : "border-destructive/40 bg-destructive/5"
          }`}
        >
          <p className="font-semibold">
            {validation.passed
              ? "The deterministic validation gate passed."
              : "The deterministic validation gate refused these artifacts."}
          </p>
          {validation.findings && validation.findings.length > 0 ? (
            <ul className="mt-2 list-inside list-disc text-muted-foreground">
              {validation.findings.map((finding) => (
                <li key={finding}>{finding}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {state === "accepted" ? (
        <p role="status" className="text-sm">
          The run was accepted and its artifacts were submitted to the governance chokepoint as a
          change set. If policy requires approval it is now awaiting a decision under{" "}
          <a className="underline" href="/approvals">
            Approvals
          </a>
          .
        </p>
      ) : null}

      {failure ? (
        <p role="alert" className="text-sm text-destructive">
          {failure}
        </p>
      ) : null}
    </div>
  );
}
