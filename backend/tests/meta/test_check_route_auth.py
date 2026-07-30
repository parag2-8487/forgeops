# SPDX-License-Identifier: FSL-1.1-ALv2
"""`scripts/check-route-auth.py` detects what it claims to detect.

design.md §4.4, §11.2; Q-19; tasks.md leaf 6.1.

A checker that has never failed is a checker nobody has tested. Four cases:

* an unprotected non-public route is reported, naming the method and path;
* the same surface with the dependency attached passes;
* an EMPTY router fails — because a check that passes on an empty inventory reports
  success for a build that composed no routes, which is how a vacuous gate is born;
* a `PUBLIC_ROUTES` entry the router does not serve is reported, because a stale
  exemption applies to whatever takes the path next.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-route-auth.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("forgeops_check_route_auth", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECKER = _load()

FIXTURES = "tests.meta.fixtures.route_auth_apps"


class TestTheNegativeFixtureIsDetected:
    def test_an_unprotected_route_is_reported(self) -> None:
        failures = CHECKER.check(f"{FIXTURES}:bad_app")
        assert any("/api/v1/projects" in failure for failure in failures), failures

    def test_the_report_names_the_method(self) -> None:
        """A path alone is not actionable when only one method is unprotected."""
        failures = CHECKER.check(f"{FIXTURES}:bad_app")
        assert any("'GET'" in failure for failure in failures), failures

    def test_the_report_says_how_to_fix_it(self) -> None:
        failures = CHECKER.check(f"{FIXTURES}:bad_app")
        joined = " ".join(failures)
        assert "public_routes.py" in joined, joined


class TestThePositiveFixturePasses:
    def test_a_protected_route_is_not_reported(self) -> None:
        failures = CHECKER.check(f"{FIXTURES}:good_app")
        assert not any("/api/v1/projects" in failure for failure in failures), failures

    def test_require_role_counts_as_protection(self) -> None:
        """`require_role` returns a closure, so its own name is
        `require_role.<locals>.dependency` — a name that only exists when the factory
        was used. The DELETE route in the fixture carries only that."""
        failures = CHECKER.check(f"{FIXTURES}:good_app")
        assert not any("/api/v1/projects/{project_id}" in failure for failure in failures), failures


class TestAnEmptyRouterFails:
    def test_no_routes_is_a_failure_not_a_pass(self) -> None:
        failures = CHECKER.check(f"{FIXTURES}:empty_app")
        assert any("no routes were examined" in failure for failure in failures), failures


class TestAStaleAllowlistEntryIsReported:
    def test_a_public_path_the_router_does_not_serve_is_reported(self) -> None:
        """`/api/v1/health` carries no `arrives_in` marker, so a router that does not
        serve it has a genuinely stale exemption."""
        failures = CHECKER.check(f"{FIXTURES}:stale_allowlist_app")
        assert any("/api/v1/health" in failure for failure in failures), failures

    def test_a_marked_entry_is_staged_rather_than_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A marked entry is reported as staged; an unmarked one is a failure.

        Both cases are driven from a SYNTHETIC `PUBLIC_ROUTES`, not from whichever
        production entry happens to be unserved today. The earlier version of this test
        used `/api/v1/auth/login` as its example, which broke the moment task 6.2 served
        that route and removed its marker — the test was asserting the mechanism through
        a fact that was designed to change. Installing a two-entry allowlist keeps it
        asserting the mechanism itself, and keeps it non-vacuous once every production
        marker is gone.
        """
        from src.auth import public_routes as public_routes_module

        staged_path = "/api/v1/not-served-yet/staged"
        stale_path = "/api/v1/not-served-yet/stale"
        synthetic = (
            public_routes_module.PublicRoute(
                staged_path, frozenset({"GET"}), "arrives later", arrives_in="a later task"
            ),
            public_routes_module.PublicRoute(stale_path, frozenset({"GET"}), "no marker"),
        )
        monkeypatch.setattr(public_routes_module, "PUBLIC_ROUTES", synthetic)
        monkeypatch.setattr(
            public_routes_module,
            "STAGED_PATHS",
            frozenset({staged_path}),
        )

        failures = CHECKER.check(f"{FIXTURES}:stale_allowlist_app")

        assert not any(staged_path in failure for failure in failures), failures
        assert any(stale_path in failure for failure in failures), failures

    def test_a_marked_entry_whose_route_is_served_fails_until_the_marker_goes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The marker is self-clearing, which is what stops it becoming permanent.

        The fixture app serves `/api/v1/projects`; marking it `arrives_in` must
        therefore be reported, so a stale marker cannot outlive the route's arrival.
        """
        from src.auth import public_routes as public_routes_module

        synthetic = (
            public_routes_module.PublicRoute(
                "/api/v1/projects",
                frozenset({"GET"}),
                "served already",
                arrives_in="a task that has already landed",
            ),
        )
        monkeypatch.setattr(public_routes_module, "PUBLIC_ROUTES", synthetic)
        monkeypatch.setattr(public_routes_module, "STAGED_PATHS", frozenset())

        failures = CHECKER.check(f"{FIXTURES}:stale_allowlist_app")
        assert any("Remove the" in failure and "arrives_in" in failure for failure in failures), failures

    def test_the_report_explains_why_it_matters(self) -> None:
        failures = CHECKER.check(f"{FIXTURES}:stale_allowlist_app")
        joined = " ".join(failures)
        assert "takes the path next" in joined, joined


class TestABuildFailureIsExitTwo:
    def test_an_unimportable_app_is_not_a_pass(self) -> None:
        """Reported through `main()`, because the exit code is the contract."""
        argv = sys.argv
        sys.argv = ["check-route-auth.py", "--app", "src.does_not_exist:create_app"]
        try:
            assert CHECKER.main() == 2
        finally:
            sys.argv = argv

    def test_a_malformed_spec_is_not_a_pass(self) -> None:
        argv = sys.argv
        sys.argv = ["check-route-auth.py", "--app", "src.main"]
        try:
            assert CHECKER.main() == 2
        finally:
            sys.argv = argv
