# SPDX-License-Identifier: FSL-1.1-ALv2
"""The CI-job existence check's own tests (design.md §8.3, §15.10).

Phase 0's Appendix E cited `build`, `test` and `lint` jobs that never existed. The
evidence column read like proof and named nothing real. These tests prove the check
that makes that a build failure actually fires, that its extraction picks job
citations rather than every backticked token, and that the staged-jobs baseline
cannot outlive its purpose.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-ci-jobs.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci"
DESIGN_EXCERPT = FIXTURES / "design-excerpt.md"
WORKFLOW_MISSING = FIXTURES / "workflow-missing-job.yml"
WORKFLOW_COMPLETE = FIXTURES / "workflow-complete.yml"

REAL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REAL_DESIGN = REPO_ROOT / ".antigravity" / "specs" / "phase-1-mvp-core" / "design.md"
BASELINE = REPO_ROOT / "scripts" / "ci-jobs-baseline.txt"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("forgeops_check_ci_jobs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECK = _load()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


class TestTheNegativeFixtureIsRejected:
    def test_a_workflow_missing_a_cited_job_fails(self) -> None:
        result = _run(str(WORKFLOW_MISSING), str(DESIGN_EXCERPT), "--no-baseline")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "backend" in result.stderr

    def test_the_complete_fixture_passes(self) -> None:
        result = _run(str(WORKFLOW_COMPLETE), str(DESIGN_EXCERPT), "--no-baseline")
        assert result.returncode == 0, result.stdout + result.stderr


class TestTheExtractionIsPrecise:
    def test_only_bold_backticked_tokens_count_as_jobs(self) -> None:
        """`kubectl` and **Q-99** must not be mistaken for job names."""
        cited = CHECK.cited_jobs(DESIGN_EXCERPT)
        assert cited == {"agent", "backend"}, sorted(cited)

    def test_the_scan_is_bounded_to_appendix_e(self) -> None:
        """Citations in the neighbouring appendices must not leak in."""
        cited = CHECK.cited_jobs(DESIGN_EXCERPT)
        assert "nonexistent-before" not in cited
        assert "nonexistent-after" not in cited

    def test_an_empty_extraction_is_a_failure(self, tmp_path: Path) -> None:
        """A pattern that stopped matching would otherwise pass forever."""
        design = tmp_path / "design.md"
        design.write_text(
            "## Appendix E - nothing cited here\n\nPlain prose with `code` and **bold**.\n",
            encoding="utf-8",
        )
        result = _run(str(WORKFLOW_COMPLETE), str(design), "--no-baseline")
        assert result.returncode == 1
        assert "no job citations" in result.stderr


class TestTheRealDocumentsAgree:
    def test_the_real_workflow_and_appendix_e_agree_modulo_the_baseline(self) -> None:
        result = _run(str(REAL_WORKFLOW), str(REAL_DESIGN))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_appendix_e_cites_the_expected_job_set(self) -> None:
        cited = CHECK.cited_jobs(REAL_DESIGN)
        expected = {
            "agent",
            "backend",
            "e2e",
            "frontend",
            "k8s",
            "mutation",
            "policy",
            "secrets",
            "supply",
            "templates",
        }
        assert cited == expected, f"unexpected: {sorted(cited ^ expected)}"


class TestTheBaselineCannotRot:
    def test_every_baselined_job_names_an_owning_task(self) -> None:
        baseline = CHECK.load_baseline(BASELINE)
        assert baseline, "the baseline is empty; if task 19.3 has landed, delete the file"
        for job, owner in baseline.items():
            assert "task" in owner.lower(), f"{job} does not name an owning task: {owner!r}"

    def test_a_baselined_job_that_now_exists_fails_the_check(self, tmp_path: Path) -> None:
        """The clause that forces 19.3 to empty the file."""
        baseline = tmp_path / "baseline.txt"
        baseline.write_text("agent: task 99.9 (already defined, so this must fail)\n", encoding="utf-8")
        result = _run(str(WORKFLOW_COMPLETE), str(DESIGN_EXCERPT), "--baseline", str(baseline))
        assert result.returncode == 1, result.stdout + result.stderr
        assert "now defines them" in result.stderr

    def test_a_baseline_line_without_an_owner_is_refused(self, tmp_path: Path) -> None:
        baseline = tmp_path / "baseline.txt"
        baseline.write_text("orphan\n", encoding="utf-8")
        result = _run(str(WORKFLOW_MISSING), str(DESIGN_EXCERPT), "--baseline", str(baseline))
        assert result.returncode != 0
        assert "no owning task" in result.stdout + result.stderr
