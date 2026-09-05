# SPDX-License-Identifier: FSL-1.1-ALv2
"""No readiness failure may be unexplained, and no check may exist without an explanation.

This is what the user saw:

    Fail iac_remote_state_configured
    0 of 25 points
    Evidence:
    Why it matters: Local state cannot be shared or locked, so two applies can silently overwrite
    each other.

The empty `Evidence:` was systematic, not a single oversight: twenty-nine call sites passed
`evidence = <path> if passed else ""`, blanking it on the one outcome that needs explaining. "Why it
matters" states a principle and says nothing about the repository being scored, so a reader who did
not already know the answer was told they had a problem and given no way to find or fix it.

These tests make that state unreachable rather than merely fixed.
"""

from __future__ import annotations

import pytest
from src.core.manifest_facts import ItemAudit
from src.core.readiness import IndexEvidence, ReadinessEngine, _proportional
from src.core.readiness_findings import (
    CHECK_EXPLANATIONS,
    GENERATED_ARTIFACT_KINDS,
    KNOWN_ARTIFACT_KINDS,
)

pytestmark = [pytest.mark.mandatory]


#: Deliberately hostile evidence: a repository with a little of everything and almost nothing correct,
#: so most checks fail and every failure has to explain itself.
HALF_CORRECT = IndexEvidence(
    paths=(
        "dockerfile",
        ".github/workflows/ci.yml",
        "k8s/deployment.yaml",
        "terraform/main.tf",
        ".env",
        "id_rsa",
    ),
    contents={
        "dockerfile": 'FROM python:3\nCOPY . .\nCMD ["python", "app.py"]\n',
        ".github/workflows/ci.yml": (
            "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
        ),
        "k8s/deployment.yaml": (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n"
            "    spec:\n      containers:\n        - name: web\n          image: web:latest\n"
        ),
        "terraform/main.tf": 'resource "aws_s3_bucket" "b" {}\n',
    },
)


#: A repository where every category is internally plausible and the categories disagree with each
#: other. Each of the three cross-checks is comparable here, and each fails.
CROSS_CATEGORY_MISMATCH = IndexEvidence(
    paths=(
        "dockerfile",
        ".github/workflows/ci.yml",
        "k8s/deployment.yaml",
        "docker-compose.yml",
        ".env.example",
    ),
    contents={
        "dockerfile": 'FROM python:3.12-slim\nCMD ["python", "app.py"]\n',
        # Builds a Dockerfile at a path that is not in the repository.
        ".github/workflows/ci.yml": (
            "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: docker build -f docker/api.Dockerfile -t local/api .\n"
        ),
        # Deploys an image no workflow mentions, and passes a variable the example omits.
        "k8s/deployment.yaml": (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n"
            "    spec:\n      containers:\n        - name: web\n"
            "          image: ghcr.io/someone-else/unrelated:1.4\n"
            "          env:\n            - name: DATABASE_URL\n            - name: REDIS_URL\n"
        ),
        "docker-compose.yml": "services:\n  web:\n    environment:\n      - DATABASE_URL\n",
        ".env.example": "DATABASE_URL=\n",
    },
)


def _all_checks() -> list:
    return ReadinessEngine().evaluate(HALF_CORRECT).checks


def test_every_check_id_has_an_explanation() -> None:
    """A new check cannot ship without the text that makes its failure actionable.

    `_check` raises on a missing entry, so this fails loudly at evaluation rather than producing a
    silently blank explanation — but asserting it here names the offending id instead of surfacing a
    KeyError from inside an unrelated test.
    """
    ids = {check.id for check in _all_checks()}
    missing = sorted(ids - set(CHECK_EXPLANATIONS))
    assert not missing, f"these checks have no CHECK_EXPLANATIONS entry: {missing}"

    # The reverse direction needs MORE THAN ONE repository. The cross-category checks are emitted only
    # when both sides exist to compare - a repository with no workflow has no build to reconcile against
    # a Dockerfile - so a single fixture cannot show that every explanation is reachable. Unioning the
    # ids across fixtures that between them make each comparison possible is the honest form of the
    # assertion, and it still catches an explanation for a check nothing emits at all.
    reachable = set(ids)
    reachable |= {c.id for c in ReadinessEngine().evaluate(CROSS_CATEGORY_MISMATCH).checks}
    unused = sorted(set(CHECK_EXPLANATIONS) - reachable)
    assert not unused, (
        f"these explanations describe checks nothing emits: {unused}. An explanation for a check that "
        "never runs is the same dead-code hazard as a check with no explanation."
    )


