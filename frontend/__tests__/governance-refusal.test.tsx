// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * Policy violation display with explanation — phases.md §1.7's second frontend box.
 *
 * The behaviour under test is narrow and specific: when the chokepoint or OPA refuses something, the
 * user must see which rule fired and why, in plain language, WITHOUT the server's own words being
 * replaced by a paraphrase that could drift from them.
 *
 * So the assertions are:
 *  - the explanation is keyed on the stable `type`, not on `title` or `status`;
 *  - the rule that fired is named;
 *  - the server's `detail` survives verbatim;
 *  - an UNREGISTERED type gets no invented explanation.
 *
 * That last one is the important one. A generic reassurance attached to a refusal nobody anticipated
 * would be a plausible sentence with nothing behind it, which is the failure this whole pass removes.
 */

import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { ApiProblemError, ApiTransportError } from "@/lib/api";
import {
  GovernanceRefusal,
  GOVERNANCE_EXPLANATIONS,
  explanationFor,
} from "@/components/ui/governance-refusal";

afterEach(() => cleanup());

function refusal(
  type: string,
  overrides: Partial<{ title: string; status: number; detail: string }> = {},
) {
  return new ApiProblemError({
    type: `https://errors.forgeops.dev/${type}`,
    title: overrides.title ?? "Refused",
    status: overrides.status ?? 403,
    detail: overrides.detail,
  });
}

describe("the explanation map", () => {
  /**
   * Every key must be a type `backend/src/core/errors.py::PROBLEM_REGISTRY` can actually raise.
   *
   * Listed here rather than imported, because there is no import path from Python to TypeScript — so
   * this is a mirror, and the value of the test is that it is an EXACT one. A key not in this list is
   * either a typo (the explanation never renders) or a type the backend removed (dead text).
   */
  const REGISTERED_GOVERNANCE_TYPES = [
    "policy-denied",
    "policy-bundle-stale",
    "governance-policy-undefined",
    "blast-radius-blocked",
    "approval-required",
    "approval-expired",
    "approval-forbidden",
    "change-set-conflict",
    "change-set-already-applied",
    "revert-unavailable",
    "device-not-connected",
    "dryrun-unavailable",
    "generation-unavailable",
    "scan-in-progress",
    "forbidden",
  ];

  it("explains exactly the registered governance types", () => {
    expect(Object.keys(GOVERNANCE_EXPLANATIONS).sort()).toEqual(
      [...REGISTERED_GOVERNANCE_TYPES].sort(),
    );
  });

  it("gives every type a headline, a reason the rule exists, and a next action", () => {
    for (const [type, explanation] of Object.entries(GOVERNANCE_EXPLANATIONS)) {
      expect(explanation.headline.length, type).toBeGreaterThan(0);
      expect(explanation.why.length, type).toBeGreaterThan(0);
      expect(explanation.next.length, type).toBeGreaterThan(0);
    }
  });

  it("keys on the type's last path segment, so a full URN resolves", () => {
    expect(explanationFor(refusal("policy-denied"))?.headline).toMatch(
      /governance policy refused/i,
    );
  });

  it("returns null for a type it does not know, rather than a generic gloss", () => {
    expect(explanationFor(refusal("some-future-type"))).toBeNull();
  });

  it("returns null for anything that is not an api problem at all", () => {
    expect(explanationFor(new Error("boom"))).toBeNull();
    expect(explanationFor(null)).toBeNull();
  });
});

