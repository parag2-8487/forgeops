// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { ApiProblemError, type ProblemDetails } from "@/lib/api";

/**
 * What a governance refusal MEANS, in plain language, beside what the server said.
 *
 * PHASES.MD §1.7: "Frontend: Policy violation display with explanation." This is that.
 *
 * The problem it solves is specific. When the chokepoint or OPA refuses something, the API answers
 * with an RFC 9457 document whose `type` is a stable, registered URN — `policy-denied`,
 * `blast-radius-blocked`, `policy-bundle-stale`, `approval-required` and so on — and whose `detail`
 * is written for whoever is reading a log. `AsyncState`'s panel renders those fields faithfully,
 * which is right for a failed READ, and quite wrong for a refused MUTATION: "Policy bundle stale
 * (409)" is accurate and tells an operator neither which rule fired nor what to do about it. The
 * user then concludes the product is broken, because from where they are sitting it is
 * indistinguishable from broken.
 *
 * So each registered type gets three things: a heading in the user's terms, an explanation of what
 * the rule is FOR, and the next action. The server's own `title` and `detail` are still shown
 * verbatim underneath — this augments the response, it never replaces it, because a paraphrase that
 * quietly diverged from what the server said would be worse than no paraphrase.
 *
 * THE MAP IS KEYED ON `type`, NOT ON `status`. RFC 9457 makes `type` the stable member and `title`
 * explicitly not; keying on the human-readable string would break the first time somebody improves
 * the wording, and keying on status would collapse `blast-radius-blocked`, `change-set-conflict`
 * and `policy-bundle-stale` — three different situations with three different next steps — into one
 * "409".
 *
 * An unregistered type falls through to the server's own words with no invented explanation. That
 * is deliberate: a generic reassurance attached to a refusal nobody anticipated would be a
 * plausible-sounding sentence with nothing behind it, which is the failure this whole pass exists
 * to remove.
 */

/** The last path segment of a `type` URN, e.g. `policy-denied`. */
function typeSlug(problem: ProblemDetails): string {
  const raw = problem.type ?? "";
  const cut = raw.lastIndexOf("/");
  return cut === -1 ? raw : raw.slice(cut + 1);
}

interface Explanation {
  /** The heading, in the user's terms rather than the registry's. */
  headline: string;
  /** What the rule is for. Why it exists, not what it did. */
  why: string;
  /** The next thing to do. Empty only where there genuinely is nothing. */
  next: string;
}

/**
 * Registered problem type → plain-language explanation.
 *
 * Every key here appears in `backend/src/core/errors.py::PROBLEM_REGISTRY`, and
 * `__tests__/governance-explanations.test.ts` asserts that in both directions for the governance
 * subset — an explanation for a type the backend cannot raise is dead text, and a governance
 * refusal with no explanation is the gap this component was built to close.
 */
export const GOVERNANCE_EXPLANATIONS: Readonly<Record<string, Explanation>> = {
  "policy-denied": {
    headline: "A governance policy refused this change",
    why:
      "Every mutation is evaluated against your tenant's published Rego bundle before anything is " +
      "written. The refusal is the policy working, not a fault: it is what stops a generated " +
      "artifact touching something it should not.",
    next:
      "Read the rule that fired below, then either change the request so it complies or amend the " +
      "policy on the Policies screen and publish a new bundle.",
  },
  "policy-bundle-stale": {
    headline: "The agent is pinned to an older policy bundle than the backend has",
    why:
      "A paired agent records the digest of the bundle it holds, and the backend refuses work from " +
      "an agent whose digest is not the current one. Otherwise the two halves of the double policy " +
      "evaluation would be judging against different rules, which would make agreement between " +
      "them meaningless.",
    next:
      "Publish the policy bundle from the Policies screen. The agent picks up the new digest on its " +
      "next connection; if it does not, it is not running.",
  },
  "governance-policy-undefined": {
    headline: "No policy bundle has been published for this tenant",
    why:
      "The chokepoint has nothing to evaluate against, and an absent policy is treated as a refusal " +
      "rather than as permission. That is deny-by-default at the layer that matters: a tenant that " +
      "has never published a bundle must not be more permissive than one that has.",
    next: "Publish the policy bundle. This is step 5 of the onboarding path, and nothing downstream works without it.",
  },
  "blast-radius-blocked": {
    headline: "This change affects too much to apply without review",
    why:
      "The Semantic Plan Analyzer scores how far a change reaches — how many resources it touches, " +
      "how many of those are destructive, and whether any of them hold state. A score past the " +
      "threshold is blocked rather than queued, because the cost of getting a wide change wrong is " +
      "not recoverable by retrying it.",
    next: "Narrow the change, or split it, and resubmit. The score and the resources behind it are below.",
  },
  "approval-required": {
    headline: "This is waiting for a human decision",
    why:
      "Not an error — the change set was compiled and admitted, and the gate is holding it for " +
      "review. A revert reaches this state by design: reversing an applied change is itself a " +
      "mutation and goes through all six stages with its own fresh authority, rather than being a " +
      "privileged back door.",
    next: "Open the Approvals screen and decide it. Nothing has been written yet.",
  },
  "approval-expired": {
    headline: "The approval is too old to act on",
    why:
      "An approval authorises a specific change set against the policy and the code as they were " +
      "when it was granted. Applying it later would apply a decision somebody made about a " +
      "different state of the world.",
    next: "Resubmit the change and have it approved again.",
  },
  "approval-forbidden": {
    headline: "You may not decide this change set",
    why:
      "Approval authority is resolved against the project and the tenant, not against being signed " +
      "in. §4.2 makes this response identical whether you lack the authority or the change set is " +
      "not yours to see, so it deliberately does not say which.",
    next: "Ask someone with authority on this project to decide it.",
  },
  "change-set-conflict": {
    headline: "Someone else changed this while you were looking at it",
    why:
      "Decisions carry the version you read, and the backend refuses one made against a stale " +
      "version. Without that check, two reviewers acting at once would silently overwrite each " +
      "other's decision.",
    next: "Reload the change set and decide again against its current state.",
  },
  "change-set-already-applied": {
    headline: "This change set has already been applied",
    why: "A change set moves through its state machine once. Applying twice is not idempotent — it would re-run writes.",
    next: "If you need to undo it, use Revert rather than applying again.",
  },
  "revert-unavailable": {
    headline: "There is nothing to revert to",
    why:
      "A revert is compiled from the recorded before-state of the original change. Without that " +
      "state there is no reverse change set to authorise, and inventing one would mean writing " +
      "files nobody has seen.",
    next: "Inspect the original change set's items to see what it recorded.",
  },
  "device-not-connected": {
    headline: "No agent is connected for this project",
    why:
      "An approved change is delivered to a paired agent as a signed command envelope. The backend " +
      "will not queue one for a device that is not there, because a command that sits waiting is " +
      "indistinguishable from one that was applied.",
    next: "Pair an agent for this project, or start the one you have, and try again. The Pairing screen shows what is known.",
  },
  "dryrun-unavailable": {
    headline: "The policy could not be evaluated",
    why:
      "A dry-run result is only meaningful if OPA produced it. This surface used to synthesise an " +
      "allow or deny when the evaluator was missing, which is the most misleading thing a security " +
      "screen can do, so it now refuses instead.",
    next: "This is a deployment fault rather than something you can fix from here. The detail below names what is missing.",
  },
  "generation-unavailable": {
    headline: "No model could serve this generation",
    why:
      "The router tried the tier's primary endpoint, its cross-vendor backup and the self-hosted " +
      "fallback, and every one was unavailable or had its circuit breaker open. The safe template " +
      "library is the last resort and did not apply here.",
    next: "The Model tiers screen shows which endpoints are open, closed or half-open right now.",
  },
  "scan-in-progress": {
    headline: "A scan is already running for this project",
    why:
      "Two concurrent scans would write the same file rows from two different views of the tree, " +
      "and the index would end up describing neither.",
    next: "Wait for the running scan to finish. The project's index status shows when it last completed.",
  },
  forbidden: {
    headline: "Refused",
    why:
      "This response is byte-identical whether you lack permission or the resource does not exist. " +
      "That is deliberate — a distinguishable answer would let anyone map which ids exist by " +
      "reading the difference.",
    next: "If you expected access, the id or the project scope is the thing to check.",
  },
};

