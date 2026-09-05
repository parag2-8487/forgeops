# SPDX-License-Identifier: FSL-1.1-ALv2
"""The compiled generation prompt must be derived, verifiable, deterministic and bounded.

Generation used to receive whatever the operator typed. That is the condition under which a model
invents: asked for "a Dockerfile" with no facts it must guess the language, the package manager, the
entry point and the port, and a guess that reads plausibly is indistinguishable from a fact until it
fails in production.

The compiler removes the guessing by stating everything. These four properties are what make the result
trustworthy rather than merely long, and each is asserted rather than asserted-about:

  DERIVED       two repositories must produce visibly different prompts
  VERIFIABLE    every path claimed to exist must be in the index
  DETERMINISTIC the same input must compile byte-identically
  BOUNDED       exceeding the budget must drop whole sections and say so, never truncate
"""

from __future__ import annotations

import pytest
from src.core.readiness import IndexEvidence, ReadinessEngine
from src.generation.prompt_compiler import (
    CHARS_PER_TOKEN_ESTIMATE,
    compile_prompt,
)

pytestmark = [pytest.mark.mandatory]


PYTHON_SERVICE = IndexEvidence(
    paths=("src/main.py", "requirements.txt", "tests/test_main.py"),
    contents={"requirements.txt": "fastapi==0.115.0\nuvicorn\n"},
)

PYTHON_INVENTORY = {
    "file_count": 3,
    "languages": ["python"],
    "manifests": ["requirements.txt"],
    "config_files": [],
    "entry_points": ["src/main.py"],
    "package_managers": ["pip"],
    "frameworks": [
        {
            "name": "FastAPI",
            "kind": "web",
            "confidence": "declared",
            "evidence": "requirements.txt",
            "version": "==0.115.0",
        }
    ],
}

GO_SERVICE = IndexEvidence(
    paths=("cmd/server/main.go", "go.mod", "go.sum", "internal/handler/handler.go"),
    contents={"go.mod": "module github.com/acme/edge\n\ngo 1.24\n"},
)

GO_INVENTORY = {
    "file_count": 4,
    "languages": ["go"],
    "manifests": ["go.mod"],
    "config_files": [],
    "entry_points": ["cmd/server/main.go"],
    "package_managers": ["go"],
    "frameworks": [
        {
            "name": "chi",
            "kind": "web",
            "confidence": "declared",
            "evidence": "go.mod",
            "version": "v5.1.0",
        }
    ],
}


def _compile(evidence: IndexEvidence, inventory: dict, **kwargs):
    result = ReadinessEngine().evaluate(evidence)
    return compile_prompt(
        checks=result.checks,
        paths=evidence.paths,
        contents=evidence.contents,
        inventory=inventory,
        **kwargs,
    )


class TestItIsDerivedNotTemplated:
    """A template with substitutions would read the same for every repository."""

    def test_two_repositories_compile_to_materially_different_prompts(self) -> None:
        python = _compile(PYTHON_SERVICE, PYTHON_INVENTORY).text
        go = _compile(GO_SERVICE, GO_INVENTORY).text

        assert python != go

        # Each must contain facts that CANNOT appear in the other, which is a stronger statement than
        # "the strings differ" — two templates with a name substituted would also differ.
        assert "FastAPI" in python and "FastAPI" not in go
        assert "requirements.txt" in python and "requirements.txt" not in go
        assert "src/main.py" in python and "src/main.py" not in go

        assert "chi" in go and "chi" not in python
        assert "go.mod" in go and "go.mod" not in python
        assert "cmd/server/main.go" in go and "cmd/server/main.go" not in python

    def test_the_declared_version_and_its_evidence_reach_the_prompt(self) -> None:
        """A version the model may rely on, and the file that establishes it.

        Without the evidence path the detection is an assertion; the model cannot check it and neither
        can the reader.
        """
        text = _compile(PYTHON_SERVICE, PYTHON_INVENTORY).text
        assert "==0.115.0" in text
        assert "from requirements.txt" in text

    def test_an_inferred_framework_is_marked_as_not_to_be_relied_on(self) -> None:
        """`declared` and `inferred` are different licences to act, and the prompt must say which."""
        inventory = dict(PYTHON_INVENTORY)
        inventory["frameworks"] = [
            {
                "name": "Celery",
                "kind": "runtime",
                "confidence": "inferred",
                "evidence": "src/main.py",
                "version": "",
            }
        ]
        text = _compile(PYTHON_SERVICE, inventory).text
        assert "INFERRED" in text
        assert "do not rely on it" in text


