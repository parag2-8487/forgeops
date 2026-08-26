# SPDX-License-Identifier: FSL-1.1-ALv2
"""The readiness engine over INDEX evidence (phases.md §1.4, Leaf 12.3).

These are pure: `ReadinessEngine.evaluate` takes an `IndexEvidence` and returns a score, so
the cases below are statements about the scoring rules rather than about a database. The
database-backed half — that the evidence really comes from `file_tree` and `file_contents`
— is `tests/integration/test_readiness_from_index.py`.
"""

from __future__ import annotations

import pytest
from src.projects.readiness import (
    CATEGORY_FIELDS,
    CATEGORY_WEIGHTS,
    IndexEvidence,
    ReadinessEngine,
    apply_ignore_globs,
)

pytestmark = [pytest.mark.mandatory]

MULTI_STAGE = "FROM golang:1.24 AS build\nRUN go build ./...\n\nFROM gcr.io/distroless/static\nUSER 65532:65532\n"
SINGLE_STAGE_ROOT = "FROM golang:1.24\nRUN go build ./...\n"


def _check(result, check_id: str):
    for check in result.checks:
        if check.id == check_id:
            return check
    raise AssertionError(f"no check {check_id!r} in {[c.id for c in result.checks]}")


def test_the_weights_are_the_six_phases_categories_and_sum_to_100() -> None:
    """A weighted mean, not an arbitrary sum that lands near 100.

    If the weights did not sum to 100 the overall score would silently change meaning
    whenever a category was added, and the 80/50 level thresholds would drift with it.
    """
    assert sum(CATEGORY_WEIGHTS.values()) == 100
    assert set(CATEGORY_WEIGHTS) == {
        "containerization",
        "ci_config",
        "orchestration",
        "env_config",
        "security_policy",
        "iac",
    }
    assert set(CATEGORY_FIELDS) == set(CATEGORY_WEIGHTS)


def test_an_unscanned_project_scores_zero_and_says_it_is_unindexed() -> None:
    """Nothing read means nothing concluded.

    Not even the absence-based checks pass here: an empty index satisfies "no committed
    .env" trivially, and scoring that would be a conclusion drawn from no evidence.
    """
    result = ReadinessEngine().evaluate(IndexEvidence())

    assert result.overall_score == 0
    assert result.level == "blocked"
    assert result.indexed is False
    assert result.evaluated_paths == 0
    assert all(value == 0 for value in result.breakdown.model_dump().values())
    assert len(result.recommendations) == 1
    assert "scan" in result.recommendations[0].lower()


def test_tests_are_not_assumed_to_exist() -> None:
    """`has_tests` defaulted to True, handing every project a quarter of its score.

    Absent an explicit statement the answer is now DERIVED from the paths, and a repository
    with no test files fails the check.
    """
    without = ReadinessEngine().evaluate(IndexEvidence(paths=("main.go", "readme.md")))
    assert _check(without, "automated_tests_present").passed is False

    with_tests = ReadinessEngine().evaluate(IndexEvidence(paths=("main.go", "internal/repo_test.go")))
    assert _check(with_tests, "automated_tests_present").passed is True
    assert _check(with_tests, "automated_tests_present").evidence == "internal/repo_test.go"

    # A test DIRECTORY counts too, since a repository may name its files anything.
    with_dir = ReadinessEngine().evaluate(IndexEvidence(paths=("tests/e2e/journey.spec.ts",)))
    assert _check(with_dir, "automated_tests_present").passed is True


def test_an_explicit_statement_about_tests_is_honoured() -> None:
    """The field exists so a caller may state something the path list cannot show — not so
    that it can be assumed."""
    stated = ReadinessEngine().evaluate(IndexEvidence(paths=("main.go",), has_tests=True))
    assert _check(stated, "automated_tests_present").passed is True

    denied = ReadinessEngine().evaluate(IndexEvidence(paths=("main_test.go",), has_tests=False))
    assert _check(denied, "automated_tests_present").passed is False


def test_the_dockerfile_checks_read_the_body_not_the_name() -> None:
    engine = ReadinessEngine()
    good = engine.evaluate(IndexEvidence(paths=("dockerfile",), contents={"dockerfile": MULTI_STAGE}))
    bad = engine.evaluate(IndexEvidence(paths=("dockerfile",), contents={"dockerfile": SINGLE_STAGE_ROOT}))

    assert _check(good, "dockerfile_multi_stage").passed is True
    assert _check(good, "dockerfile_non_root").passed is True
    assert _check(bad, "dockerfile_multi_stage").passed is False
    # No USER at all means root, which is the case the check exists for.
    assert _check(bad, "dockerfile_non_root").passed is False
    assert good.breakdown.containerization_score > bad.breakdown.containerization_score


def test_a_user_root_directive_is_not_non_root() -> None:
    """The failure mode a naive `USER` search would miss."""
    result = ReadinessEngine().evaluate(
        IndexEvidence(paths=("dockerfile",), contents={"dockerfile": "FROM alpine\nUSER root\n"})
    )
    assert _check(result, "dockerfile_non_root").passed is False


def test_a_committed_env_file_and_committed_key_material_lose_points() -> None:
    clean = ReadinessEngine().evaluate(IndexEvidence(paths=("main.go", ".env.example")))
    dirty = ReadinessEngine().evaluate(IndexEvidence(paths=("main.go", ".env.example", ".env", "certs/server.key")))

    assert _check(clean, "no_committed_env_file").passed is True
    assert _check(dirty, "no_committed_env_file").passed is False
    assert _check(dirty, "no_committed_env_file").evidence == ".env"
    assert _check(dirty, "no_committed_key_material").passed is False
    assert dirty.overall_score < clean.overall_score


