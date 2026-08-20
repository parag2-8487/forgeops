// SPDX-License-Identifier: FSL-1.1-ALv2
import React from "react";

/**
 * A visible, specific gap.
 *
 * The rule this enforces: a surface with no backend endpoint renders THIS, never invented data.
 * Three of the eight feature modules under `features/` shipped hardcoded content that looked
 * live — an audit log with two fabricated entries, a vault row for a secret that does not exist,
 * an agent panel asserting "Connected & Attested" with nothing attached. Each of those is worse
 * than an empty screen, because a demo cannot tell them from working software and neither can a
 * reviewer.
 *
 * `reason` must say what is actually missing, and `owner` which phase owns it, so the gap is
 * auditable rather than merely admitted.
 */
export function NotImplemented({
  feature,
  owner,
  reason,
  detail,
}: {
  feature: string;
  owner: string;
  reason: string;
  detail?: React.ReactNode;
}) {
  return (
    <section
      aria-labelledby="not-implemented-heading"
      className="rounded-lg border border-dashed border-border bg-muted/30 p-6"
    >
      <h2 id="not-implemented-heading" className="text-lg font-semibold">
        {feature} is not implemented in Phase 1
      </h2>
      <dl className="mt-4 space-y-3 text-sm">
        <div>
          <dt className="font-medium">Owned by</dt>
          <dd className="text-muted-foreground">{owner}</dd>
        </div>
        <div>
          <dt className="font-medium">Why this screen is empty</dt>
          <dd className="text-muted-foreground">{reason}</dd>
        </div>
      </dl>
      {detail ? <div className="mt-4 border-t border-border pt-4 text-sm">{detail}</div> : null}
      <p className="mt-4 text-xs text-muted-foreground">
        This panel is deliberately blank rather than populated with sample data. A visible gap can
        be planned around; a convincing fake cannot.
      </p>
    </section>
  );
}
