# SPDX-License-Identifier: FSL-1.1-ALv2
"""The model prompt and the artifact parse (design §11.5, §12.6 step 6).

The parse is the boundary between free-form model output and `change_items` rows, so its FAILURE
modes are what matter. A parser that filled a missing file with a default would produce a change
set containing a template on a run recorded as `provider` — the exact fabrication this work
removes — so every case below either yields the complete required set or names what is absent.

Fixtures live here, which is where the standing rule puts them. The same parse is exercised on
bytes a real model emitted in `tests/integration/test_self_hosted_generation.py`.
"""

from __future__ import annotations

import pytest
from src.generation.model_prompt import (
    REQUIRED_ARTIFACTS,
    ArtifactParseError,
    ProjectFacts,
    build_generation_prompt,
    facts_from_project,
    parse_artifacts,
)

FACTS = ProjectFacts(
    app_name="checkout-api",
    runtime="python",
    port=8000,
    base_image="python:3.11-slim",
    start_command=("python", "main.py"),
)


def _block(path: str, body: str, *, fence: str = "```") -> str:
    return f"### FILE: {path}\n{fence}\n{body}\n```\n"


def _complete(**overrides: str) -> str:
    bodies = {path: f"content of {path}\n" for path in REQUIRED_ARTIFACTS}
    bodies.update(overrides)
    return "\n".join(_block(path, bodies[path].rstrip("\n")) for path in REQUIRED_ARTIFACTS)


class TestTheParseYieldsTheRequiredSetOrNamesWhatIsMissing:
    def test_a_well_formed_response_yields_all_four_in_order(self) -> None:
        parsed = parse_artifacts(_complete())
        assert list(parsed) == list(REQUIRED_ARTIFACTS)

    def test_content_is_verbatim_between_the_fences(self) -> None:
        """Whitespace is significant in a Dockerfile and a manifest.

        Normalising model output would mean the bytes the deterministic gate judged are not the
        bytes written to the repository.
        """
        dockerfile = "FROM python:3.11-slim\n\n    RUN  echo   spaced\nUSER 1001"
        parsed = parse_artifacts(_complete(Dockerfile=dockerfile))
        assert parsed["Dockerfile"] == dockerfile + "\n"

    def test_a_language_tagged_fence_is_accepted(self) -> None:
        raw = _block("Dockerfile", "FROM x", fence="```dockerfile") + "".join(
            _block(path, "body") for path in REQUIRED_ARTIFACTS[1:]
        )
        assert parse_artifacts(raw)["Dockerfile"] == "FROM x\n"

    @pytest.mark.parametrize("marker", ["### FILE:", "## FILE:", "# FILE:", "FILE:", "###   file:"])
    def test_the_heading_level_and_case_do_not_matter(self, marker: str) -> None:
        """Models vary the heading freely and the variance carries no information.

        Lenient where it costs nothing; strict about the path, because an unrecognised path becomes
        a file reported missing rather than a guess.
        """
        raw = "".join(f"{marker} {path}\n```\nbody\n```\n" for path in REQUIRED_ARTIFACTS)
        assert list(parse_artifacts(raw)) == list(REQUIRED_ARTIFACTS)

    def test_a_leading_path_component_is_normalised(self) -> None:
        raw = "".join(f"### FILE: ./{path}\n```\nbody\n```\n" for path in REQUIRED_ARTIFACTS)
        assert list(parse_artifacts(raw)) == list(REQUIRED_ARTIFACTS)

    def test_prose_before_the_first_marker_is_discarded(self) -> None:
        """A chatty model is not a broken one."""
        raw = "Sure! Here are the four files you asked for.\n\n" + _complete()
        assert list(parse_artifacts(raw)) == list(REQUIRED_ARTIFACTS)

    def test_an_unrequested_file_is_dropped(self) -> None:
        """An unrequested file in a change set is a write nobody asked for."""
        raw = _complete() + _block("README.md", "# hello")
        assert list(parse_artifacts(raw)) == list(REQUIRED_ARTIFACTS)

    def test_a_missing_file_names_itself_rather_than_being_synthesised(self) -> None:
        raw = "".join(_block(path, "body") for path in REQUIRED_ARTIFACTS[:-1])
        with pytest.raises(ArtifactParseError) as raised:
            parse_artifacts(raw)
        assert raised.value.missing == ("k8s/ingress.yaml",)
        assert "k8s/ingress.yaml" in str(raised.value)
        # The findings go into the retry prompt, so what WAS found has to be reported too.
        assert "Dockerfile" in str(raised.value)

    def test_an_empty_block_counts_as_missing(self) -> None:
        """A present marker with no body is a file that does not exist yet."""
        raw = _complete().replace("content of Dockerfile", "   ")
        with pytest.raises(ArtifactParseError) as raised:
            parse_artifacts(raw)
        assert "Dockerfile" in raised.value.missing

    def test_no_markers_at_all_reports_every_file(self) -> None:
        with pytest.raises(ArtifactParseError) as raised:
            parse_artifacts("I cannot help with that request.")
        assert raised.value.missing == REQUIRED_ARTIFACTS

    def test_a_marker_inside_a_fence_does_not_start_a_new_file(self) -> None:
        """A Dockerfile comment reading `# FILE: something` must not split the block."""
        dockerfile = "FROM x\n# FILE: not-a-real-marker\nUSER 1001"
        parsed = parse_artifacts(_complete(Dockerfile=dockerfile))
        assert "not-a-real-marker" not in parsed
        assert "# FILE: not-a-real-marker" in parsed["Dockerfile"]


