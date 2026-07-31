# SPDX-License-Identifier: FSL-1.1-ALv2
"""The chokepoint check's own tests (design.md §0.4.5, §2.2.1, leaf 7.3).

A check whose failure path has never fired is not a check. Leaf 7.3's own entry says as much:
§2.2.1 requires `exit 1` on an **empty** discovered primitive set precisely so a renamed
decorator cannot make the whole thing trivially pass. That rule is what made the leaf
unbuildable in its original position, and it is the first thing asserted here.

Four groups of assertions:

* the negative fixture is flagged, and **every branch** of the Python half fires at least once;
* the positive fixture and the real tree are clean, so the check is usable rather than merely
  loud — a check that flags `list.append` gets switched off within a week;
* the vacuity guards fail closed: an empty primitive set, an empty import graph, and a graph
  missing the boundary package;
* the Go half's classification reports an offender, fed a synthetic graph, because `go list`
  cannot see the compile-time fixture under `testdata`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "scripts" / "chokepoint_graph.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "chokepoint"
BAD = FIXTURES / "bad_chokepoint.py"
GOOD = FIXTURES / "governance" / "good_chokepoint.py"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
AGENT_ROOT = REPO_ROOT / "agent"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("forgeops_chokepoint_graph", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECK = _load()


@pytest.fixture(scope="module")
def fixture_analysis() -> tuple[list, list]:
    return CHECK.analyse(FIXTURES)


class TestTheFixturesExistAndAreNotProductionCode:
    def test_both_fixtures_are_present(self) -> None:
        assert BAD.is_file() and GOOD.is_file()

    def test_neither_fixture_lives_under_the_walked_source_tree(self) -> None:
        """A permanent offender under `backend/src/` would red the build forever."""
        for fixture in (BAD, GOOD):
            assert BACKEND_SRC not in fixture.parents, fixture


class TestTheNegativeFixtureIsFlagged:
    def test_the_bad_fixture_produces_offenders(self, fixture_analysis: tuple[list, list]) -> None:
        _, calls = fixture_analysis
        offenders = [call for call in calls if not call.authorised]
        assert offenders, "bad_chokepoint.py produced no offenders; the check is decorative"

    @pytest.mark.parametrize("verdict", ["no-authority", "unresolved-receiver"])
    def test_every_failing_verdict_fires_at_least_once(self, fixture_analysis: tuple[list, list], verdict: str) -> None:
        """Each way of failing must be demonstrated, not merely defined."""
        _, calls = fixture_analysis
        assert any(call.verdict == verdict for call in calls), f"{verdict} never fired on the fixture"

    @pytest.mark.parametrize("verdict", ["governance", "authority"])
    def test_every_passing_verdict_fires_at_least_once(self, fixture_analysis: tuple[list, list], verdict: str) -> None:
        """And each way of PASSING, or the check could be refusing everything."""
        _, calls = fixture_analysis
        assert any(call.verdict == verdict for call in calls), f"{verdict} never fired on the fixture"

    def test_an_authority_named_but_not_typed_is_still_an_offender(self, fixture_analysis: tuple[list, list]) -> None:
        """`authority=None` must not satisfy the check, while a real one must.

        This is the case a keyword-name heuristic waves through, and it is the exact failure
        §11.6 says the capability type replaces. Both call sites in the fixture spell the
        argument `authority=authority`; only one of them has a `MutationAuthority` behind it.
        """
        _, calls = fixture_analysis
        verdict_by_line = {call.line: call.verdict for call in calls}
        untyped = _line_of(BAD, 'writer.append("pretending"')
        typed = _line_of(BAD, 'writer.append("authorised", authority=authority)')
        assert verdict_by_line.get(untyped) == "no-authority", verdict_by_line
        assert verdict_by_line.get(typed) == "authority", verdict_by_line

    def test_a_list_append_is_never_reported(self, fixture_analysis: tuple[list, list]) -> None:
        """The false-positive that would make the real check unusable."""
        _, calls = fixture_analysis
        lines = _lines(BAD)
        list_call = next(index for index, text in enumerate(lines, 1) if "collected.append(" in text)
        assert list_call not in {call.line for call in calls}, (
            "a list.append was reported as a mutation primitive; the real check would then "
            "produce hundreds of false positives in backend/src and get switched off"
        )

    def test_offender_messages_name_the_file_the_line_and_the_primitive(
        self, fixture_analysis: tuple[list, list]
    ) -> None:
        _, calls = fixture_analysis
        for call in (call for call in calls if not call.authorised):
            rendered = call.render()
            assert call.path in rendered
            assert f":{call.line}:" in rendered
            assert call.primitive in rendered


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _line_of(path: Path, needle: str) -> int:
    """The 1-based line number of the first line containing `needle`.

    Raises rather than returning a sentinel: a fixture edit that removed the line the test is
    about would otherwise turn the assertion into a comparison against `-1`, which passes for
    the wrong reason.
    """
    for index, text in enumerate(_lines(path), 1):
        if needle in text:
            return index
    raise AssertionError(f"{needle!r} is not in {path.name}; the fixture and the test disagree")


class TestThePositiveSideIsClean:
    def test_the_governance_fixture_is_authorised_by_position(self, fixture_analysis: tuple[list, list]) -> None:
        _, calls = fixture_analysis
        governance = [call for call in calls if call.verdict == "governance"]
        assert governance
        assert all("governance/" in call.path for call in governance)

    def test_the_real_backend_tree_is_clean(self) -> None:
        """The check must pass on the tree as it stands, or it is not a gate."""
        assert CHECK.run_python_half(BACKEND_SRC, quiet=True) == 0

    def test_the_real_backend_tree_has_a_non_empty_primitive_set(self) -> None:
        """The reason leaf 7.3 waited for 7.5 and 7.6: before them this was zero."""
        primitives = CHECK.discover_primitives(BACKEND_SRC)
        assert primitives, "the primitive set is empty; the check would fail by its own rule"
        assert any(primitive.name == "append" and primitive.owner == "AuditWriter" for primitive in primitives)


class TestTheVacuityGuards:
    def test_an_empty_primitive_set_fails(self, tmp_path: Path) -> None:
        """§2.2.1's explicit rule: a renamed decorator must not make the check pass."""
        (tmp_path / "nothing.py").write_text("def plain() -> None:\n    pass\n", encoding="utf-8")
        assert CHECK.run_python_half(tmp_path, quiet=True) == 1

    def test_a_renamed_decorator_is_what_that_guard_catches(self, tmp_path: Path) -> None:
        """The concrete scenario, not just the empty directory."""
        (tmp_path / "renamed.py").write_text(
            "def state_changing(func):\n    return func\n\n\n@state_changing\ndef writes() -> None:\n    pass\n",
            encoding="utf-8",
        )
        assert CHECK.run_python_half(tmp_path, quiet=True) == 1

    def test_an_empty_import_graph_raises(self) -> None:
        with pytest.raises(RuntimeError, match="import graph is empty"):
            CHECK.classify_importers({})

    def test_a_graph_without_the_boundary_package_raises(self) -> None:
        """A moved or deleted boundary must not read as "nothing imports it"."""
        with pytest.raises(RuntimeError, match="does not contain"):
            CHECK.classify_importers({"example.com/x": ["example.com/y"]})