class TestEveryPathIsReal:
    """A prompt naming a path the scan never saw is the failure this module exists to prevent."""

    def test_every_referenced_path_is_in_the_index(self) -> None:
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY)
        indexed = {p.replace("\\", "/") for p in PYTHON_SERVICE.paths}
        for path in compiled.referenced_paths:
            assert path in indexed, (
                f"the prompt asserts {path!r} exists and the index does not contain it; that is a "
                "fabricated fact, and the model would build on it"
            )

    def test_a_write_target_that_exists_is_a_modify_and_never_a_create(self) -> None:
        """Letting the model decide is how an existing file gets silently overwritten."""
        with_dockerfile = IndexEvidence(
            paths=("Dockerfile", "src/main.py", "requirements.txt"),
            contents={
                "dockerfile": '# hand-written, do not lose this\nFROM python:3.12\nCMD ["python", "-m", "src"]\n',
                "requirements.txt": "fastapi==0.115.0\n",
            },
        )
        compiled = _compile(with_dockerfile, PYTHON_INVENTORY)
        assert "MODIFY `Dockerfile`" in compiled.text
        assert "CREATE `Dockerfile`" not in compiled.text
        # And it must say not to replace it, in as many words.
        assert "do not replace" in compiled.text

    def test_a_modify_quotes_the_file_with_real_line_numbers(self) -> None:
        with_dockerfile = IndexEvidence(
            paths=("Dockerfile", "src/main.py"),
            contents={"dockerfile": '# keep me\n# and me\nFROM python:3.12\nCMD ["x"]\n'},
        )
        compiled = _compile(with_dockerfile, PYTHON_INVENTORY)
        assert "FROM python:3.12" in compiled.text
        # Numbered, so a fault reported at a line can be located in the quoted text.
        assert "    3 | FROM python:3.12" in compiled.text

    def test_a_modify_names_what_must_be_preserved(self) -> None:
        """Read off the file, not guessed at: a comment block that exists must survive."""
        with_dockerfile = IndexEvidence(
            paths=("Dockerfile",),
            contents={
                "dockerfile": (
                    "# This block explains a workaround\n"
                    "# that took a week to find.\n"
                    "# Do not delete it.\n"
                    "FROM python:3.12\n"
                )
            },
        )
        compiled = _compile(with_dockerfile, PYTHON_INVENTORY)
        assert "must preserve" in compiled.text
        assert "comment block at lines 1 to 3" in compiled.text


class TestItIsDeterministic:
    """A prompt that varies between identical runs makes every generation unreproducible."""

    def test_the_same_input_compiles_identically(self) -> None:
        first = _compile(PYTHON_SERVICE, PYTHON_INVENTORY).text
        second = _compile(PYTHON_SERVICE, PYTHON_INVENTORY).text
        assert first == second

    def test_the_ordering_does_not_depend_on_dictionary_iteration(self) -> None:
        """The inventory arrives as JSON, whose key order is not guaranteed to be stable.

        Compiling from two dicts with the same content in a different insertion order must produce the
        same text, or the prompt is a function of how the row happened to deserialise.
        """
        reordered = {
            "frameworks": PYTHON_INVENTORY["frameworks"],
            "package_managers": PYTHON_INVENTORY["package_managers"],
            "entry_points": PYTHON_INVENTORY["entry_points"],
            "config_files": PYTHON_INVENTORY["config_files"],
            "manifests": PYTHON_INVENTORY["manifests"],
            "languages": PYTHON_INVENTORY["languages"],
            "file_count": PYTHON_INVENTORY["file_count"],
        }
        assert _compile(PYTHON_SERVICE, reordered).text == _compile(PYTHON_SERVICE, PYTHON_INVENTORY).text


