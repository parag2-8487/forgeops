/**
 * Turn a readiness report into a generation prompt — or say honestly that it cannot.
 *
 * WHY THIS IS NOT "PASTE THE RECOMMENDATIONS IN". The readiness engine reports twenty-eight checks
 * across six categories. Generation emits exactly four files: `Dockerfile`, `k8s/deployment.yaml`,
 * `k8s/service.yaml` and `k8s/ingress.yaml`. So most recommendations name things it cannot produce —
 * a pinned CI action, a `.env.example`, a Terraform backend block, a committed lockfile.
 *
 * Pasting the recommendation list would therefore run a model for minutes and return a Dockerfile
 * that addresses none of them, leaving the score exactly where it was and the user with no way to
 * know why. That is worse than offering nothing, because it looks like the feature worked.
 *
 * So this maps only the checks generation CAN satisfy, and when none of a project's failing checks
 * are in that set it returns no prompt and the reason. A suggestion that cannot help is not a
 * suggestion.
 */

/** A check generation can address, and the clause that asks for it. */
const ADDRESSABLE: Readonly<Record<string, string>> = {
  // ── Containerization: all of these are properties of the Dockerfile it writes ──
  dockerfile_present: "add a Dockerfile that builds and runs this service",
  dockerfile_multi_stage: "use a multi-stage build so build tools are absent from the final image",
  dockerfile_non_root: "run as a non-root user created in the image",
  dockerfile_base_pinned: "pin the base image to an explicit version tag rather than latest",
  dockerfile_healthcheck_present: "declare a HEALTHCHECK",

  // ── Orchestration: properties of the three manifests it writes ──
  kubernetes_manifests_present: "add Kubernetes manifests: a Deployment, a Service and an Ingress",
  kubernetes_resource_limits_declared:
    "declare CPU and memory requests and limits on every container",
  kubernetes_probes_declared: "declare readiness and liveness probes",
  kubernetes_image_tags_pinned:
    "reference images by an explicit tag in every manifest, never latest",
};

/**
 * Checks generation cannot address, and the artifact that would be needed.
 *
 * Held as data rather than inferred from absence, so the panel can explain WHY a failing check has no
 * suggestion instead of silently omitting it. An unexplained omission reads as an oversight.
 */
const OUT_OF_SCOPE: Readonly<Record<string, string>> = {
  dockerignore_present: "a .dockerignore",
  helm_chart_present: "a Helm chart",
  compose_file_present: "a Compose file",
  ci_pipeline_present: "a CI workflow",
  automated_tests_present: "a test suite",
  lint_configuration_present: "a linter configuration",
  pipeline_stages_declared: "a CI workflow",
  pipeline_runs_tests: "a CI workflow",
  pipeline_actions_pinned: "a CI workflow",
  env_example_present: "a .env.example",
  no_committed_env_file: "removing a committed .env",
  centralised_configuration: "a source change",
  security_policy_present: "a SECURITY.md",
  secret_scanning_configured: "a secret-scanning configuration",
  no_secrets_found_by_scan: "removing committed secrets",
  dependency_lockfile_present: "a dependency lockfile",
  no_committed_key_material: "removing committed key material",
  iac_sources_present: "IaC sources",
  iac_provider_lock_present: "an IaC provider lock",
  iac_remote_state_configured: "an IaC backend block",
};

export interface PromptSuggestion {
  /** The prompt to paste, or `null` when generation cannot improve this project's score. */
  prompt: string | null;
  /** The check ids the prompt asks for, so the panel can show what it will act on. */
  addresses: string[];
  /** Artifacts the failing checks need that generation does not emit, deduplicated. */
  outOfScope: string[];
}

/**
 * Build a suggestion from a report's failing checks.
 *
 * Deliberately takes the CHECKS rather than the `recommendations` strings: a recommendation is prose
 * written for a human and matching on it would be matching on wording, which changes. A check id is
 * the stable fact, and it is already in the payload.
 */
export function suggestGenerationPrompt(
  checks: ReadonlyArray<{ id: string; passed: boolean }>,
): PromptSuggestion {
  const failing = checks.filter((check) => !check.passed).map((check) => check.id);

  const addresses = failing.filter((id) => id in ADDRESSABLE);
  const outOfScope = [
    ...new Set(failing.filter((id) => id in OUT_OF_SCOPE).map((id) => OUT_OF_SCOPE[id])),
  ];

  if (addresses.length === 0) {
    return { prompt: null, addresses: [], outOfScope };
  }

  // One sentence naming the artifacts, then the specific asks. The artifacts are named because the
  // model is being asked to produce files, and the asks are the failing checks in the order the
  // report listed them, so a reader can match prompt to report line by line.
  const clauses = addresses.map((id) => ADDRESSABLE[id]);
  return {
    prompt:
      "Generate a Dockerfile and Kubernetes manifests for this service. " +
      `Specifically: ${clauses.join("; ")}.`,
    addresses,
    outOfScope,
  };
}
