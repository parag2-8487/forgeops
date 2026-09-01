"use client";

/**
 * A live countdown on a pairing code, and a way to mint another when it runs out.
 *
 * THE DEFECT. Codes last five minutes. The screen showed `expires_at` as a raw ISO timestamp, so a
 * user had to subtract two times in their head to know how long they had — and on a first run, when
 * the old flow required building the agent from source, the first code ALWAYS expired before it
 * could be used. The error `the pairing code is not valid: issue a new code and try again` is
 * correct and useless: it arrives after the work, and the screen offered no way to issue one without
 * starting over.
 *
 * So: seconds remaining, updated every second, and a re-mint button in place the moment it expires.
 */

import { useEffect, useState } from "react";

/** Below this, the code is nearly gone and the display says so more loudly. */
const URGENT_SECONDS = 60;

export interface CodeCountdownProps {
  /** RFC 3339 expiry from the mint response. */
  expiresAt: string;
  /** Re-mint. Rendered as a button the moment the code expires. */
  onRemint: () => void;
  /** True while a re-mint is in flight. */
  isReminting: boolean;
  /**
   * Injectable clock. Tests need to observe expiry without waiting five real minutes, and a
   * component that reads `Date.now()` directly cannot be tested at all without faking timers
   * globally.
   */
  now?: () => number;
}

/** Whole seconds until `expiresAt`, floored at zero. */
function secondsRemaining(expiresAt: string, now: number): number {
  const expiry = Date.parse(expiresAt);
  if (Number.isNaN(expiry)) return 0;
  return Math.max(0, Math.floor((expiry - now) / 1000));
}

/** `4:07`, or `0:09`. */
function formatRemaining(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

export function CodeCountdown({
  expiresAt,
  onRemint,
  isReminting,
  now = Date.now,
}: CodeCountdownProps) {
  // A bare tick, and the remaining time is DERIVED at render.
  //
  // Storing the seconds and decrementing them drifts, and stops entirely when the tab is
  // backgrounded and the interval is throttled — so it would show time left on a code that had
  // already expired, which is worse than showing no countdown at all. Deriving it from the expiry on
  // every render makes that impossible, and means a change of `expiresAt` after a re-mint is correct
  // immediately rather than a second later.
  const [, tick] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const remaining = secondsRemaining(expiresAt, now());

  const expired = remaining === 0;
  const urgent = !expired && remaining <= URGENT_SECONDS;

  if (expired) {
    return (
      <div
        data-testid="code-expired"
        role="status"
        aria-live="polite"
        className="space-y-2 rounded-md border border-destructive/40 bg-destructive/5 p-2"
      >
        <p className="text-xs font-semibold">
          This code has expired. Codes last five minutes and cannot be extended.
        </p>
        <button
          type="button"
          onClick={onRemint}
          disabled={isReminting}
          data-testid="remint-code"
          className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {isReminting ? "Minting…" : "Mint a new code"}
        </button>
      </div>
    );
  }

  return (
    <p
      data-testid="code-countdown"
      // `role="timer"` is the right role and is deliberately NOT `aria-live="assertive"`: announcing
      // every second would make the screen unusable with a screen reader. The urgent threshold gets
      // one polite announcement instead.
      role="timer"
      aria-live={urgent ? "polite" : "off"}
      className={`text-xs ${urgent ? "font-semibold text-destructive" : "text-muted-foreground"}`}
    >
      Expires in <span data-testid="code-countdown-value">{formatRemaining(remaining)}</span>
      {urgent ? " — mint a new one if you need longer" : ""}
    </p>
  );
}
