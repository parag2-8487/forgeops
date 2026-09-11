# SPDX-License-Identifier: FSL-1.1-ALv2
"""Invariant 3 and the acceptance case: predicted equals achieved, and 90 becomes 100.

WHAT THESE PIN

The product commits to a number before generation runs - "this is the score a run can reach" - and the
value of that commitment is entirely that being wrong about it is visible. So the prediction is compared
against what the real renderer achieves, and a gap fails the build instead of being reported.

The acceptance case is the end-to-end claim in its smallest honest form: a project reading exactly 90, a
generation run, and a rescan reading exactly 100. Both numbers come from `GenerationService._render`
(the real template path) and `ReadinessEngine` (the real scorer), so neither is asserted against a
fixture of itself.

WHY THE FIXTURE HAS THE SHAPE IT HAS

To reach 100 the project must already hold what generation legitimately cannot produce, and the
reachable-score contract names those refusals: tests that encode intent, configuration reads gathered
into one module, a lockfile resolved against a live registry, a provider lock from a real `tofu init`,
and no credential baked into an image layer. Each is a refusal this platform makes on purpose.

So the fixture SUPPLIES those and withholds two things generation does produce - the Kubernetes
manifests and the compose file, which is what a project deploying by Helm and never locally tends to
look like. That is not stacking the deck: a fixture missing something generation cannot make would
assert the platform does the impossible, and a fixture missing nothing would assert nothing at all.

`dependency_specifiers` is supplied because the two dependency-reconciliation checks read the scan's
import graph, not the file bodies. Without it `flask` appears declared and unimported, and the ceiling
is 99 for a reason that has nothing to do with generation.
"""

from __future__ import annotations

from src.core.index_evidence import IndexEvidence
from src.core.reachable_score import PredictionOutcome, reachable_score
from src.core.readiness import ReadinessEngine
from src.generation.service import GenerationService

APPLICATION = """from flask import Flask

from config.settings import APP_NAME


def create_app():
    return Flask(APP_NAME)
"""

# Every environment read in the project happens here. `centralised_configuration` measures that, and a
# module holding no reads would not satisfy it.
CENTRALISED_CONFIG = """import os

APP_NAME = os.getenv("APP_NAME", "demo")
PORT = int(os.getenv("PORT", "8000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info")
DATABASE_URL = os.environ["DATABASE_URL"]
"""

# Two cases, each asserting on something the application decides. `automated_tests_present` refuses an
# empty file and refuses `assert True`.
REAL_TESTS = """from app import create_app
from config.settings import PORT


def test_the_app_boots_with_its_real_configuration():
    assert create_app() is not None


def test_the_configured_port_is_usable():
    assert 1 <= PORT <= 65535
"""

# A lockfile is the OUTPUT of a resolver against a live registry, which is why generation refuses to
# write one. The fixture carries the resolver's output.
DEPENDENCY_MANIFEST = "flask==3.0.3\n"
LOCKFILE = """flask==3.0.3 \\
    --hash=sha256:34e815dfaa43340d1d15a5c3a02b1ccb2d4a2b2a2b0e1f3e0b3d5c9f7a8b6c5d
"""
PROVIDER_LOCK = """provider "registry.opentofu.org/hashicorp/kubernetes" {
  version     = "2.31.0"
  constraints = "~> 2.30"
}
"""

#: `(importing file, specifier, resolved)` as an agent scan records it. `resolved=True` means the import
#: pointed at another file in this repository and is therefore not a dependency at all.
DEPENDENCY_SPECIFIERS = (
    ("app.py", "flask", False),
    ("app.py", "config.settings", True),
    ("tests/test_app.py", "app", True),
    ("tests/test_app.py", "config.settings", True),
)


def _render(name: str = "demo") -> dict[str, str]:
    """The real template path, called unbound because `_render` reads no instance state."""
    return {
        item.path: item.content
        for item in GenerationService._render(None, "make it deployable", {"name": name, "settings": {}})
    }


def _evidence(files: dict[str, str]) -> IndexEvidence:
    return IndexEvidence(
        paths=tuple(sorted(files)),
        contents={path.lower(): body for path, body in files.items()},
        dependency_specifiers=DEPENDENCY_SPECIFIERS,
    )


def _evaluate(files: dict[str, str]):
    return ReadinessEngine().evaluate(_evidence(files))


def _score(files: dict[str, str]) -> int:
    return _evaluate(files).overall_score


