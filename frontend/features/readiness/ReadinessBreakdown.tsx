// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import {
  categoryLabel,
  type ReadinessCheck,
  type ReadinessReport,
} from "@/features/projects/types";

/**
 * The six categories, each expanding into the checks behind its score — phases.md §1.4, PRD FR-19.
 *
 * The radar chart shows six numbers. A number with nothing behind it cannot be acted on: "Security
 * 40" tells an operator they have a problem and not what it is, and worse, it is unfalsifiable — a
 * category at 40 with no visible evidence is indistinguishable from a bug in the scorer.
 *
 * So each category expands into its individual checks, and each check carries three things the backend
 * already computes and nothing rendered: whether it passed, the INDEXED PATH that satisfied it
 * (`evidence`), and PRD FR-19's "why it matters". The evidence is what makes the score auditable, and
 * the why is what makes it teach rather than merely grade.
 *
 * The grouping is derived from the checks themselves rather than from a list of category names held
 * here. A category the engine scores but for which no check exists would otherwise be invisible, and
 * a check in a category this file did not know about would be dropped — both silently.
 */
export function ReadinessBreakdown({ report }: { report: ReadinessReport }) {
  // Which categories are open. A Set rather than one open-at-a-time, because comparing two weak
  // categories is the normal reason to open them and an accordion that closes the first would make
  // that impossible.
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());

  const byCategory = new Map<string, ReadinessCheck[]>();
  // `?? []` rather than trusting the field to be present. It is non-optional in the response model,
  // but a render-time `for...of` over `undefined` throws inside React's commit phase, and there is no
  // error boundary between here and the page — so one older backend, or one field renamed on the
  // wire, would blank the whole readiness screen rather than degrade this panel. An empty breakdown
  // is a recoverable disappointment; a white page is not.
  for (const check of report.checks ?? []) {
    const existing = byCategory.get(check.category);
    if (existing) existing.push(check);
    else byCategory.set(check.category, [check]);
  }

  // Ordered by the engine's own category keys so the list matches the radar chart's order, with any
  // category that has checks but no score appended rather than dropped.
  const categories = [
    ...Object.keys(report.categories),
    ...[...byCategory.keys()].filter((c) => !(c in report.categories)),
  ];

  if (!report.indexed) {
    return (
      <div
        role="status"
        className="rounded-lg border border-border bg-background p-4 text-sm text-muted-foreground"
      >
        There is nothing to break down: this project has no indexed files, so no check has been
        evaluated. Every category reads zero because nothing was measured, not because everything
        failed. Scan the project and the breakdown appears here.
      </div>
    );
  }

  return (
    <div className="space-y-2" data-testid="readiness-breakdown">
      {categories.map((key) => {
        const checks = byCategory.get(key) ?? [];
        const score = report.categories[key];
        const isOpen = open.has(key);
        const passed = checks.filter((c) => c.passed).length;
        const panelId = `readiness-checks-${key}`;

        return (
          <div key={key} className="rounded-lg border border-border bg-background">
            <h3>
              <button
                type="button"
                // A real disclosure: `aria-expanded` on the trigger and `aria-controls` pointing at
                // the region it reveals, so the relationship is announced rather than only visible.
                aria-expanded={isOpen}
                aria-controls={panelId}
                data-testid={`category-toggle-${key}`}
                onClick={() =>
                  setOpen((current) => {
                    const next = new Set(current);
                    if (next.has(key)) next.delete(key);
                    else next.add(key);
                    return next;
                  })
                }
                className="flex w-full items-center justify-between gap-4 p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span className="flex items-baseline gap-3">
                  <span className="text-sm font-semibold">{categoryLabel(key)}</span>
                  <span className="text-xs text-muted-foreground">
                    {checks.length === 0
                      ? "no checks recorded"
                      : `${passed} of ${checks.length} check${checks.length === 1 ? "" : "s"} passing`}
                  </span>
                </span>
                <span className="flex items-center gap-3">
                  <span className="text-sm font-medium" data-testid={`category-score-${key}`}>
                    {score === undefined ? "—" : `${score}/100`}
                  </span>
                  <span aria-hidden="true" className="text-xs text-muted-foreground">
                    {isOpen ? "▲" : "▼"}
                  </span>
                </span>
              </button>
            </h3>

            {isOpen ? (
              <div id={panelId} className="border-t border-border p-4">
                {checks.length === 0 ? (
                  <p className="text-sm text-muted-foreground">
                    This category has a score but the report carried no individual checks for it, so
                    there is nothing to show. That is a gap in the report rather than a passing
                    category, and it is stated rather than filled in.
                  </p>
                ) : (
                  <ul className="space-y-3">
                    {checks.map((check) => (
                      <li key={check.id} className="text-sm" data-testid={`check-${check.id}`}>
                        <div className="flex flex-wrap items-baseline justify-between gap-2">
                          <p className="font-medium">
                            {/* The word, not only the colour — a red dot is invisible to a
                                colour-blind reader and to a screen reader alike. */}
                            <span className={check.passed ? "text-emerald-600" : "text-amber-600"}>
                              {check.passed ? "Pass" : "Fail"}
                            </span>{" "}
                            <span>{check.id}</span>
                          </p>
                          <span className="text-xs text-muted-foreground">
                            {check.points} of {check.max_points} points
                          </span>
                        </div>
                        <p className="mt-1 text-muted-foreground">
                          <span className="font-medium">Evidence: </span>
                          {check.evidence}
                        </p>
                        <p className="mt-0.5 text-muted-foreground">
                          <span className="font-medium">Why it matters: </span>
                          {check.why_it_matters}
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