class TestItIsBounded:
    """Long is fine. Silently cut is not."""

    def test_a_small_budget_defers_whole_sections_and_names_them(self) -> None:
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY, token_budget=900)
        assert compiled.deferred_checks, "a budget this small must have deferred something"
        assert "deferred" in compiled.budget_strategy
        # Every deferred check is NAMED, so a user can see that one run cannot cover everything.
        for check_id in compiled.deferred_checks:
            assert check_id not in compiled.addressed_checks

    def test_nothing_is_truncated_mid_instruction(self) -> None:
        """A cut instruction is acted on as though it were whole, so it is worse than a missing one."""
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY, token_budget=900)
        # EVERY SECTION SURVIVES. Dropping an artifact must not cost the facts, the acceptance criteria
        # or the prohibitions - those are what stop the model inventing, and a prompt missing them is
        # more dangerous when it is short than when it is long.
        for heading in (
            "## 1. ESTABLISHED FACTS ABOUT THIS REPOSITORY",
            "## 2. WHAT TO PRODUCE",
            "## 3. HOW EACH FILE WILL BE CHECKED",
            "## 4. PROHIBITIONS",
        ):
            assert heading in compiled.text, f"the budget cut removed {heading}"
        # And the text ends on a complete line rather than mid-token.
        assert compiled.text.endswith("\n")
        assert not compiled.text.rstrip().endswith(("-", ",", "the", "a"))

    def test_a_generous_budget_defers_nothing_and_says_so(self) -> None:
        """The strategy is always stated, so a reader never infers it from the absence of a warning."""
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY, token_budget=200_000)
        assert compiled.deferred_checks == ()
        assert "nothing was deferred" in compiled.budget_strategy

    def test_the_estimate_is_reported_against_the_budget(self) -> None:
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY, token_budget=50_000)
        assert compiled.token_budget == 50_000
        assert compiled.token_estimate == len(compiled.text) // CHARS_PER_TOKEN_ESTIMATE


class TestItAsksOnlyForWhatIsNeeded:
    def test_a_passing_check_produces_no_instruction(self) -> None:
        """A repository with a correct Dockerfile must not be told to write one."""
        good = IndexEvidence(
            paths=("Dockerfile", "src/main.py"),
            contents={
                "dockerfile": (
                    "FROM python:3.12-slim@sha256:" + "a" * 64 + " AS build\n"
                    "RUN pip install .\n"
                    "FROM python:3.12-slim@sha256:" + "a" * 64 + "\n"
                    "USER 10001\n"
                    "HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1\n"
                    'CMD ["python", "-m", "src"]\n'
                )
            },
        )
        result = ReadinessEngine().evaluate(good)
        dockerfile_checks = [c for c in result.checks if c.id.startswith("dockerfile_")]
        assert all(c.passed for c in dockerfile_checks), (
            "the fixture is meant to be a correct Dockerfile; "
            f"failing: {[c.id for c in dockerfile_checks if not c.passed]}"
        )
        compiled = _compile(good, PYTHON_INVENTORY)
        assert "`Dockerfile`" not in compiled.text

    def test_selecting_checks_narrows_the_instruction(self) -> None:
        """FR-89's shape: act on the recommendations you chose, not on everything."""
        compiled = _compile(
            PYTHON_SERVICE,
            PYTHON_INVENTORY,
            selected_check_ids=["dockerfile_present", "dockerfile_non_root"],
        )
        assert compiled.addressed_checks == ("dockerfile_non_root", "dockerfile_present")
        assert "k8s/deployment.yaml" not in compiled.write_targets

    def test_the_validator_for_each_artifact_is_named(self) -> None:
        """The model should know the bar before it writes a line."""
        text = _compile(PYTHON_SERVICE, PYTHON_INVENTORY).text
        assert "validate.k8s" in text
        assert "HOW EACH FILE WILL BE CHECKED" in text

    def test_unaddressable_checks_are_listed_with_their_reason(self) -> None:
        """So nothing in the report looks like an oversight."""
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY)
        assert compiled.unaddressable
        joined = " ".join(compiled.unaddressable)
        assert "dependency_lockfile_present" in joined
        assert "resolving against a live registry" in joined


