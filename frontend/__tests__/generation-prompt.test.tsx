import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { suggestGenerationPrompt } from "@/features/projects/generation-prompt";
import { GenerationPromptSuggestion } from "@/features/projects/GenerationPromptSuggestion";
import type { ReadinessCheck } from "@/features/projects/types";

/**
 * A suggestion that cannot work is worse than none.
 *
 * The readiness engine reports twenty-eight checks; generation emits four files. Pasting the
 * recommendation list runs a model for minutes and returns a Dockerfile addressing none of it, with
 * nothing to explain why the score did not move. These tests pin the honest behaviour: suggest only
 * what generation can satisfy, and when it can satisfy nothing, say so.
 */

function check(id: string, passed: boolean): ReadinessCheck {
  return {
    id,
    category: "containerization",
    passed,
    points: passed ? 1 : 0,
    max_points: 1,
    evidence: "",
    why_it_matters: "",
  };
}

describe("suggestGenerationPrompt", () => {
  it("asks for the failing checks a Dockerfile and manifests can satisfy", () => {
    const s = suggestGenerationPrompt([
      check("dockerfile_non_root", false),
      check("kubernetes_probes_declared", false),
      check("dockerfile_base_pinned", true),
    ]);
    expect(s.prompt).toContain("non-root");
    expect(s.prompt).toContain("readiness and liveness probes");
    // A PASSING check must not be asked for: the model would rewrite something already correct, and
    // the change set would carry an edit with no reason behind it.
    expect(s.prompt).not.toContain("pin the base image");
    expect(s.addresses).toEqual(["dockerfile_non_root", "kubernetes_probes_declared"]);
  });

  it("offers NO prompt when every failing check needs an artifact generation does not emit", () => {
    // This is the case the real project hit: 86/100 with the four failures all being CI, environment
    // and IaC. A prompt here would be a run that cannot change the score.
    const s = suggestGenerationPrompt([
      check("pipeline_actions_pinned", false),
      check("env_example_present", false),
      check("centralised_configuration", false),
      check("iac_remote_state_configured", false),
    ]);
    expect(s.prompt).toBeNull();
    expect(s.outOfScope).toEqual([
      "a CI workflow",
      "a .env.example",
      "a source change",
      "an IaC backend block",
    ]);
  });

  it("deduplicates artifacts, because three CI checks are still one CI workflow", () => {
    const s = suggestGenerationPrompt([
      check("pipeline_actions_pinned", false),
      check("pipeline_runs_tests", false),
      check("pipeline_stages_declared", false),
    ]);
    expect(s.outOfScope).toEqual(["a CI workflow"]);
  });

  it("reads the check ids, not the recommendation prose", () => {
    // Matching on wording would break the moment a recommendation is reworded; an id is the stable
    // fact and is already in the payload.
    const s = suggestGenerationPrompt([check("dockerfile_present", false)]);
    expect(s.addresses).toEqual(["dockerfile_present"]);
  });

  it("suggests nothing when nothing is failing", () => {
    const s = suggestGenerationPrompt([check("dockerfile_non_root", true)]);
    expect(s.prompt).toBeNull();
    expect(s.outOfScope).toEqual([]);
  });
});

describe("GenerationPromptSuggestion", () => {
  it("renders a copyable prompt when generation can help", () => {
    render(<GenerationPromptSuggestion checks={[check("dockerfile_non_root", false)]} />);
    expect(screen.getByTestId("suggested-prompt").textContent).toContain("non-root");
    // The environment note matters: set, a low-blast-radius change can auto-apply without the user
    // seeing the diff.
    expect(screen.getByText(/Leave the environment field empty/i)).toBeInTheDocument();
  });

  it("says plainly that generation cannot help, and what the failures need instead", () => {
    render(<GenerationPromptSuggestion checks={[check("env_example_present", false)]} />);
    expect(screen.getByTestId("generation-cannot-help").textContent).toContain(".env.example");
    expect(screen.queryByTestId("suggested-prompt")).not.toBeInTheDocument();
  });

  it("names what a prompt does NOT cover, rather than omitting it silently", () => {
    render(
      <GenerationPromptSuggestion
        checks={[check("dockerfile_non_root", false), check("pipeline_actions_pinned", false)]}
      />,
    );
    expect(screen.getByTestId("generation-out-of-scope").textContent).toContain("a CI workflow");
  });

  it("renders nothing at a full score, so a perfect report carries no caveat", () => {
    const { container } = render(
      <GenerationPromptSuggestion checks={[check("dockerfile_non_root", true)]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