def test_no_failing_check_is_unexplained() -> None:
    """THE REGRESSION TEST FOR THE EMPTY `Evidence:`.

    Every failing check must answer all four questions. Asserted per field rather than as one truthy
    check so a gap names which of the four is missing.
    """
    for check in _all_checks():
        if check.passed:
            continue
        assert check.looked_for, f"{check.id} does not say what it looked for"
        assert check.looked_in, f"{check.id} does not say where it looked"
        assert check.found, f"{check.id} does not say what it found instead"
        assert check.remedy, f"{check.id} offers no concrete change"
        assert check.why_it_matters, f"{check.id} does not say why it matters"


def test_a_failure_names_a_file_to_change() -> None:
    """A remedy with nowhere to put it is advice, not a fix."""
    for check in _all_checks():
        if not check.passed:
            assert check.remedy_path, f"{check.id} does not name the file its remedy belongs in"


def test_no_failure_reports_the_generic_fallback_when_it_could_be_specific() -> None:
    """The fallback exists for honesty, not as a resting place.

    A check that examined a real file must name that file. Only checks whose failure is a pure absence
    are allowed the generic sentence, and even then it states where it looked.
    """
    examined_something = {
        "dockerfile_multi_stage",
        "dockerfile_non_root",
        "dockerfile_base_pinned",
        "dockerfile_healthcheck_present",
        "kubernetes_resource_limits_declared",
        "kubernetes_probes_declared",
        "kubernetes_image_tags_pinned",
        "iac_remote_state_configured",
        "no_committed_env_file",
        "no_committed_key_material",
    }
    for check in _all_checks():
        if check.passed or check.id not in examined_something:
            continue
        assert "nothing matched in any of the locations searched" not in check.found, (
            f"{check.id} examined a file that exists in this fixture and still reported the generic "
            "fallback; the finding must name what it saw"
        )


def test_every_passing_check_reports_no_finding() -> None:
    """`found` means "what was there instead", so a pass must leave it empty.

    Filling it on a pass would read as a complaint about the file that satisfied the check.
    """
    for check in _all_checks():
        if check.passed:
            assert check.found == "", f"{check.id} passed and still reported a finding: {check.found!r}"
            assert check.remedy == "", f"{check.id} passed and still offered a remedy"


def test_a_line_is_reported_only_when_it_is_knowable() -> None:
    """An invented line number sends a reader confidently to the wrong place.

    A check whose failure is an absence has no line, and must say `None` rather than 0 or 1.
    """
    absences = {
        "dockerfile_present",
        "dockerignore_present",
        "ci_pipeline_present",
        "helm_chart_present",
        "compose_file_present",
        "env_example_present",
        "iac_sources_present",
        "iac_provider_lock_present",
    }
    for check in _all_checks():
        if check.id in absences:
            assert check.line is None, f"{check.id} reported line {check.line} for a missing file"
        if check.line is not None:
            assert check.line >= 1, f"{check.id} reported a non-positive line {check.line}"


