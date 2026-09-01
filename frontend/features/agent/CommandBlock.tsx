"use client";

/**
 * A command the user can paste without editing it.
 *
 * THE DEFECT THIS REPLACES. The Pairing screen printed:
 *
 *     Run: forgeops-agent pair --code BYDPQC
 *
 * which fails on Windows twice over — PowerShell does not search the current directory, and the
 * binary is `forgeops-agent.exe` — and fails everywhere for want of `--backend`, without which
 * `pair` refuses. A user had to know all three corrections before anything worked.
 *
 * Every string here comes from `renderCommand`, which throws on a flag the CLI does not have, and
 * `scripts/check-rendered-commands.py` checks that flag list against the Go source.
 */

import { useCallback, useState } from "react";

/** A copy button that reports what it did, because a silent copy is indistinguishable from a dead one. */
function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);

  const copy = useCallback(() => {
    // `navigator.clipboard` is unavailable on an insecure origin and can be denied by permission.
    // Both are reported rather than swallowed: a user who thinks they copied a pairing code and did
    // not will paste the previous clipboard contents into a terminal and get a confusing error.
    void navigator.clipboard
      ?.writeText(value)
      .then(() => {
        setCopied(true);
        setFailed(false);
        window.setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => {
        setFailed(true);
        setCopied(false);
      });
  }, [value]);

  return (
    <button
      type="button"
      onClick={copy}
      data-testid={`copy-${label}`}
      aria-label={`Copy ${label} to the clipboard`}
      className="shrink-0 rounded border border-border px-2 py-1 text-xs font-medium hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {/* `aria-live` so a screen reader hears the outcome; the visual label changes for everyone else. */}
      <span aria-live="polite">{copied ? "Copied" : failed ? "Copy failed" : "Copy"}</span>
    </button>
  );
}

export interface CommandBlockProps {
  /** The exact text to run. Built by `renderCommand`, never assembled at the call site. */
  command: string;
  /** What this command achieves, shown above it. */
  caption?: string;
  /** Identifies the block for tests and for the copy button's label. */
  testId: string;
}

/**
 * One command, with a copy button and no editing required.
 *
 * `whitespace-pre-wrap` and `break-all`: a Windows path with a space in it must stay on one logical
 * line when copied, and a long `--backend` URL must not overflow the panel. Wrapping visually while
 * copying exactly is the behaviour a user needs from something they are about to paste into a shell.
 */
export function CommandBlock({ command, caption, testId }: CommandBlockProps) {
  return (
    <div className="space-y-1">
      {caption ? <p className="text-xs text-muted-foreground">{caption}</p> : null}
      <div className="flex items-start gap-2 rounded-md border border-border bg-muted/40 p-2">
        <code
          data-testid={testId}
          className="min-w-0 flex-1 whitespace-pre-wrap break-all font-mono text-xs"
        >
          {command}
        </code>
        <CopyButton value={command} label={testId} />
      </div>
    </div>
  );
}