def _project_own_work() -> dict[str, str]:
    """What the project brings: the five things generation does not produce."""
    return {
        "app.py": APPLICATION,
        "config/settings.py": CENTRALISED_CONFIG,
        "tests/test_app.py": REAL_TESTS,
        "requirements.txt": DEPENDENCY_MANIFEST,
        "requirements.lock": LOCKFILE,
        "infra/.terraform.lock.hcl": PROVIDER_LOCK,
    }


#: The artifact kinds the demo project is missing. Restoring them is exactly what a generation run does.
_MISSING_PREFIXES = ("k8s/",)
_MISSING_EXACT = ("docker-compose.yml",)


def demo_project_before() -> dict[str, str]:
    """The demo project as an operator would find it: 90, with Kubernetes and compose absent."""
    files = _project_own_work()
    for path, body in _render().items():
        if path.startswith(_MISSING_PREFIXES) or path in _MISSING_EXACT:
            continue
        files[path] = body
    return files


def demo_project_after() -> dict[str, str]:
    """The same project once a generation run has produced what was missing."""
    files = demo_project_before()
    files.update(_render())
    return files


class TestInvariantThree:
    def test_the_prediction_is_met_exactly_by_a_real_render(self) -> None:
        """A gap between predicted and achieved is a defect, so it fails here rather than being reported."""
        before = demo_project_before()
        prediction = reachable_score(_evaluate(before))

        achieved = _score(demo_project_after())

        outcome = PredictionOutcome(predicted=prediction.reachable, achieved=achieved)
        assert outcome.matched, outcome.describe()

    def test_the_prediction_names_a_reason_for_every_point_it_cannot_reach(self) -> None:
        """ "Generation cannot raise this score" is not something an operator can act on."""
        prediction = reachable_score(_evaluate({"main.py": "print('hi')\n"}))

        assert prediction.unreachable, "a bare repository has checks generation cannot fix"
        for item in prediction.unreachable:
            assert item.points_short > 0
            assert len(item.reason) > 40, f"{item.check_id} has no usable reason: {item.reason!r}"
            # The reason must be about the project or the environment, never about this platform's
            # effort or its roadmap.
            lowered = item.reason.lower()
            for forbidden in ("not implemented", "not supported", "todo", "coming soon"):
                assert forbidden not in lowered, f"{item.check_id} blames effort: {item.reason!r}"

    def test_a_project_with_nothing_left_to_generate_predicts_no_gain(self) -> None:
        prediction = reachable_score(_evaluate(demo_project_after()))

        assert prediction.gain == 0, f"nothing left to generate, yet it predicts +{prediction.gain}"
        assert prediction.fixable == ()


class TestTheAcceptanceCase:
    """The demo project reads exactly 90 before and exactly 100 after."""

    def test_the_project_scores_exactly_ninety_before_generation(self) -> None:
        assert _score(demo_project_before()) == 90

    def test_it_scores_exactly_one_hundred_after_a_generation_run(self) -> None:
        assert _score(demo_project_after()) == 100

    def test_the_prediction_made_before_the_run_was_one_hundred(self) -> None:
        """The contract and the acceptance case must agree, or one of them is decorative."""
        prediction = reachable_score(_evaluate(demo_project_before()))

        assert prediction.current == 90
        assert prediction.reachable == 100

    def test_every_check_passes_afterwards_so_the_hundred_is_earned(self) -> None:
        """100 has to be earned check by check, not arrived at by a weighting quirk."""
        result = _evaluate(demo_project_after())

        short = [f"{c.id} {c.points}/{c.max_points}" for c in result.checks if c.points < c.max_points]
        assert short == [], f"score reads 100 while checks are short: {short}"

    def test_the_gain_comes_from_the_artifacts_that_were_missing(self) -> None:
        """The two absent kinds are what moved, so the test cannot pass for an unrelated reason.

        `kubernetes_manifests_present` is deliberately NOT asserted here: the Helm chart templates the
        project already ships are manifests, so presence was satisfied before. What the absent `k8s/`
        directory cost was the CONTENT checks - probes, limits, pinned tags - which is the more useful
        thing to pin anyway, since those are the checks a generated manifest has to earn.
        """
        was = {c.id: c for c in _evaluate(demo_project_before()).checks}
        now = {c.id: c for c in _evaluate(demo_project_after()).checks}

        improved = {cid for cid, check in now.items() if cid in was and check.points > was[cid].points}
        assert "compose_file_present" in improved
        assert "kubernetes_probes_declared" in improved
        assert "kubernetes_resource_limits_declared" in improved
        assert "kubernetes_image_tags_pinned" in improved