class TestNoPathIsEverAPlaceholder:
    """A path the model cannot act on is worse than an artifact it was not asked for.

    The findings table carries one placeholder — `charts/<name>/Chart.yaml` — because the table is
    repository-independent and cannot know the name. Reaching a model verbatim, the model would either
    invent a name or write the angle brackets into somebody's tree.
    """

    def test_the_chart_directory_is_resolved_from_the_project_name(self) -> None:
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY, project_name="My Service 2!")
        assert "charts/my-service-2/Chart.yaml" in compiled.write_targets
        assert "<name>" not in compiled.text

    def test_no_write_target_contains_a_placeholder_or_a_parenthetical(self) -> None:
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY, project_name="edge")
        for target in compiled.write_targets:
            assert "<" not in target, f"{target!r} carries a placeholder the model cannot resolve"
            assert "(" not in target, f"{target!r} is a convention rather than a path"

    def test_an_unresolvable_path_is_reported_rather_than_guessed(self) -> None:
        """With no project name the chart directory cannot be known, and must not be invented."""
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY, project_name="")
        assert not any("Chart.yaml" in target for target in compiled.write_targets)
        assert any("helm_chart_present" in entry for entry in compiled.unaddressable)
        joined = " ".join(compiled.unaddressable)
        assert "no location this scan can determine" in joined

    def test_the_lint_config_path_follows_the_detected_language(self) -> None:
        """Derived from the scan, not from a default: a Go project must not be asked for ruff.toml."""
        python = _compile(PYTHON_SERVICE, PYTHON_INVENTORY, project_name="p")
        assert "ruff.toml" in python.write_targets

        go = _compile(GO_SERVICE, GO_INVENTORY, project_name="p")
        assert ".golangci.yml" in go.write_targets
        assert "ruff.toml" not in go.write_targets


class TestItCoversEveryArtifactTheScoreMeasures:
    """The generator could produce four kinds of file. The parser was the reason, not the model."""

    def test_a_bare_repository_is_asked_for_every_generatable_kind(self) -> None:
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY, project_name="edge")
        targets = " ".join(compiled.write_targets)
        for expected in (
            "Dockerfile",
            ".dockerignore",
            "k8s/deployment.yaml",
            "docker-compose.yml",
            "Chart.yaml",
            ".github/workflows/ci.yml",
            ".env.example",
            "terraform/",
            "SECURITY.md",
            ".gitleaks.toml",
            "ruff.toml",
        ):
            assert expected in targets, (
                f"{expected} was not requested; before the required-set fix the generator could only "
                "ever be asked for a Dockerfile and three Kubernetes manifests"
            )

    def test_the_ci_workflow_is_no_longer_reported_as_impossible(self) -> None:
        """It was listed as a generator gap. It is not one any more, and the prompt must not say so."""
        compiled = _compile(PYTHON_SERVICE, PYTHON_INVENTORY, project_name="edge")
        assert "ci_pipeline_present" in compiled.addressed_checks
        assert not any("ci_pipeline_present" in entry for entry in compiled.unaddressable)
