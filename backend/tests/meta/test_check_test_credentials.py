# SPDX-License-Identifier: FSL-1.1-ALv2
"""`scripts/check-test-credentials.py` catches what it claims to (secret-safety.md).

A gate is only worth having if it has been observed to fail. This drives the checker against a
fixture holding every shape it must flag and one holding every shape it must not, and asserts
the vacuity guard fires on an empty scan — the failure mode that would make the gate green
forever regardless of what the tests contained.

Written because the rule this checker enforces had no enforcement at all until now, and
stopped holding the first time it was inconvenient: `test_q19_route_coverage.py` shipped
`Bearer …`, `Basic <base64>` and an `eyJ`-prefixed JWT as source literals.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPO_ROOT / "scripts" / "check-test-credentials.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
BAD = FIXTURES / "bad_credential.py"
GOOD = FIXTURES / "good_credential.py"


def _checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("forgeops_check_test_credentials", CHECKER_PATH)
    assert spec is not None and spec.loader is not None, CHECKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Every rule the bad fixture exercises, by the name the checker reports.
EXPECTED_RULES = (
    "a JWT (`eyJ` header)",
    "an HTTP Bearer credential",
    "an HTTP Basic credential",
    "an OpenAI-style key",
    "a GitHub token",
    "an AWS access key id",
    "a Google API key",
    "a Slack token",
    "a PEM block",
)


class TestTheCheckerFlagsTheBadFixture:
    def test_it_reports_findings(self) -> None:
        findings, files, literals = _checker().check([BAD])
        assert files == 1
        assert literals > 0
        assert findings, "the bad fixture produced no findings; the checker sees nothing"

    @pytest.mark.parametrize("rule", EXPECTED_RULES, ids=EXPECTED_RULES)
    def test_every_rule_fires(self, rule: str) -> None:
        """Parametrised per rule, so one broken pattern is one red test rather than a
        general "something matched"."""
        findings, _files, _literals = _checker().check([BAD])
        assert any(rule in finding for finding in findings), (
            f"no finding mentioned {rule!r}; that pattern matches nothing. Findings: {findings}"
        )

    def test_a_suppression_without_a_reason_is_itself_a_finding(self) -> None:
        findings, _files, _literals = _checker().check([BAD])
        assert any("suppression without a reason" in finding for finding in findings), findings

    def test_no_finding_echoes_the_matched_value(self) -> None:
        """A checker that printed the value would put the shape it objects to into CI logs."""
        findings, _files, _literals = _checker().check([BAD])
        joined = " ".join(findings)
        for leaked in ("JhbGciOiJub25lIn0", "abcdefghijklmnop", "YWxpY2U6b3Blbi1zZXNhbWU"):
            assert leaked not in joined, f"a finding echoed {leaked!r}"
        assert "chars)" in joined, joined


class TestTheCheckerAcceptsTheGoodFixture:
    def test_it_reports_nothing(self) -> None:
        findings, files, literals = _checker().check([GOOD])
        assert files == 1
        assert literals > 0, "no literals examined, so acceptance proves nothing"
        assert not findings, findings

    def test_docstring_prose_about_a_shape_is_not_a_finding(self) -> None:
        """The good fixture's docstrings name `eyJ`, `sk-` and `AKIA…` deliberately. If those
        were findings, the only way to pass would be to delete the explanation."""
        findings, _files, _literals = _checker().check([GOOD])
        assert not any("eyJ" in finding for finding in findings), findings


class TestTheVacuityGuard:
    def test_an_empty_directory_is_a_failure_not_a_pass(self, tmp_path: Path) -> None:
        findings, files, literals = _checker().check([tmp_path])
        assert files == 0 and literals == 0
        # `check()` reports the counts; `main()` is what turns them into exit 1.
        assert not findings

    def test_main_exits_one_on_an_empty_scan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _checker()
        monkeypatch.setattr("sys.argv", ["check-test-credentials.py", str(tmp_path)])
        assert module.main() == 1

    def test_main_exits_one_on_the_bad_fixture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _checker()
        monkeypatch.setattr("sys.argv", ["check-test-credentials.py", str(BAD)])
        assert module.main() == 1

    def test_main_exits_zero_on_the_good_fixture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _checker()
        monkeypatch.setattr("sys.argv", ["check-test-credentials.py", str(GOOD)])
        assert module.main() == 0


class TestTheRealTestTreeIsClean:
    def test_no_credential_shaped_literal_anywhere_under_backend_tests(self) -> None:
        """The assertion that would have caught the Q-19 regression at the moment it was
        written, rather than after it was pushed."""
        findings, files, literals = _checker().check([REPO_ROOT / "backend" / "tests"])
        assert files > 50, f"only {files} files scanned; the glob is wrong"
        assert literals > 500, f"only {literals} literals examined; the scan is too narrow"
        assert not findings, findings

    def test_the_directory_scan_skips_the_fixtures_but_a_named_fixture_is_scanned(self) -> None:
        """The exclusion is a traversal rule, not an allowlist — otherwise the fixture whose
        job is to be flagged could never be reached, and the whole file above would be
        untested. Both halves are asserted, because an exclusion that swallowed the tree would
        make `test_no_credential_shaped_literal…` pass over nothing."""
        module = _checker()
        traversed = list(module.iter_python_files([REPO_ROOT / "backend" / "tests"]))
        assert BAD not in traversed, "the bad fixture is reachable by traversal; the tree will be red"
        assert GOOD not in traversed
        assert len(traversed) > 50, f"the exclusion swallowed the tree: {len(traversed)} files"

        named = list(module.iter_python_files([BAD]))
        assert named == [BAD], named

    def test_the_synthetic_secrets_helper_is_exempt_but_present(self) -> None:
        """Exempt from the literal scan, not from the rule: it holds no contiguous shape."""
        module = _checker()
        assert "backend/tests/synthetic_secrets.py" in module.EXEMPT_FILES
        assert (REPO_ROOT / "backend" / "tests" / "synthetic_secrets.py").is_file()
