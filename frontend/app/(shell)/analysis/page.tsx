// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, ApiProblemError } from "@/lib/api";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";

/** Mirrors `FindingResponse` in `backend/src/analysis/routes.py`. */
interface Finding {
  stage: string;
  severity: string;
  code: string;
  message: string;
  resource: string | null;
}

/** Mirrors `BlastRadiusResponse`. */
interface BlastRadius {
  score: number;
  destructive_count: number;
  affected_resources: number;
  stateful_deletions: string[];
  verdict: string;
}

/** Mirrors `PlanAnalysisResponse`. */
interface PlanAnalysis {
  findings: Finding[];
  blast_radius: BlastRadius | null;
  verdict: string;
  approval_decision: string | null;
}

/**
 * What each verdict means. The vocabulary comes from the analyzer, not from this file.
 *
 * `fatal` and `block` are genuinely different: fatal means a stage refused to analyse the document at
 * all (it did not parse, or its schema is wrong), block means it analysed fine and the blast radius is
 * too wide. Rendering both as red would tell an operator to narrow a plan the analyzer never read.
 */
const VERDICT_MEANING: Record<string, string> = {
  allow: "Nothing in this plan exceeds the thresholds. It would pass the blast-radius stage.",
  require_approval:
    "Within limits, but wide enough that the approval gate would hold it for a human decision rather than auto-approving.",
  block:
    "Too wide to apply. The chokepoint would refuse this rather than queue it, because the cost of getting a change this size wrong is not recoverable by retrying.",
  fatal:
    "A stage refused the document itself — it did not parse, or its schema is not a plan. Nothing was analysed, so there is no blast radius to report.",
};

const EXAMPLE_PLAN = `{
  "format_version": "1.2",
  "resource_changes": [
    {
      "address": "aws_s3_bucket.assets",
      "type": "aws_s3_bucket",
      "change": { "actions": ["create"] }
    }
  ]
}
`;

/**
 * Analyse an OpenTofu/Terraform plan — `POST /api/v1/analysis/plan`, which had no screen.
 *
 * This was Phase 0 §0.7 and Phase 1 §1.5 work, and it is the same Semantic Plan Analyzer the
 * governance chokepoint runs as its blast-radius stage. That is the value of a standalone screen: it
 * lets an operator ask "what would the chokepoint say about this?" BEFORE submitting a change, rather
 * than discovering it as a `blast-radius-blocked` refusal at the end of a generation run.
 *
 * It analyses only. Nothing is stored, no change set is created, and no policy is evaluated — so a
 * verdict here is a prediction about the blast-radius stage, not an authorisation. The panel says that,
 * because a screen that returns "allow" and is mistaken for a green light to apply would be worse than
 * no screen.
 */