def test_terraform_remote_state_is_read_from_the_body() -> None:
    paths = ("infra/main.tf", "infra/.terraform.lock.hcl")
    remote = ReadinessEngine().evaluate(
        IndexEvidence(paths=paths, contents={"infra/main.tf": 'terraform {\n  backend "s3" {}\n}\n'})
    )
    local = ReadinessEngine().evaluate(
        IndexEvidence(paths=paths, contents={"infra/main.tf": 'resource "aws_s3_bucket" "b" {}\n'})
    )

    assert _check(remote, "iac_remote_state_configured").passed is True
    assert _check(local, "iac_remote_state_configured").passed is False
    assert remote.breakdown.iac_score == 100
    assert local.breakdown.iac_score == 75


def test_every_check_that_passes_names_its_evidence() -> None:
    """A category above zero with no evidence anywhere is indistinguishable from a bug."""
    result = ReadinessEngine().evaluate(
        IndexEvidence(
            paths=("dockerfile", ".dockerignore", ".github/workflows/ci.yml", "tests/test_x.py"),
            contents={"dockerfile": MULTI_STAGE},
        )
    )
    for check in result.checks:
        if check.id.startswith("no_"):
            # An absence check reports the OFFENDING path when it fails, and nothing when it
            # passes — there is no file to point at for a file that is not there.
            assert check.evidence == "" if check.passed else check.evidence != "", check.id
        elif check.passed:
            assert check.evidence, check.id
        else:
            # Nothing was found, so there is nothing to name. Evidence on a failed positive
            # check would read as "found, and still failed", which is a different claim.
            assert check.evidence == "", check.id


def test_the_same_evidence_always_scores_identically() -> None:
    """`analysis_reports` stores a score next to an `inventory_hash` so two reports can be
    compared; the comparison is meaningless unless identical evidence scores identically."""
    evidence = IndexEvidence(
        paths=("dockerfile", "k8s/deployment.yaml", "infra/main.tf", "tests/test_x.py"),
        contents={"dockerfile": MULTI_STAGE},
    )
    engine = ReadinessEngine()
    first, second = engine.evaluate(evidence), engine.evaluate(evidence)

    assert first.model_dump() == second.model_dump()


def test_ignore_globs_only_remove_evidence() -> None:
    paths = ("infra/main.tf", "dockerfile", "vendor/lib/dockerfile")

    assert apply_ignore_globs(paths, None) == ("dockerfile", "infra/main.tf", "vendor/lib/dockerfile")
    assert apply_ignore_globs(paths, ["vendor/*"]) == ("dockerfile", "infra/main.tf")
    assert apply_ignore_globs(paths, ["infra/*", "vendor/*"]) == ("dockerfile",)
    # A glob that matches everything yields an unindexed project, not a perfect one.
    assert apply_ignore_globs(paths, ["*"]) == ()
    scored = ReadinessEngine().evaluate(IndexEvidence(paths=apply_ignore_globs(paths, ["*"])))
    assert scored.overall_score == 0
    assert scored.indexed is False


def test_the_score_is_bounded_and_the_levels_follow_it() -> None:
    engine = ReadinessEngine()
    everything = IndexEvidence(
        paths=(
            "dockerfile",
            ".dockerignore",
            ".github/workflows/ci.yml",
            "tests/test_x.py",
            ".pre-commit-config.yaml",
            "k8s/deployment.yaml",
            "chart/chart.yaml",
            "docker-compose.yml",
            ".env.example",
            "config/settings.py",
            "security.md",
            ".gitleaks.toml",
            "go.sum",
            "infra/main.tf",
            "infra/.terraform.lock.hcl",
        ),
        contents={
            "dockerfile": MULTI_STAGE,
            "k8s/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\n",
            "infra/main.tf": 'terraform {\n  backend "s3" {}\n}\n',
        },
    )
    best = engine.evaluate(everything)
    assert best.overall_score == 100
    assert best.level == "production_ready"
    assert best.recommendations == []

    middling = engine.evaluate(
        IndexEvidence(
            paths=("dockerfile", ".dockerignore", ".github/workflows/ci.yml", "tests/test_x.py", ".env.example"),
            contents={"dockerfile": MULTI_STAGE},
        )
    )
    assert 0 < middling.overall_score < 100
    assert middling.level in {"blocked", "needs_improvement"}


def test_the_legacy_dict_entry_point_reads_its_keys_as_paths() -> None:
    """`evaluate_project` is retained, and its meaning changed: the strings are indexed
    paths, not configuration keys. That is what stops `sorted(settings.keys())` from ever
    scoring again."""
    result = ReadinessEngine().evaluate_project(
        {"manifests": ["Dockerfile"], "config_files": [".github/workflows/ci.yml"], "has_tests": False}
    )
    assert _check(result, "dockerfile_present").passed is True
    assert _check(result, "ci_pipeline_present").passed is True
    assert _check(result, "automated_tests_present").passed is False
    # A settings-shaped blob has no repository evidence in it. It cannot reach a level above
    # `blocked`: the only checks two arbitrary strings can satisfy are the two absence ones,
    # which is 8 of 100 — where the old engine gave the same blob 20 for documentation and 25
    # for tests it never looked for.
    configured = ReadinessEngine().evaluate_project({"config_files": ["favourite", "embedding_backend"]})
    assert configured.overall_score < 20, configured.breakdown
    assert configured.level == "blocked"
    assert configured.breakdown.containerization_score == 0
    assert configured.breakdown.ci_config_score == 0
