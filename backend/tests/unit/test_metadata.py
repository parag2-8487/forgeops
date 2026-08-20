# SPDX-License-Identifier: FSL-1.1-ALv2
"""Task 2.1 — backend/pyproject.toml metadata checks.

These tests FAIL on:
- a non-exact (non ==) direct pin
- wrong Python constraint (must be >=3.13,<3.14)
- disallowed dependency (sse-starlette, celery, opentelemetry-*, structlog, langchain)
- wrong licence expression (must be FSL-1.1-ALv2)
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _load_pyproject() -> dict:
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)


class TestPyprojectMetadata:
    """Validate the exact backend package metadata as defined by design.md §16.2."""

    def test_requires_python_constraint(self):
        """requires-python must be >=3.13,<3.14 — never 3.13.* or other variants."""
        data = _load_pyproject()
        requires_python = data["project"]["requires-python"]
        assert requires_python == ">=3.13,<3.14", (
            f"Wrong python constraint: got {requires_python!r}, expected '>=3.13,<3.14'"
        )

    def test_license_expression(self):
        """License must be the exact registered SPDX identifier FSL-1.1-ALv2."""
        data = _load_pyproject()
        license_val = data["project"]["license"]
        assert license_val == "FSL-1.1-ALv2", f"Wrong licence expression: got {license_val!r}, expected 'FSL-1.1-ALv2'"

    def test_all_direct_pins_are_exact(self):
        """Every direct dependency in [project.dependencies] must use == pins."""
        data = _load_pyproject()
        deps = data["project"]["dependencies"]
        non_exact = []
        for dep in deps:
            # A valid exact pin has == somewhere
            if "==" not in dep:
                non_exact.append(dep)
        assert non_exact == [], f"Non-exact direct pins found: {non_exact}. All must use == pinning."

    def test_dev_dependencies_are_exact(self):
        """Every dev extra dependency must use == pins."""
        data = _load_pyproject()
        dev_deps = data["project"]["optional-dependencies"]["dev"]
        non_exact = []
        for dep in dev_deps:
            if "==" not in dep:
                non_exact.append(dep)
        assert non_exact == [], f"Non-exact dev pins found: {non_exact}. All must use == pinning."

    def test_disallowed_dependency_sse_starlette(self):
        """sse-starlette must never appear — FastAPI native EventSourceResponse."""
        data = _load_pyproject()
        all_deps = data["project"]["dependencies"]
        all_deps += data["project"]["optional-dependencies"].get("dev", [])
        for dep in all_deps:
            assert "sse-starlette" not in dep.lower(), (
                f"Disallowed dependency found: {dep}. "
                "Research §0: use FastAPI native EventSourceResponse, never sse-starlette."
            )

    def test_disallowed_dependency_celery(self):
        """celery must never appear — permanently banned by Research §0."""
        data = _load_pyproject()
        all_deps = data["project"]["dependencies"]
        all_deps += data["project"]["optional-dependencies"].get("dev", [])
        for dep in all_deps:
            assert "celery" not in dep.lower(), f"Disallowed dependency found: {dep}. Celery is permanently banned."

    def test_disallowed_dependency_opentelemetry(self):
        """opentelemetry-* must not appear — OTel SDK is Phase 3."""
        data = _load_pyproject()
        all_deps = data["project"]["dependencies"]
        all_deps += data["project"]["optional-dependencies"].get("dev", [])
        for dep in all_deps:
            assert not dep.lower().startswith("opentelemetry"), (
                f"Disallowed dependency found: {dep}. OTel SDK is Phase 3."
            )

    def test_disallowed_dependency_structlog(self):
        """structlog must not appear — stdlib logging is the choice (OQ-3)."""
        data = _load_pyproject()
        all_deps = data["project"]["dependencies"]
        all_deps += data["project"]["optional-dependencies"].get("dev", [])
        for dep in all_deps:
            assert "structlog" not in dep.lower(), f"Disallowed dependency found: {dep}. Use stdlib logging (OQ-3)."

    def test_disallowed_dependency_langchain(self):
        """langchain must not appear — Phase 1."""
        data = _load_pyproject()
        all_deps = data["project"]["dependencies"]
        all_deps += data["project"]["optional-dependencies"].get("dev", [])
        for dep in all_deps:
            assert "langchain" not in dep.lower(), f"Disallowed dependency found: {dep}. LangChain is Phase 1."

    def test_pip_tools_version(self):
        """pip-tools must be exactly 7.6.0 in the dev dependencies."""
        data = _load_pyproject()
        dev_deps = data["project"]["optional-dependencies"]["dev"]
        found = False
        for dep in dev_deps:
            if dep.startswith("pip-tools"):
                assert dep == "pip-tools==7.6.0", f"pip-tools version must be ==7.6.0, got: {dep}"
                found = True
                break
        assert found, "pip-tools==7.6.0 must be in the dev dependencies"

    def test_ruff_config_present(self):
        """Backend-scoped ruff config must be present."""
        data = _load_pyproject()
        assert "ruff" in data.get("tool", {}), "Ruff config must be in [tool.ruff]"
        assert data["tool"]["ruff"]["target-version"] == "py313"

    def test_pytest_asyncio_mode(self):
        """pytest-asyncio must be configured in auto mode."""
        data = _load_pyproject()
        pytest_cfg = data["tool"]["pytest"]["ini_options"]
        assert pytest_cfg["asyncio_mode"] == "auto"

    def test_coverage_is_a_gate(self):
        """Coverage is a gate at 70 — `fail_under` must be set in the coverage config.

        This test previously asserted the OPPOSITE. It was `test_coverage_not_a_gate` and it
        asserted `fail_under` was ABSENT, with the message "Coverage must be a GOAL not a gate
        in Phase 0". That was correct for Phase 0 and became an active guard against Phase 1's
        own requirement: criterion 11 makes coverage a gate, and this test passed on every run
        by proving it was not one. Three documents claimed the gate while this asserted its
        absence, and nothing compared them (LEARNING-JOURNAL finding 81).

        The lesson is in the shape rather than the value: a test of the form
        `assert X not in config` encodes an intent, and intents expire at phase boundaries.
        Asserting the threshold's VALUE rather than merely its presence is what stops the gate
        being quietly weakened to whatever the code currently achieves.
        """
        data = _load_pyproject()
        coverage_report = data.get("tool", {}).get("coverage", {}).get("report", {})
        assert "fail_under" in coverage_report, (
            "Coverage must be a GATE in Phase 1 (design.md §7.13, D-31, criterion 11): "
            "`fail_under` is missing from [tool.coverage.report]"
        )
        assert coverage_report["fail_under"] == 70, (
            f"the coverage gate must be 70, not {coverage_report['fail_under']} — a threshold "
            "tuned down to what the code currently reaches is not a gate"
        )

        pytest_addopts = data["tool"]["pytest"]["ini_options"]["addopts"]
        assert "--cov-fail-under=70" in pytest_addopts, (
            "`--cov-fail-under=70` must be in the backend addopts so the gate applies to every "
            "pytest run and not only to the CI entry point"
        )

    def test_banned_imports_cross_domain(self):
        """Ruff must have banned-api rules for cross-domain imports."""
        data = _load_pyproject()
        banned = data["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"]
        # Must ban at least some domain-crossing imports
        assert "src.projects" in banned
        assert "src.analysis" in banned
        assert "src.ai" in banned
        assert "src.mcp" in banned

    def test_banned_imports_queue_engines(self):
        """Ruff must ban celery/arq/dramatiq/temporalio/inngest outside tasks.py."""
        data = _load_pyproject()
        banned = data["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"]
        for engine in ["celery", "arq", "dramatiq", "temporalio", "inngest"]:
            assert engine in banned, f"Queue engine {engine!r} must be in banned-api"

    def test_tasks_file_exempted_from_ban(self):
        """src/core/tasks.py must be exempted from the queue-engine ban."""
        data = _load_pyproject()
        per_file = data["tool"]["ruff"]["lint"]["per-file-ignores"]
        assert "src/core/tasks.py" in per_file
        assert "TID251" in per_file["src/core/tasks.py"]
