# SPDX-License-Identifier: FSL-1.1-ALv2
"""The no-skips gate's own tests (design.md §0.4.4).

Two failure paths must be demonstrated, not merely written:

1. a mandatory node that skipped, and
2. an **empty** mandatory selection.

The second is the subtle one. `pytest -m mandatory` over a tree with no
`mandatory` markers exits 0 having run nothing, so a gate that only looked for
skips would pass forever while proving nothing — the same vacuity trap §0.4.5
closes for the mutation harness. Phase 0's criterion 14 failed in exactly this
shape: a real database service running beside seven tests that never executed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-no-skips.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("forgeops_check_no_skips", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = _load()


def _pytest_log(tmp_path: Path, records: list[tuple[str, str, str]]) -> Path:
    """Write a minimal `--report-log` file: (nodeid, when, outcome) triples."""
    path = tmp_path / "report.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for node_id, when, outcome in records:
            event = {
                "$report_type": "TestReport",
                "nodeid": node_id,
                "when": when,
                "outcome": outcome,
            }
            if outcome == "skipped":
                event["longrepr"] = ["file.py", 1, "Skipped: capability 'postgres' unavailable"]
            handle.write(json.dumps(event) + "\n")
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class TestASkipIsDetected:
    def test_a_skipped_mandatory_node_fails_the_gate(self, tmp_path: Path) -> None:
        log = _pytest_log(
            tmp_path,
            [
                ("tests/unit/test_a.py::test_ok", "call", "passed"),
                ("tests/integration/test_b.py::test_gated", "setup", "skipped"),
            ],
        )
        result = _run(str(log))
        assert result.returncode == 1, result.stdout + result.stderr
        assert "test_gated" in result.stderr

    def test_a_setup_skip_is_not_masked_by_a_later_phase(self, tmp_path: Path) -> None:
        """`--report-log` emits one record per phase; a skip must win the fold.

        Without folding, a test skipped at setup still emits a `passed` teardown
        record, and reading the last record would report the run as clean.
        """
        log = _pytest_log(
            tmp_path,
            [
                ("tests/integration/test_b.py::test_gated", "setup", "skipped"),
                ("tests/integration/test_b.py::test_gated", "teardown", "passed"),
            ],
        )
        outcomes = GATE.parse_pytest(log)
        assert [o.outcome for o in outcomes] == ["skipped"]
        assert _run(str(log)).returncode == 1

    def test_the_skip_reason_is_reported(self, tmp_path: Path) -> None:
        log = _pytest_log(tmp_path, [("tests/x.py::t", "setup", "skipped")])
        result = _run(str(log))
        assert "postgres" in result.stderr


class TestAnEmptySelectionIsDetected:
    def test_an_empty_report_fails_the_gate(self, tmp_path: Path) -> None:
        """The clause the hard gate names explicitly."""
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        result = _run(str(empty))
        assert result.returncode == 1, result.stdout + result.stderr
        assert "EMPTY" in result.stderr

    def test_a_report_with_no_test_records_fails_the_gate(self, tmp_path: Path) -> None:
        """A well-formed log that contains only session events is still empty."""
        path = tmp_path / "session-only.jsonl"
        path.write_text(json.dumps({"$report_type": "CollectReport"}) + "\n", encoding="utf-8")
        assert _run(str(path)).returncode == 1

    def test_allow_empty_is_available_only_to_these_tests(self, tmp_path: Path) -> None:
        """The escape hatch exists, is explicit, and is never used in CI."""
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        assert _run(str(empty), "--allow-empty").returncode == 0
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "--allow-empty" not in workflow, "CI must never permit an empty mandatory selection"


class TestGoOutputIsUnderstood:
    def test_a_skipped_go_test_fails_the_gate(self, tmp_path: Path) -> None:
        path = tmp_path / "go.jsonl"
        events = [
            {"Action": "run", "Package": "pkg/a", "Test": "TestOne"},
            {"Action": "pass", "Package": "pkg/a", "Test": "TestOne"},
            {"Action": "run", "Package": "pkg/b", "Test": "TestTwo"},
            {
                "Action": "output",
                "Package": "pkg/b",
                "Test": "TestTwo",
                "Output": "    x_test.go:9: SKIP tofu not found",
            },
            {"Action": "skip", "Package": "pkg/b", "Test": "TestTwo"},
        ]
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        result = _run(str(path), "--go")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "TestTwo" in result.stderr

    def test_an_all_passing_go_run_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "go.jsonl"
        events = [
            {"Action": "run", "Package": "pkg/a", "Test": "TestOne"},
            {"Action": "pass", "Package": "pkg/a", "Test": "TestOne"},
        ]
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        assert _run(str(path), "--go").returncode == 0


class TestCapabilityRegistration:
    def test_an_unregistered_capability_is_an_error_not_a_skip(self) -> None:
        """A misspelled capability that skipped would be D-26 all over again."""
        sys.path.insert(0, str(REPO_ROOT / "backend"))
        from tests.integration.capability import UnknownCapabilityError, require_capability

        with pytest.raises(UnknownCapabilityError):
            require_capability("postgress")  # deliberate typo

    def test_every_phase_1_capability_is_registered(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "backend"))
        from tests.integration.capability import CAPABILITIES

        expected = {"opa", "cerbos", "oidc", "kubernetes", "trivy", "infisical", "agent_binary"}
        assert expected <= set(CAPABILITIES), sorted(expected - set(CAPABILITIES))