class TestPartialScoring:
    """A half-correct file must score between zero and full."""

    def test_a_repository_with_one_bad_container_of_two_scores_between(self) -> None:
        evidence = IndexEvidence(
            paths=("k8s/deployment.yaml",),
            contents={
                "k8s/deployment.yaml": (
                    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n"
                    "  template:\n    spec:\n      containers:\n"
                    "        - name: good\n          image: reg/app@sha256:aaa\n"
                    "          resources:\n            requests: { cpu: 100m, memory: 128Mi }\n"
                    "            limits: { cpu: 500m, memory: 512Mi }\n"
                    "          livenessProbe: { httpGet: { path: /h, port: 8080 } }\n"
                    "          readinessProbe: { httpGet: { path: /r, port: 8080 } }\n"
                    "        - name: bad\n          image: app:latest\n"
                )
            },
        )
        checks = {c.id: c for c in ReadinessEngine().evaluate(evidence).checks}
        for check_id in (
            "kubernetes_resource_limits_declared",
            "kubernetes_probes_declared",
            "kubernetes_image_tags_pinned",
        ):
            check = checks[check_id]
            assert 0 < check.points < check.max_points, (
                f"{check_id} scored {check.points}/{check.max_points}; one correct container of two "
                "must score between nothing and everything, or fixing half the work moves nothing"
            )
            # And the reader is told which container and which line.
            assert "bad" in check.found
            assert check.line is not None

    def test_a_full_score_needs_every_item(self) -> None:
        """Rounding must never reach the maximum while an item is still wrong."""
        audit = ItemAudit(examined=100, satisfied=99)
        assert _proportional(audit, 35) < 35
        assert _proportional(ItemAudit(examined=100, satisfied=100), 35) == 35

    def test_nothing_examined_earns_nothing(self) -> None:
        """A vacuous pass would score a repository for work it has not done."""
        assert _proportional(ItemAudit(examined=0, satisfied=0), 35) == 0


class TestCrossCategoryChecks:
    """Six internally correct categories can still describe six different pieces of software."""

    def test_three_real_inconsistencies_are_detected_on_one_repository(self) -> None:
        """Each individual artifact here reviews cleanly; no two of them agree.

        These are the failures most likely to reach production, precisely because a category-at-a-time
        score cannot see them.
        """
        checks = {c.id: c for c in ReadinessEngine().evaluate(CROSS_CATEGORY_MISMATCH).checks}
        for check_id in (
            "ci_builds_an_existing_dockerfile",
            "kubernetes_images_are_built_here",
            "env_example_matches_the_deployment",
        ):
            assert check_id in checks, f"{check_id} was not emitted for a repository it can judge"
            check = checks[check_id]
            assert not check.passed, f"{check_id} did not detect the inconsistency"
            # Each must name both sides, or a reader cannot tell which file to change.
            assert check.found, f"{check_id} reported no finding"
            assert check.remedy_path, f"{check_id} named no file to change"

    def test_the_build_mismatch_names_the_path_the_workflow_asked_for(self) -> None:
        checks = {c.id: c for c in ReadinessEngine().evaluate(CROSS_CATEGORY_MISMATCH).checks}
        found = checks["ci_builds_an_existing_dockerfile"].found
        # Naming the root Dockerfile would be wrong: it exists, and it is not what the build asked for.
        assert "docker/api.Dockerfile" in found

    def test_the_image_mismatch_names_the_image_nothing_pushes(self) -> None:
        checks = {c.id: c for c in ReadinessEngine().evaluate(CROSS_CATEGORY_MISMATCH).checks}
        assert "ghcr.io/someone-else/unrelated" in checks["kubernetes_images_are_built_here"].found

    def test_the_env_mismatch_names_the_missing_variable(self) -> None:
        checks = {c.id: c for c in ReadinessEngine().evaluate(CROSS_CATEGORY_MISMATCH).checks}
        assert "REDIS_URL" in checks["env_example_matches_the_deployment"].found

    def test_nothing_to_compare_is_not_a_failure(self) -> None:
        """A repository with no workflow has no build to reconcile, and must not be marked wrong for it.

        Scoring an absent artifact as a consistency fault would report it twice: once honestly, by the
        check that looks for it, and once as a disagreement that does not exist.
        """
        bare = IndexEvidence(paths=("dockerfile",), contents={"dockerfile": "FROM scratch\n"})
        ids = {c.id for c in ReadinessEngine().evaluate(bare).checks}
        assert "ci_builds_an_existing_dockerfile" not in ids
        assert "kubernetes_images_are_built_here" not in ids
        assert "env_example_matches_the_deployment" not in ids


