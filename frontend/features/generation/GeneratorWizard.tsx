// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useCallback, useState } from "react";
import { api, ApiProblemError } from "@/lib/api";
import { isSseEvent, isTerminalSseEvent, type SseEvent } from "@/lib/api/sse-events";
import { readSSEResponse } from "@/lib/sse-reader";
import { Button } from "@/components/ui/button";

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

export function GeneratorWizard({ projectId }: { projectId: string }) {
  const [prompt, setPrompt] = useState("");
  const [state, setState] = useState<RunState>("idle");
  const [runId, setRunId] = useState<string | null>(null);
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

    let sawTerminal = false;

    try {
      const response = await api.stream("/generation/runs", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, prompt }),
      });

      // Accumulated per path rather than into one buffer: the stream interleaves tokens for several
      // files, and one buffer would splice a Dockerfile into a Kubernetes manifest.
      const buffers = new Map<string, string>();

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
          }
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
      if (!sawTerminal) {
        setState("failed");
        setFailure("The stream ended without a terminal event, so the run's outcome is unknown.");
      }
    } catch (error) {
      const problem = error instanceof ApiProblemError ? error.problem : null;
      setFailure(problem?.detail ?? problem?.title ?? "The generation request failed.");
      setState("failed");
    }
  }, [projectId, prompt]);

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

      <Button disabled={state === "streaming" || prompt.trim() === ""} onClick={() => void run()}>
        {state === "streaming" ? "Generating…" : "Generate artifacts"}
      </Button>

      {runId ? (
        <p className="text-sm text-muted-foreground">
          Run <code data-testid="run-id">{runId}</code>
        </p>
      ) : null}

      {seen.length > 0 ? (
        <p className="text-xs text-muted-foreground" data-testid="event-log">
          Events received: {seen.join(" \u2192 ")}
        </p>
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
