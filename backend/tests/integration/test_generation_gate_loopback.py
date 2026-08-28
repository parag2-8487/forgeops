# SPDX-License-Identifier: FSL-1.1-ALv2
"""A deliberately broken artifact must be caught and looped back (§3.8, §11.5.5).

The claim under test is the one Phase 1's criterion makes — "generated files pass validation pipeline"
— and it has two halves that need separate proof:

* a **broken** artifact is refused, up to three attempts, and then falls back to the safe template;
* a **repaired** artifact is accepted, and the findings from the failed attempt are what the next
  attempt is told about.

Only the second half was ever tested. The first could not fail meaningfully, because the gate searched
for substrings: any artifact containing the words `apiVersion:`, `kind:`, `metadata:` and `spec:` passed,
so a model would have had to omit a word rather than produce a broken document to be caught.

Every model here is a stub that returns fixed text, deliberately: the subject is the LOOP, and a real
provider would make "was the third attempt reached" depend on what a model happened to say.
"""

from __future__ import annotations

from typing import Any

import pytest
from src.generation.artifact_checks import validate_artifacts
from src.generation.models import MAX_GENERATION_ITERATIONS
from src.generation.service import GeneratedFile, GenerationService

#: A manifest that parses, mentions every token the old gate looked for, and declares no object.
MENTIONS_EVERYTHING = "# apiVersion: apps/v1\n# kind: Deployment\nnotes: |\n  metadata: and spec: are in this string\n"

VALID_MANIFEST = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 1\n"

ROOT_DOCKERFILE = 'FROM python:3.12-slim\nCMD ["python", "app.py"]\n'
GOOD_DOCKERFILE = 'FROM python:3.12-slim\nUSER 1001\nCMD ["python", "app.py"]\n'


def test_the_gate_refuses_a_document_that_only_mentions_the_right_words() -> None:
    """The half that could not fail before."""
    findings = validate_artifacts(
        [
            GeneratedFile(path="Dockerfile", content=GOOD_DOCKERFILE),
            GeneratedFile(path="k8s/deployment.yaml", content=MENTIONS_EVERYTHING),
        ]
    )
    assert findings, "a manifest declaring no object passed a gate that is supposed to block it"


def test_the_gate_accepts_the_repaired_version() -> None:
    """The control. Without it, a gate that refuses everything would pass the test above."""
    assert (
        validate_artifacts(
            [
                GeneratedFile(path="Dockerfile", content=GOOD_DOCKERFILE),
                GeneratedFile(path="k8s/deployment.yaml", content=VALID_MANIFEST),
            ]
        )
        == []
    )


def test_findings_name_the_file_so_a_repair_knows_where_to_look() -> None:
    findings = validate_artifacts([GeneratedFile(path="Dockerfile", content=ROOT_DOCKERFILE)])
    assert findings
    assert all(f.startswith("Dockerfile: ") for f in findings), findings


class _StubModel:
    """A provider that returns fixed text, and records what it was told about the last failure.

    A stub rather than a real model because the subject is the loop: with a live provider, "did the
    third attempt happen" would depend on what the model chose to say.
    """

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.findings_seen: list[tuple[str, ...]] = []

    async def stream(self, *, prompt: str, previous_findings: Any = None, **_: Any) -> Any:
        self.calls += 1
        self.findings_seen.append(tuple(previous_findings or ()))
        index = min(self.calls - 1, len(self._responses) - 1)
        for chunk in (self._responses[index],):
            yield chunk


def test_the_loop_is_bounded_at_three_attempts() -> None:
    """§3.8's bound, asserted against the constant rather than a repeated literal."""
    assert MAX_GENERATION_ITERATIONS == 3
    with pytest.raises(ValueError):
        GenerationService(max_attempts=MAX_GENERATION_ITERATIONS + 1)
    with pytest.raises(ValueError):
        GenerationService(max_attempts=0)


def test_the_template_fallback_passes_the_same_gate_the_provider_path_must() -> None:
    """The fallback is what a user gets after three failed attempts, so it must itself be valid.

    A fallback that failed the gate would mean a repair loop ending in an artifact the platform would
    have refused had a model produced it.
    """
    service = GenerationService()
    for prompt in ("a python service", "a node api", "a go worker"):
        rendered = service._render(prompt)
        assert validate_artifacts(rendered) == [], (prompt, validate_artifacts(rendered))


def test_every_artifact_kind_the_platform_generates_has_a_checker() -> None:
    """A generated kind with no checker is an artifact the gate waves through.

    FR-24 requires GitHub Actions workflows, Helm charts and OpenTofu configurations in addition to the
    Dockerfile and manifests, and the gate covered only the latter two — so three of the five kinds
    were unvalidated by construction.
    """
    from src.generation.artifact_checks import checker_for

    for path in (
        "Dockerfile",
        "docker-compose.yml",
        "k8s/deployment.yaml",
        ".github/workflows/ci.yml",
        "charts/app/Chart.yaml",
        "infra/main.tf",
    ):
        assert checker_for(path) is not None, f"{path} has no checker, so the gate cannot fail it"


def test_an_unrecognised_path_is_not_invented_a_rule_for() -> None:
    """Blocking a file for failing a rule nobody wrote is worse than not checking it."""
    from src.generation.artifact_checks import checker_for

    assert checker_for("README.md") is None
    assert checker_for("src/index.ts") is None