describe("GovernanceRefusal", () => {
  it("renders nothing when there is no error, so a form is not permanently decorated", () => {
    const { container } = render(<GovernanceRefusal error={null} action="do the thing" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("announces a refusal as an alert, because an error must be announced", () => {
    render(<GovernanceRefusal error={refusal("policy-denied")} action="approve" />);
    // Not a silent panel below the fold. The accessibility requirement that errors be announced is
    // exactly about a refusal appearing after a button press.
    expect(screen.getByTestId("governance-refusal")).toHaveAttribute("role", "alert");
  });

  it("names the rule that fired, in the stable form a client can key on", () => {
    render(
      <GovernanceRefusal
        error={refusal("blast-radius-blocked", { status: 409 })}
        action="approve"
      />,
    );
    expect(screen.getByTestId("refusal-type")).toHaveTextContent("blast-radius-blocked");
  });

  it("explains what the rule is for and what to do about it", () => {
    render(
      <GovernanceRefusal error={refusal("policy-bundle-stale", { status: 409 })} action="submit" />,
    );
    expect(screen.getByText(/pinned to an older policy bundle/i)).toBeInTheDocument();
    // The "why" — a refusal that reads as a fault is what makes people distrust the product.
    expect(
      screen.getByText(/double policy evaluation would be judging against different rules/i),
    ).toBeInTheDocument();
    // The "what to do", which is the part a generic error page never has.
    expect(screen.getByText(/publish the policy bundle/i)).toBeInTheDocument();
  });

  it("keeps the server's own detail verbatim beside the explanation", () => {
    render(
      <GovernanceRefusal
        error={refusal("policy-denied", { detail: "rule no_friday_deploys refused write_file" })}
        action="approve"
      />,
    );
    // Verbatim, not paraphrased. A paraphrase that quietly diverged from what the server said would be
    // worse than no paraphrase at all.
    expect(screen.getByTestId("refusal-detail")).toHaveTextContent(
      "rule no_friday_deploys refused write_file",
    );
  });

  it("presents an approval escalation as what it is rather than as a failure", () => {
    render(
      <GovernanceRefusal error={refusal("approval-required", { status: 202 })} action="revert" />,
    );
    expect(screen.getByText(/waiting for a human decision/i)).toBeInTheDocument();
    // "Not an error" said explicitly, because a 202 rendered under a red heading teaches the operator
    // that the system is broken when it is working.
    expect(screen.getByText(/not an error/i)).toBeInTheDocument();
  });

  it("falls back to the server's words for an unregistered type, and says it is doing so", () => {
    render(
      <GovernanceRefusal
        error={refusal("brand-new-refusal", { title: "Something Specific", detail: "the reason" })}
        action="save this thing"
      />,
    );
    expect(screen.getByText(/could not save this thing/i)).toBeInTheDocument();
    expect(screen.getByText(/no registered explanation/i)).toBeInTheDocument();
    expect(screen.getByText("Something Specific")).toBeInTheDocument();
    expect(screen.getByTestId("refusal-detail")).toHaveTextContent("the reason");
  });

  it("lists field errors when the problem carries them", () => {
    const error = new ApiProblemError({
      type: "https://errors.forgeops.dev/validation-failed",
      title: "Request validation failed",
      status: 422,
      errors: [{ pointer: "#/body/confirm_name", detail: "must exactly match the project's name" }],
    });
    render(<GovernanceRefusal error={error} action="delete" />);
    expect(screen.getByText("#/body/confirm_name")).toBeInTheDocument();
    expect(screen.getByText(/must exactly match/i)).toBeInTheDocument();
  });

  it("shows the trace id so a failure can be quoted in a bug report", () => {
    const error = new ApiProblemError({
      type: "https://errors.forgeops.dev/internal",
      title: "Internal Server Error",
      status: 500,
      trace_id: "abc123",
    });
    render(<GovernanceRefusal error={error} action="do it" />);
    expect(screen.getByText("abc123")).toBeInTheDocument();
  });

  it("handles a transport failure, which is a problem with status 0", () => {
    // `ApiTransportError extends ApiProblemError`, so one branch covers both and there is no error the
    // client can produce that lands outside this component.
    const error = new ApiTransportError({
      type: "urn:client:transport-error",
      title: "Network request failed",
      status: 0,
      detail: "Failed to fetch",
    });
    render(<GovernanceRefusal error={error} action="save" />);
    expect(screen.getByText("Network request failed")).toBeInTheDocument();
    expect(screen.getByTestId("refusal-detail")).toHaveTextContent("Failed to fetch");
  });

  it("renders a plain Error without pretending it carried a problem document", () => {
    render(<GovernanceRefusal error={new Error("boom")} action="save" />);
    expect(screen.getByText(/could not save/i)).toBeInTheDocument();
    // No Rule/Status table, because there is no problem document to read one from.
    expect(screen.queryByTestId("refusal-type")).not.toBeInTheDocument();
  });
});