/** Look up an explanation for an error, or `null` when there is nothing honest to add. */
export function explanationFor(error: unknown): Explanation | null {
  if (!(error instanceof ApiProblemError)) return null;
  return GOVERNANCE_EXPLANATIONS[typeSlug(error.problem)] ?? null;
}

/**
 * Render a refused mutation: what it means, why the rule exists, what to do, and what the server said.
 *
 * `role="alert"` so a refusal is announced rather than silently appearing below the fold — the
 * accessibility requirement that errors be announced is exactly about this case.
 */
export function GovernanceRefusal({
  error,
  action,
}: {
  error: unknown;
  /** What the user was trying to do, e.g. "approve this change set". Used in the fallback heading. */
  action: string;
}) {
  if (error === null || error === undefined) return null;

  const problem = error instanceof ApiProblemError ? error.problem : null;
  const explanation = explanationFor(error);

  return (
    <div
      role="alert"
      data-testid="governance-refusal"
      className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-4 text-sm"
    >
      <p className="font-semibold">{explanation ? explanation.headline : `Could not ${action}.`}</p>

      {explanation ? (
        <>
          <p className="mt-2 text-muted-foreground">{explanation.why}</p>
          <p className="mt-2">
            <span className="font-medium">What to do: </span>
            <span className="text-muted-foreground">{explanation.next}</span>
          </p>
        </>
      ) : (
        <p className="mt-2 text-muted-foreground">
          This refusal has no registered explanation, so what the server said is shown verbatim
          rather than paraphrased into a guess.
        </p>
      )}

      {problem ? (
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-t border-border pt-3 text-xs text-muted-foreground">
          <dt className="font-medium">Rule</dt>
          {/* The stable `type` member, which is what identifies the rule that fired. */}
          <dd>
            <code data-testid="refusal-type">{typeSlug(problem)}</code>
          </dd>
          <dt className="font-medium">Status</dt>
          <dd>{problem.status}</dd>
          <dt className="font-medium">Reported as</dt>
          <dd>{problem.title}</dd>
          {problem.detail ? (
            <>
              <dt className="font-medium">Detail</dt>
              <dd data-testid="refusal-detail">{problem.detail}</dd>
            </>
          ) : null}
          {problem.errors?.length ? (
            <>
              <dt className="font-medium">Fields</dt>
              <dd>
                <ul className="space-y-0.5">
                  {problem.errors.map((e) => (
                    <li key={`${e.pointer}:${e.detail}`}>
                      <code>{e.pointer}</code> — {e.detail}
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          ) : null}
          {problem.trace_id ? (
            <>
              <dt className="font-medium">Trace</dt>
              {/* Quotable in a bug report, which is the only reason it is on screen. */}
              <dd>
                <code>{problem.trace_id}</code>
              </dd>
            </>
          ) : null}
        </dl>
      ) : null}
    </div>
  );
}