class TestGeneratabilityIsDerivedNotAsserted:
    """The screen must not promise a file the generator cannot write.

    The blanket message said "Generation cannot raise this score", which was wrong for most checks. The
    obvious correction - a hand-maintained `generatable` flag - would have been wrong in the other
    direction the moment it claimed a CI workflow the generator does not emit. So the claim is DERIVED
    from the artifact kinds the generator actually produces.
    """

    def test_a_check_is_generatable_only_if_its_artifact_is_emitted(self) -> None:
        for check_id, explanation in CHECK_EXPLANATIONS.items():
            if explanation.generatable_today:
                assert explanation.artifact in GENERATED_ARTIFACT_KINDS, (
                    f"{check_id} claims to be generatable today, but its artifact "
                    f"{explanation.artifact!r} is not one the generator emits"
                )
                assert explanation.fixability.generatable, (
                    f"{check_id} claims to be generatable today while its own table says the fix is not "
                    "a file that can be written"
                )

    def test_every_artifact_kind_is_one_of_the_known_ones(self) -> None:
        """A typo would silently make a check unfixable rather than failing."""
        for check_id, explanation in CHECK_EXPLANATIONS.items():
            if explanation.artifact:
                assert explanation.artifact in KNOWN_ARTIFACT_KINDS, (
                    f"{check_id} names artifact kind {explanation.artifact!r}, which is not in "
                    "KNOWN_ARTIFACT_KINDS - a typo here reads as 'nothing can fix this'"
                )

    def test_the_validated_kinds_match_the_checker_dispatch(self) -> None:
        """The kinds that claim an executable validator must be ones `checker_for` really dispatches.

        THIS REPLACES A TEST TIED TO `generation/schemas.py`, which turned out to be dead on the runtime
        path: `DockerfileArtifact` and `KubernetesManifestArtifact` are referenced only by `renderers.py`
        and one unit test. Deriving the emittable set from them understated the generator by nine kinds,
        because the real pipeline returns `{path: content}` for ANY path the model marks — the limit was
        `parse_artifacts`' hard-coded required set, not the model and not the validators.

        `artifact_checks.checker_for` is the honest source for the validated subset, so it is asserted
        against directly rather than restated here.
        """
        from src.generation.artifact_checks import checker_for

        representative = {
            "dockerfile": "Dockerfile",
            "compose": "docker-compose.yml",
            "helm": "charts/app/Chart.yaml",
            "github_workflow": ".github/workflows/ci.yml",
            "k8s": "k8s/deployment.yaml",
            "opentofu": "terraform/main.tf",
        }
        for kind, path in representative.items():
            assert checker_for(path) is not None, (
                f"{kind} is claimed to be checked by a tool, and checker_for({path!r}) returns nothing"
            )
            assert kind in GENERATED_ARTIFACT_KINDS

        # A kind with no executable validator is still emittable, and must not claim one. The readiness
        # checks are its whole criterion, which the prompt states rather than implying a tool exists.
        assert checker_for(".env.example") is None
        assert "env_example" in GENERATED_ARTIFACT_KINDS

    def test_a_generator_gap_would_be_named_as_a_generator_gap(self) -> None:
        """The derivation, exercised directly rather than through a check that has since moved.

        Every artifact a live check names is now emittable, which is the point of the work that widened
        the set — so no current check sits in the "generatable in principle, not emitted yet" state. The
        mechanism still has to be right for the next kind somebody adds, and the two reasons for "cannot"
        must stay apart: telling a user a `.env.example` is impossible would be false, while telling them
        the generator does not write one yet is true and points at the right thing.
        """
        from src.core.readiness import _blocked_reason
        from src.core.readiness_findings import CheckExplanation, Fixability

        not_yet_emitted = CheckExplanation(
            looked_for="x",
            looked_in="y",
            remedy="z",
            remedy_path="p",
            fixability=Fixability(generatable=True),
            artifact="a_kind_the_generator_does_not_emit",
        )
        assert not not_yet_emitted.generatable_today
        reason = _blocked_reason(not_yet_emitted)
        assert "does not emit yet" in reason
        assert "gap in the generator" in reason

        never = CHECK_EXPLANATIONS["automated_tests_present"]
        assert not never.fixability.generatable
        permanent = _blocked_reason(never)
        assert "does not emit yet" not in permanent, (
            "a test cannot be generated for a reason that has nothing to do with which artifact kinds "
            "the generator emits, and saying otherwise would promise it later"
        )