class TestTheGoHalf:
    def test_an_outside_importer_is_reported(self) -> None:
        """The Go half's negative control, over a synthetic graph.

        `go list ./...` skips `testdata`, so the real compile-time fixture
        (`agent/testdata/chokepoint/outsider`) is invisible to the query. The compile rule is
        proved by `mutate/boundary_test.go`; the CLASSIFICATION is proved here.
        """
        outsider = "github.com/parag8487/ForgeOps/agent/internal/session"
        graph = {
            CHECK.GO_MUTATE_PACKAGE: [],
            outsider: [CHECK.GO_MUTATE_PACKAGE],
        }
        importers, offenders = CHECK.classify_importers(graph)
        assert [entry.importer for entry in importers] == [outsider]
        assert offenders == [f"package {outsider} imports executor/internal/mutate"]

    def test_an_importer_inside_the_executor_subtree_is_permitted(self) -> None:
        insider = "github.com/parag8487/ForgeOps/agent/internal/executor"
        graph = {CHECK.GO_MUTATE_PACKAGE: [], insider: [CHECK.GO_MUTATE_PACKAGE]}
        importers, offenders = CHECK.classify_importers(graph)
        assert offenders == []
        assert importers and importers[0].permitted

    def test_the_boundary_package_path_is_the_one_the_agent_actually_has(self) -> None:
        """A constant naming a package that does not exist would ban nothing.

        The same vacuity trap D-60 closed for the banned-api table, applied here.
        """
        expected = AGENT_ROOT / "internal" / "executor" / "internal" / "mutate"
        assert expected.is_dir()
        assert CHECK.GO_MUTATE_PACKAGE.endswith("agent/internal/executor/internal/mutate")
        assert CHECK.GO_EXECUTOR_PREFIX.endswith("agent/internal/executor")


class TestTheEntryPointExists:
    def test_the_shell_script_is_present_and_documents_both_halves(self) -> None:
        script = REPO_ROOT / "scripts" / "check-chokepoint.sh"
        assert script.is_file()
        text = script.read_text(encoding="utf-8")
        assert "--python" in text and "--go" in text
        assert "chokepoint_graph.py" in text

    def test_the_check_still_speaks_under_a_cp1252_console(self) -> None:
        """Run it for real with a legacy encoding and assert it still produces output.

        Found the hard way while building this check: with `PYTHONIOENCODING` unset on Windows,
        a section sign in a message made `print` raise inside the writer thread, and the process
        exited **1 with no output at all** — a check that fails for a reason nobody can read.
        Leaf 7.6 hit the same wall with an em dash in `verify-chain`'s output.

        A source grep for non-ASCII would not do: the module's docstrings legitimately quote
        design sections, and only what is *printed* matters. So this drives the real entry point
        with the encoding that broke it and asserts the verdict came through.
        """
        import os
        import subprocess

        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "cp1252"
        environment.pop("PYTHONUTF8", None)
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--half", "python", "--src", str(BACKEND_SRC)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "check-chokepoint(python): OK" in result.stdout, (
            f"the check produced no readable verdict under cp1252.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

    def test_the_empty_set_failure_is_readable_under_a_cp1252_console_too(self, tmp_path: Path) -> None:
        """The failure path as well as the success path, because that is the one that matters."""
        import os
        import subprocess

        (tmp_path / "nothing.py").write_text("x = 1\n", encoding="utf-8")
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "cp1252"
        environment.pop("PYTHONUTF8", None)
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--half", "python", "--src", str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert result.returncode == 1
        assert "EMPTY" in result.stderr, f"stderr={result.stderr!r}"
