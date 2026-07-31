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


def _go_log(tmp_path: Path, test: str, reason_line: str) -> Path:
    """Write a `go test -json` report with exactly one skipped test and one passing one.

    The passing test is not decoration: without it a report holding only a skip would also be
    testing the empty-selection clause, and the two failures would be indistinguishable.
    """
    path = tmp_path / f"{test}.jsonl"
    events = [
        {"Action": "run", "Package": "pkg/ok", "Test": "TestPasses"},
        {"Action": "output", "Package": "pkg/ok", "Test": "TestPasses", "Output": "=== RUN   TestPasses\n"},
        {"Action": "pass", "Package": "pkg/ok", "Test": "TestPasses"},
        {"Action": "run", "Package": "pkg/under-test", "Test": test},
        {
            "Action": "output",
            "Package": "pkg/under-test",
            "Test": test,
            "Output": f"=== RUN   {test}\n",
        },
        {"Action": "output", "Package": "pkg/under-test", "Test": test, "Output": reason_line + "\n"},
        {
            "Action": "output",
            "Package": "pkg/under-test",
            "Test": test,
            "Output": f"--- SKIP: {test} (0.00s)\n",
        },
        {"Action": "skip", "Package": "pkg/under-test", "Test": test},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
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
        # `--os` is explicit so the test does not need a Go toolchain to decide the platform.
        result = _run(str(path), "--go", "--os", "linux")
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

    def test_a_clean_go_report_needs_no_go_toolchain(self, tmp_path: Path) -> None:
        """The platform is resolved lazily, so confirming a clean run never shells out.

        The backend test job has no Go. A gate that ran `go env GOOS` to confirm that nothing
        skipped would be unable to pass in a job with every right to run it — the shape D-51
        rejects — so the resolution happens only when there is a skip to classify.
        """
        path = tmp_path / "go.jsonl"
        events = [
            {"Action": "run", "Package": "pkg/a", "Test": "TestOne"},
            {"Action": "pass", "Package": "pkg/a", "Test": "TestOne"},
        ]
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

        def _explode() -> str:
            raise AssertionError("the platform must not be resolved when nothing skipped")

        assert GATE.check(GATE.parse_go(path), source=str(path), allow_empty=False, platform=_explode) == 0

    def test_the_go_skip_reason_is_captured_not_just_the_banner(self, tmp_path: Path) -> None:
        """The reason line does not contain the word "skip", and used to be discarded.

        `parse_go` filtered output lines on "skip", which matched the `--- SKIP: TestX` BANNER
        and not the `x_test.go:9: <reason>` line above it. So the gate whose whole purpose is
        reporting skips could not report why any of them skipped — and D-68's declaration lives
        in exactly that line, so the platform classification would have seen nothing.
        """
        path = _go_log(tmp_path, "TestQuiet", "    x_test.go:9: the parent process vanished")
        outcomes = GATE.parse_go(path)
        skipped = [o for o in outcomes if o.outcome == "skipped"]
        assert len(skipped) == 1
        assert "the parent process vanished" in skipped[0].reason
        assert "--- SKIP" not in skipped[0].reason, "framing lines are noise, not a reason"
        assert "=== RUN" not in skipped[0].reason


class TestPlatformConditionalSkips:
    """D-68. A skip may declare a platform it cannot run on; nothing else is exempt."""

    def test_a_declared_platform_skip_passes_on_a_platform_that_cannot_run_it(self, tmp_path: Path) -> None:
        path = _go_log(
            tmp_path,
            "TestNeedsPosix",
            "    store_test.go:1: platform-only: posix - NTFS uses ACLs",
        )
        result = _run(str(path), "--go", "--os", "windows")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PERMITTED" in result.stdout
        assert "1 skip(s) permitted" in result.stdout

    def test_the_same_declaration_fails_where_the_platform_satisfies_it(self, tmp_path: Path) -> None:
        """The clause that stops the tag being a blanket exemption.

        This is the control on D-68: the identical report, judged for linux, must FAIL. Without
        it, writing `platform-only: posix` into any skip message would exempt it everywhere,
        including in CI, which is the whole guarantee §0.4.4 exists to hold.
        """
        path = _go_log(
            tmp_path,
            "TestNeedsPosix",
            "    store_test.go:1: platform-only: posix - NTFS uses ACLs",
        )
        result = _run(str(path), "--go", "--os", "linux")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "SATISFIES" in result.stderr

    def test_an_undeclared_skip_still_fails_on_every_platform(self, tmp_path: Path) -> None:
        path = _go_log(tmp_path, "TestSilent", "    x_test.go:1: tofu not found")
        for goos in ("windows", "linux", "darwin"):
            result = _run(str(path), "--go", "--os", goos)
            assert result.returncode == 1, f"{goos}: {result.stdout}{result.stderr}"
            assert "no platform declaration" in result.stderr

    def test_an_unknown_requirement_fails_rather_than_exempting(self, tmp_path: Path) -> None:
        """A typo must not be a pass. This is why the vocabulary is closed."""
        path = _go_log(tmp_path, "TestTypo", "    x_test.go:1: platform-only: posixx - oops")
        result = _run(str(path), "--go", "--os", "windows")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "unknown platform requirement" in result.stderr

    def test_the_declaration_is_read_from_the_message_not_from_a_list(self) -> None:
        """The requirement travels with the guard that produces it.

        A list of exempt test NAMES in this repository would be data restated away from the
        `runtime.GOOS` check that causes the skip — finding 49's rot in a new place. Renaming the
        test would strand the entry; deleting the guard would leave it exempting nothing.
        """
        source = SCRIPT.read_text(encoding="utf-8")
        assert "platform-only:" in source
        for forbidden in ("TestFileStore_WritesOwnerOnly", "TestResolve_RejectsASymlinkEscape"):
            assert forbidden not in source, f"{forbidden} is named in the gate; the declaration must live in the test"

    def test_the_vocabulary_covers_both_directions(self) -> None:
        assert GATE.PLATFORM_REQUIREMENTS["posix"]("linux") is True
        assert GATE.PLATFORM_REQUIREMENTS["posix"]("darwin") is True
        assert GATE.PLATFORM_REQUIREMENTS["posix"]("windows") is False
        assert GATE.PLATFORM_REQUIREMENTS["windows"]("windows") is True
        assert GATE.PLATFORM_REQUIREMENTS["windows"]("linux") is False

    def test_os_is_rejected_for_a_pytest_report(self, tmp_path: Path) -> None:
        """A pytest report carries no GOOS, so accepting the flag would invite a false verdict."""
        log = _pytest_log(tmp_path, [("tests/x.py::t", "call", "passed")])
        result = _run(str(log), "--os", "linux")
        assert result.returncode == 2
        assert "--go reports only" in result.stderr

    def test_the_agent_ci_job_runs_this_gate_over_a_go_report(self) -> None:
        """The gate's CI result must be observed, not inferred.

        design.md criterion 11 says the `agent` job runs `check-no-skips.py`. It did not: the
        only invocation in the workflow was the backend `auth` job's. So the Go side's zero-skip
        claim rested on nothing, which is how the nine skips this leaf found survived.
        """
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "check-no-skips.py --go" in workflow, "the agent job must run the Go half"
        agent_job = workflow.split("\n  agent:", 1)[1].split("\n  backend:", 1)[0]
        assert "check-no-skips.py --go" in agent_job, "the Go half must be in the agent job"
        assert "go test -json" in agent_job, "the agent job must produce the report it checks"
        assert "setup-python" in agent_job, "the agent job invokes `python`, so it needs one"


class TestTestsDoNotClobberTheProcessEnvironment:
    """The defect the nine skips turned out to be hiding.

    `agent/internal/iac/env_test.go` called `os.Setenv("PATH", "/usr/bin")` with no restore, so
    every later test in that binary saw a one-entry PATH and the three `TestTerminateGroup_*`
    tests skipped with "powershell.exe is not available". They passed under `-run` and skipped in
    the full suite, so the skip looked like a platform limitation and was caused by a sibling.

    `defer os.Unsetenv` is not the fix either: it DELETES the name, so for a variable that
    already existed — `PATH` always does — it restores the process to a state nobody asked for.
    """

    IAC_TESTS = REPO_ROOT / "agent" / "internal" / "iac"

    @staticmethod
    def _code_lines(path: Path) -> list[tuple[int, str]]:
        """Numbered lines with `//` comments dropped.

        Both checks below first flagged the paragraphs that EXPLAIN the defect, because those
        paragraphs quote the offending call. A source scan that cannot tell code from prose
        reports its own documentation as a violation, and the tempting fix — deleting the
        explanation — is the wrong one.
        """
        out: list[tuple[int, str]] = []
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = raw.strip()
            if stripped.startswith("//"):
                continue
            out.append((number, raw))
        return out

    def test_no_iac_test_sets_a_process_variable_without_restoring_it(self) -> None:
        offenders: list[str] = []
        for path in sorted(self.IAC_TESTS.glob("*_test.go")):
            text = path.read_text(encoding="utf-8")
            for number, line in self._code_lines(path):
                if "os.Setenv(" not in line:
                    continue
                # The one permitted shape is the property test's explicit save-and-restore,
                # which cannot use t.Setenv inside a rapid closure. It is recognisable because
                # the file also captures the previous value.
                if "os.LookupEnv(" in text:
                    continue
                offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert not offenders, (
            "os.Setenv in a test mutates the whole test binary; use t.Setenv, which restores "
            "the previous value:\n  " + "\n  ".join(offenders)
        )

    def test_no_iac_test_restores_a_variable_by_deleting_it(self) -> None:
        offenders: list[str] = []
        for path in sorted(self.IAC_TESTS.glob("*_test.go")):
            for number, line in self._code_lines(path):
                if "defer os.Unsetenv(" in line:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert not offenders, (
            "Unsetenv deletes rather than restores; a name that existed beforehand is not put "
            "back:\n  " + "\n  ".join(offenders)
        )

    def test_the_three_terminate_group_tests_no_longer_skip_in_a_full_package_run(self) -> None:
        """The observable consequence, asserted rather than described.

        Read from the source: the guard that made them skip was a sibling's `os.Setenv`, and
        with that gone the only remaining reason to skip is powershell genuinely being absent —
        which on a Windows machine means something is wrong, so it stays an undeclared skip and
        the gate fails it. The three tests are NOT tagged `platform-only`, and that is the
        assertion: tagging them would have hidden the defect instead of fixing it.
        """
        source = (self.IAC_TESTS / "procattr_windows_test.go").read_text(encoding="utf-8")
        assert "powershell.exe is not available" in source, "the capability guard is still there"
        assert "platform-only:" not in source, (
            "these skips are capability skips, not platform guards; declaring them would have "
            "papered over the PATH clobber rather than fixing it"
        )


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
