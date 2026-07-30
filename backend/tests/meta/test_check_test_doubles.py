# SPDX-License-Identifier: FSL-1.1-ALv2
"""The test-double lint's own tests (design.md §0.4.3).

A lint whose own tests are missing is a lint nobody trusts, and a lint whose
failure path has never fired is not a lint at all. These tests assert that every
rule fires on `fixtures/bad_double.py` and that `fixtures/good_double.py` is
completely clean, so neither direction can rot.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-test-doubles.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
BAD = FIXTURES / "bad_double.py"
GOOD = FIXTURES / "good_double.py"


def _load_script() -> ModuleType:
    """Import the hyphenated script by path; it is not an importable module name."""
    spec = importlib.util.spec_from_file_location("forgeops_check_test_doubles", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LINT = _load_script()


def _codes(path: Path) -> list[str]:
    findings, _ = LINT.check_file(path)
    return [f.code for f in findings]


class TestTheNegativeFixtureIsFlagged:
    def test_the_bad_fixture_produces_findings(self) -> None:
        assert _codes(BAD), "bad_double.py produced no findings; the lint is decorative"

    @pytest.mark.parametrize("code", ["FO-TD001", "FO-TD002", "FO-TD003"])
    def test_every_rule_fires_at_least_once(self, code: str) -> None:
        """Each rule must be demonstrated, not merely defined."""
        assert code in _codes(BAD), f"{code} never fired on the negative fixture"

    def test_the_reassignment_defect_is_reported_with_its_line(self) -> None:
        """The D-23 pattern must be located, not just counted."""
        findings, _ = LINT.check_file(BAD)
        reassignments = [f for f in findings if f.code == "FO-TD001"]
        assert reassignments
        source = BAD.read_text(encoding="utf-8").splitlines()
        for finding in reassignments:
            line = source[finding.line - 1]
            assert "=" in line, f"FO-TD001 pointed at a non-assignment: {line!r}"

    def test_a_reasonless_suppression_is_itself_a_finding(self) -> None:
        """`# noqa: FO-TD001` with no reason must not silence anything."""
        findings, _ = LINT.check_file(BAD)
        messages = [f.message for f in findings]
        assert any("reasonless suppression" in m for m in messages), (
            "a bare noqa was honoured; that is how this defect class gets waved through"
        )


class TestThePositiveFixtureIsClean:
    def test_the_good_fixture_produces_no_findings(self) -> None:
        findings, _ = LINT.check_file(GOOD)
        assert not findings, f"good_double.py was wrongly flagged: {[str(f) for f in findings]}"

    def test_a_reasoned_suppression_is_recorded_as_suppressed(self) -> None:
        """It must be waived, and visibly waived, rather than never detected."""
        _, suppressed = LINT.check_file(GOOD)
        assert any(f.code == "FO-TD001" for f in suppressed), (
            "the reasoned suppression was never exercised, so the fixture proves nothing"
        )


class TestFO_TD004_IsScopedToIntegration:  # noqa: N801 - rule id in the name
    def test_a_mock_under_integration_is_flagged(self, tmp_path: Path) -> None:
        target = tmp_path / "tests" / "integration" / "test_x.py"
        target.parent.mkdir(parents=True)
        target.write_text("from unittest.mock import Mock\nm = Mock()\n", encoding="utf-8")
        assert "FO-TD004" in _codes(target)

    def test_the_same_mock_under_unit_is_not_flagged_by_TD004(self, tmp_path: Path) -> None:  # noqa: N802
        target = tmp_path / "tests" / "unit" / "test_x.py"
        target.parent.mkdir(parents=True)
        target.write_text("from unittest.mock import Mock\nm = Mock()\n", encoding="utf-8")
        assert "FO-TD004" not in _codes(target)


class TestTheRealTreeIsClean:
    def test_the_committed_test_tree_has_no_findings(self) -> None:
        """The gate itself: `backend/tests` must be clean, fixtures excluded."""
        findings, _, scanned = LINT.check_paths([REPO_ROOT / "backend" / "tests"])
        assert scanned > 0, "the scan found no files; the check would pass vacuously"
        assert not findings, "\n".join(str(f) for f in findings)

    def test_the_negative_fixture_is_excluded_from_the_default_walk(self) -> None:
        """Otherwise the real tree could never be clean and the check gets disabled."""
        _, _, scanned_all = LINT.check_paths([REPO_ROOT / "backend" / "tests"])
        assert LINT._is_lint_fixture(BAD)
        assert scanned_all > 0