export default function PlanAnalysisPage() {
  const [planText, setPlanText] = useState(EXAMPLE_PLAN);

  const analyse = useMutation({
    mutationFn: () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(planText);
      } catch (cause) {
        throw new Error(
          `That is not valid JSON: ${cause instanceof Error ? cause.message : "unparseable"}`,
        );
      }
      return api.post<PlanAnalysis>("/analysis/plan", { plan: parsed });
    },
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Plan analysis</h1>
        <p className="mt-1 text-muted-foreground">
          Runs the same Semantic Plan Analyzer the governance chokepoint uses as its blast-radius
          stage, against a plan you paste. Read from <code>POST /api/v1/analysis/plan</code>.
        </p>
      </div>

      <form
        className="space-y-3 rounded-lg border border-border bg-background p-4"
        onSubmit={(event) => {
          event.preventDefault();
          analyse.mutate();
        }}
      >
        <div>
          <label htmlFor="plan-json" className="block text-sm font-medium">
            Plan JSON
          </label>
          <textarea
            id="plan-json"
            value={planText}
            onChange={(event) => setPlanText(event.target.value)}
            rows={14}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            data-testid="plan-json"
            className="mt-1 w-full rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            The output of <code>tofu show -json tfplan</code>. The starting content above is a
            minimal example so the shape is obvious — replace it with your own plan.
          </p>
        </div>

        <button
          type="submit"
          disabled={analyse.isPending}
          data-testid="analyse-plan"
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {analyse.isPending ? "Analysing…" : "Analyse"}
        </button>

        {analyse.error && !(analyse.error instanceof ApiProblemError) ? (
          <p role="alert" data-testid="plan-local-error" className="text-sm text-destructive">
            {analyse.error instanceof Error ? analyse.error.message : "The analysis could not run."}
          </p>
        ) : null}

        {analyse.error instanceof ApiProblemError ? (
          <GovernanceRefusal error={analyse.error} action="analyse this plan" />
        ) : null}
      </form>

      {analyse.data ? (
        <div className="space-y-4" data-testid="plan-result">
          <div className="rounded-lg border border-border bg-background p-4">
            <h2 className="text-sm font-semibold">
              Verdict: <span data-testid="plan-verdict">{analyse.data.verdict}</span>
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {VERDICT_MEANING[analyse.data.verdict] ??
                "This verdict has no description here, so only the analyzer's own value is shown rather than a guess at its meaning."}
            </p>
            {analyse.data.approval_decision ? (
              <p className="mt-2 text-xs text-muted-foreground">
                The approval gate&apos;s own decision for this plan would be{" "}
                <code>{analyse.data.approval_decision}</code>.
              </p>
            ) : null}
          </div>

          {analyse.data.blast_radius ? (
            <div className="rounded-lg border border-border bg-background p-4">
              <h2 className="text-sm font-semibold">Blast radius</h2>
              <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-4">
                <div>
                  <dt className="font-medium">Score</dt>
                  <dd data-testid="blast-score">{analyse.data.blast_radius.score}</dd>
                </div>
                <div>
                  <dt className="font-medium">Resources affected</dt>
                  <dd>{analyse.data.blast_radius.affected_resources}</dd>
                </div>
                <div>
                  <dt className="font-medium">Destructive actions</dt>
                  <dd>{analyse.data.blast_radius.destructive_count}</dd>
                </div>
                <div>
                  <dt className="font-medium">Stage verdict</dt>
                  <dd>{analyse.data.blast_radius.verdict}</dd>
                </div>
              </dl>

              {analyse.data.blast_radius.stateful_deletions.length > 0 ? (
                <div className="mt-3">
                  <p className="text-xs font-medium">Stateful deletions</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    These are what make a score high rather than merely large. Deleting a stateless
                    resource is recoverable by re-creating it; deleting one that holds state is not,
                    so the analyzer weights them separately and names them.
                  </p>
                  <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs">
                    {analyse.data.blast_radius.stateful_deletions.map((resource) => (
                      <li key={resource}>
                        <code>{resource}</code>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="rounded-lg border border-border bg-background p-4 text-sm text-muted-foreground">
              No blast radius was computed. That happens when an earlier stage refused the document,
              so there was nothing to score — the findings below say which stage and why.
            </div>
          )}

          <div className="rounded-lg border border-border bg-background p-4">
            <h2 className="text-sm font-semibold">Findings</h2>
            {analyse.data.findings.length === 0 ? (
              <p className="mt-2 text-sm text-muted-foreground">
                None. Every stage accepted the plan without comment.
              </p>
            ) : (
              <ul className="mt-2 space-y-2" data-testid="plan-findings">
                {analyse.data.findings.map((finding) => (
                  <li
                    key={`${finding.stage}:${finding.code}:${finding.resource ?? ""}`}
                    className="text-sm"
                  >
                    <p className="font-medium">
                      <span className="uppercase text-xs">{finding.severity}</span>{" "}
                      <code className="text-xs">{finding.code}</code>
                      <span className="ml-2 text-xs font-normal text-muted-foreground">
                        from the {finding.stage} stage
                      </span>
                    </p>
                    <p className="text-muted-foreground">{finding.message}</p>
                    {finding.resource ? (
                      <p className="font-mono text-xs text-muted-foreground">{finding.resource}</p>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      ) : null}

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">This analyses. It does not authorise.</p>
        <p className="mt-2">
          Nothing here is stored, no change set is created, and no policy is evaluated — the OPA
          stage and the approval gate are separate from this. So an <code>allow</code> verdict is a
          prediction about how the chokepoint&apos;s blast-radius stage would score this plan, not
          permission to apply it. The value is in getting that prediction <em>before</em> submitting
          a change rather than as a refusal at the end of a generation run.
        </p>
      </aside>
    </div>
  );
}
