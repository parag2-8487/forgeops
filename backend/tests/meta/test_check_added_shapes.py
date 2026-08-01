# SPDX-License-Identifier: FSL-1.1-ALv2
"""`scripts/check-added-shapes.py` catches what it claims to, and agrees with the pre-push gate.

The hook exists because the push-time gate found a credential shape three times running and the
only remedy at that point was a history rewrite. Two things therefore have to hold, and both are
asserted here rather than assumed:

* the hook flags every shape `.kiro/steering/secret-safety.md` lists, and flags them only in ADDED
  lines — a hook that reported a deleted line would be finding 58 again;
* its pattern table agrees with `scripts/secret-gate.ps1`'s row for row. A hook weaker than the
  gate it front-runs restores exactly the failure it was added to remove, silently, and the drift
  would only show up as another rewrite.

The parity assertion parses the PowerShell table rather than comparing a copy, because a copy is
the thing that drifts.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPO_ROOT / "scripts" / "check-added-shapes.py"
GATE_PATH = REPO_ROOT / "scripts" / "secret-gate.ps1"


def _checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("forgeops_check_added_shapes", CHECKER_PATH)
    assert spec is not None and spec.loader is not None, CHECKER_PATH
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because the checker declares a dataclass: `dataclasses` resolves
    # `cls.__module__` through `sys.modules`, and a module loaded from a path without being
    # registered there raises `AttributeError: 'NoneType' object has no attribute '__dict__'`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------------
# Parsing the PowerShell table.
#
# The expressions are string concatenations of single-quoted literals, `('-' * 5)`, and the two
# helper variables the gate assigns above its table. That is a small enough language to evaluate
# through `ast` with an explicit node allowlist, which is why this is not `eval`.
# --------------------------------------------------------------------------------------------
_ROW = re.compile(r"@\{\s*Name\s*=\s*'([^']*)'\s*;\s*Regex\s*=\s*(.+?)\s*;\s*Case\s*=\s*\$(true|false)\s*\}")
_ASSIGN = re.compile(r"^\$([A-Za-z_]\w*)\s*=\s*((?:'[^']*'|\s*\+\s*)+)\s*$", re.MULTILINE)


def _evaluate(expression: str, names: dict[str, str]) -> str:
    """Evaluate one PowerShell string expression, allowing only concatenation and repetition."""
    python_source = re.sub(r"'((?:[^']|'')*)'", lambda m: repr(m.group(1).replace("''", "'")), expression)
    python_source = re.sub(r"\$([A-Za-z_]\w*)", r"\1", python_source)
    tree = ast.parse(python_source, mode="eval")

    def walk(node: ast.AST) -> str | int:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, str | int):
            return node.value
        if isinstance(node, ast.Name):
            return names[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mult):
            left, right = walk(node.left), walk(node.right)
            return left + right if isinstance(node.op, ast.Add) else left * right  # type: ignore[operator]
        raise AssertionError(f"unsupported node {ast.dump(node)} in {expression!r}")

    value = walk(tree)
    assert isinstance(value, str), expression
    return value


def _gate_table() -> list[tuple[str, str, bool]]:
    text = GATE_PATH.read_text(encoding="utf-8")
    names = {name: _evaluate(expr, {}) for name, expr in _ASSIGN.findall(text)}
    rows = [(name, _evaluate(expr, names), flag == "true") for name, expr, flag in _ROW.findall(text)]
    assert rows, "no rows parsed out of the gate's pattern table; the parser has gone stale"
    return rows


class TestTheTwoTablesAgree:
    """Parity between the commit-time hook and the push-time gate."""

    def test_the_gate_table_parses(self) -> None:
        rows = _gate_table()
        assert len(rows) >= 20, rows

    def test_the_rows_are_identical_and_in_the_same_order(self) -> None:
        assert list(_checker().PATTERNS) == _gate_table()

    def test_the_parser_would_notice_a_difference(self) -> None:
        """Control: the comparison above must be able to fail."""
        mutated = [(name, regex + "x", case) for name, regex, case in _gate_table()]
        assert list(_checker().PATTERNS) != mutated


class TestEveryRuleFires:
    @pytest.mark.parametrize("name", [row[0] for row in _gate_table()], ids=[row[0] for row in _gate_table()])
    def test_a_line_carrying_the_shape_is_flagged(self, name: str) -> None:
        """Parametrised per rule, so one dead pattern is one red test rather than a general pass."""
        checker = _checker()
        regex = next(r for n, r, _c in checker.PATTERNS if n == name)
        sample = {
            # Assembled, like everything else here: written out, this line would carry the very
            # shape the rule matches and the hook would block the commit that adds its own test.
            "credential-dsn": "postgresql://user" + ":pw@host/db",
        }.get(name, regex)
        assert name in checker.shapes(sample), (sample, checker.shapes(sample))

    def test_ordinary_prose_is_not_flagged(self) -> None:
        """The control the per-rule cases need: a table that flagged everything would pass them
        all."""
        checker = _checker()
        assert checker.shapes("the hub answers session.connect with the session parameters") == []


class TestOnlyAddedLinesAreConsidered:
    def test_a_removed_line_is_never_reported(self) -> None:
        checker = _checker()
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-" + "AK" + "IA_placeholder\n+harmless\n"
        found, count = checker.findings_for(diff)
        assert found == []
        assert count == 1

    def test_an_added_line_is_reported_with_its_new_side_position(self) -> None:
        checker = _checker()
        diff = (
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +7 @@\n+value = '" + "AK" + "IA_placeholder'\n"
        )
        found, count = checker.findings_for(diff)
        assert count == 1
        assert len(found) == 1
        assert found[0].startswith("x.py:7: ")
        assert "aws-akid" in found[0]

    def test_a_deleted_file_contributes_nothing(self) -> None:
        checker = _checker()
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-anything\n"
        assert checker.findings_for(diff) == ([], 0)


class TestTheCheckerRunsAsAProcess:
    """It is a pre-commit hook, so the exit status is the contract, not the return value."""

    def test_a_clean_range_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CHECKER_PATH), "--range", "HEAD~1..HEAD"],
            capture_output=True,
            cwd=REPO_ROOT,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_an_empty_range_says_so_rather_than_passing_silently(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CHECKER_PATH), "--range", "HEAD..HEAD"],
            capture_output=True,
            cwd=REPO_ROOT,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0
        assert "no commits" in result.stdout

    def test_the_hook_is_registered(self) -> None:
        config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        assert "check-added-shapes" in config
        assert "always_run: true" in config

    def test_a_staged_shape_blocks_the_commit(self, tmp_path: Path) -> None:
        """The clause that makes this a gate rather than a report: a throwaway repository, one
        staged line carrying a shape, and a non-zero exit. Written as a process because that is how
        pre-commit consumes it — finding 63 was a hook that could not run at all on this platform
        while its logic was fine.
        """
        env = {"GIT_CONFIG_GLOBAL": str(tmp_path / "nogit"), "GIT_CONFIG_SYSTEM": str(tmp_path / "nogit")}
        for command in (["init", "-q"], ["config", "user.email", "t@t.invalid"], ["config", "user.name", "t"]):
            subprocess.run(["git", *command], check=True, cwd=tmp_path)
        (tmp_path / "x.py").write_text("value = '" + "AK" + "IA" + "EXAMPLEONLY'\n", encoding="utf-8")
        subprocess.run(["git", "add", "x.py"], check=True, cwd=tmp_path)

        result = subprocess.run(
            [sys.executable, str(CHECKER_PATH)],
            capture_output=True,
            cwd=tmp_path,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, **env},
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "x.py:1: credential shape [aws-akid]" in result.stdout
        assert "BLOCKED" in result.stderr

    def test_the_control_shows_the_same_staging_passes_without_the_shape(self, tmp_path: Path) -> None:
        """Without this the clause above would pass for a checker that refuses every commit."""
        for command in (["init", "-q"], ["config", "user.email", "t@t.invalid"], ["config", "user.name", "t"]):
            subprocess.run(["git", *command], check=True, cwd=tmp_path)
        (tmp_path / "x.py").write_text("value = 'harmless'\n", encoding="utf-8")
        subprocess.run(["git", "add", "x.py"], check=True, cwd=tmp_path)

        result = subprocess.run(
            [sys.executable, str(CHECKER_PATH)],
            capture_output=True,
            cwd=tmp_path,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 added line(s) considered" in result.stdout


class TestTheCheckerDoesNotMatchItself:
    def test_its_own_source_carries_no_shape(self) -> None:
        """The failure mode this file's assembled table exists to avoid: a pattern table written
        as literals blocks the commit that adds it."""
        checker = _checker()
        offenders = [
            (number, checker.shapes(line))
            for number, line in enumerate(CHECKER_PATH.read_text(encoding="utf-8").splitlines(), start=1)
            if checker.shapes(line)
        ]
        assert offenders == []