class TestThePromptStatesTheContractTheGateEnforces:
    def test_it_names_every_clause_the_deterministic_gate_checks(self) -> None:
        """A requirement the gate enforces and the prompt does not state is a guaranteed retry."""
        prompt = build_generation_prompt(operator_prompt="a checkout service", facts=FACTS)
        assert "FROM python:3.11-slim" in prompt
        assert "USER 1001" in prompt
        assert "apiVersion:" in prompt and "kind:" in prompt and "metadata:" in prompt
        for path in REQUIRED_ARTIFACTS:
            assert f"### FILE: {path}" in prompt

    def test_it_carries_the_project_facts_rather_than_a_fixed_name(self) -> None:
        prompt = build_generation_prompt(operator_prompt="x", facts=FACTS)
        assert "checkout-api" in prompt
        assert "forgeops-app" not in prompt

    def test_the_operator_request_is_present_but_not_the_authority(self) -> None:
        """A prompt is a request, not a fact about the repository (`_render`'s own argument)."""
        prompt = build_generation_prompt(operator_prompt="make it a node app", facts=FACTS)
        assert "make it a node app" in prompt
        # The facts still say python, and they are labelled authoritative.
        assert "runtime: python" in prompt
        assert "authoritative" in prompt

    def test_findings_are_quoted_back_so_a_retry_is_a_repair(self) -> None:
        prompt = build_generation_prompt(
            operator_prompt="x", facts=FACTS, previous_findings=("Dockerfile does not drop root",), attempt=2
        )
        assert "Dockerfile does not drop root" in prompt
        assert "REJECTED" in prompt

    def test_the_attempt_number_makes_each_retry_a_distinct_cache_key(self) -> None:
        """Identical findings across retries would otherwise produce an identical prompt.

        The cache key is a digest of the prompt, so attempts 2 and 3 with the same findings would
        hit L1 and serve the rejected content back — three attempts, two provider calls. Observed,
        not hypothesised.
        """
        findings = ("Dockerfile does not drop root with a USER instruction",)
        second = build_generation_prompt(operator_prompt="x", facts=FACTS, previous_findings=findings, attempt=2)
        third = build_generation_prompt(operator_prompt="x", facts=FACTS, previous_findings=findings, attempt=3)
        assert second != third

    def test_a_first_attempt_is_stable_across_runs_so_l1_can_serve_it(self) -> None:
        """The other direction: without findings the prompt must be deterministic, or L1 never hits."""
        one = build_generation_prompt(operator_prompt="x", facts=FACTS)
        two = build_generation_prompt(operator_prompt="x", facts=FACTS)
        assert one == two


class TestFactsMatchWhatTheTemplatePathWouldHaveUsed:
    """A template fallback must describe the SAME application the provider attempt described.

    If the two derivations disagreed, a fallback would silently produce different infrastructure
    than the attempt it replaced, and the operator would see the port change with no way to know
    why.
    """

    def test_settings_win_over_the_runtime_default(self) -> None:
        facts = facts_from_project(
            project={"name": "x", "settings": {"runtime": "node", "port": 4321, "base_image": "node:22-alpine"}},
            operator_prompt="a python service",
            default_app_name="x",
        )
        assert facts.runtime == "node"
        assert facts.port == 4321
        assert facts.base_image == "node:22-alpine"

    def test_the_prompt_is_the_last_resort_hint(self) -> None:
        facts = facts_from_project(
            project={"name": "x", "settings": {}}, operator_prompt="an express node api", default_app_name="x"
        )
        assert facts.runtime == "node"
        assert facts.port == 3000

    def test_a_non_numeric_configured_port_falls_back_rather_than_raising(self) -> None:
        """Operator-entered settings are not a reason to fail a run."""
        facts = facts_from_project(
            project={"name": "x", "settings": {"port": "not-a-number"}},
            operator_prompt="a python service",
            default_app_name="x",
        )
        assert facts.port == 8000

    def test_a_string_start_command_is_split(self) -> None:
        facts = facts_from_project(
            project={"name": "x", "settings": {"start_command": "uvicorn main:app --port 9000"}},
            operator_prompt="a python service",
            default_app_name="x",
        )
        assert facts.start_command == ("uvicorn", "main:app", "--port", "9000")

    def test_no_project_row_still_yields_usable_facts(self) -> None:
        facts = facts_from_project(project=None, operator_prompt="a python service", default_app_name="fallback")
        assert facts.app_name == "fallback"
        assert facts.runtime == "python"
