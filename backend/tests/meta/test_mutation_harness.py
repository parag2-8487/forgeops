# SPDX-License-Identifier: FSL-1.1-ALv2
"""The mutation harness's own tests (design.md §0.4.5).

Three properties of the harness are asserted, because all three are load-bearing
and none is self-evident:

1. a property that its control genuinely breaks is reported `OK`;
2. a property the control cannot break is reported `VACUOUS` and fails the run —
   this is P-09's situation, and catching it is the entire point;
3. the harness works from a temp directory **outside** the repository and leaves
   `git status --porcelain` empty.

Clause 3 is the one the hard gate names explicitly. A harness that mutated a
tracked file would leave the tree dirty for whatever ran next, and "the mutation
was reverted afterwards" is a promise; an assertion is evidence.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "mutation-harness.py"
FIXTURE_DIR = "backend/tests/meta/fixtures/mutation"

#: Installs a `step` that never decrements. The healthy property must then fail.
_NO_DECREMENT_PATCH = """
from tests.meta.fixtures.mutation import subject
subject.step = lambda remaining: remaining
"""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("forgeops_mutation_harness", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HARNESS = _load()


def _manifest(tmp_path: Path, ident: str, property_file: str, patch: str) -> Path:
    path = tmp_path / "mutations.toml"
    path.write_text(
        textwrap.dedent(
            f"""\
            [{ident}]
            runtime     = "python"
            property    = "{FIXTURE_DIR}/{property_file}"
            target      = "tests.meta.fixtures.mutation.subject.step"
            mutation    = "make step return its input without decrementing"
            description = "removes the decrement that guarantees termination"
            patch = '''
{textwrap.indent(patch.strip(), "            ")}
            '''
            """
        ),
        encoding="utf-8",
    )
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


class TestAHealthyPropertyIsAccepted:
    def test_a_property_its_control_breaks_is_reported_ok(self, tmp_path: Path) -> None:
        # --skip-git-check because a developer's working tree is legitimately dirty
        # mid-task. The clean-tree clause is asserted on its own below, as a
        # before/after delta, so it stays proven without making every other meta
        # test depend on the state of the checkout.
        manifest = _manifest(tmp_path, "Q-99", "test_healthy_property.py", _NO_DECREMENT_PATCH)
        result = _run("--manifest", str(manifest), "--all", "--skip-git-check")
        combined = result.stdout + result.stderr
        assert "VACUOUS" not in combined, combined
        assert result.returncode == 0, combined
        assert "FAIL      OK" in result.stdout, combined


class TestAVacuousPropertyIsRejected:
    def test_a_property_its_control_cannot_break_is_reported_vacuous(self, tmp_path: Path) -> None:
        """The clause §0.4.5 exists for. P-09 in miniature."""
        manifest = _manifest(tmp_path, "Q-98", "test_vacuous_property.py", _NO_DECREMENT_PATCH)
        result = _run("--manifest", str(manifest), "--all", "--skip-git-check")
        combined = result.stdout + result.stderr
        assert result.returncode == 1, combined
        assert "VACUOUS" in result.stdout, combined
        assert "Q-98" in result.stdout, combined

    def test_a_prose_only_control_is_refused_at_load_time(self, tmp_path: Path) -> None:
        """A control with no executable patch is what made P-09 look tested."""
        manifest = tmp_path / "mutations.toml"
        manifest.write_text(
            textwrap.dedent(
                f"""\
                [Q-97]
                property    = "{FIXTURE_DIR}/test_healthy_property.py"
                mutation    = "described in prose only"
                description = "no patch, so nothing is actually mutated"
                """
            ),
            encoding="utf-8",
        )
        result = _run("--manifest", str(manifest), "--all")
        assert result.returncode != 0
        assert "no `patch`" in result.stdout + result.stderr


class TestASkippedControlRunIsAnErrorNotAVacuousProperty:
    """A run in which everything skipped also exits 0 (leaf 7.8).

    Reporting that as `VACUOUS` says "the property survived its own control", which is false and
    sends a reader looking for a decorative property instead of a missing service. It happened:
    Q-04's row was reported VACUOUS because the harness had been invoked without
    `FORGEOPS_TEST_DATABASE_URL`, so every database-backed clause skipped.

    §0.4.4 forbids silent skips in the mandatory selection; this is the same rule applied to the
    control's own run.
    """

    @pytest.mark.parametrize(
        "output",
        [
            "ssssssss                                    [100%]\n8 skipped in 0.15s",
            "s                                           [100%]\n1 skipped, 0 passed in 0.10s",
            "no tests ran in 0.01s",
            "collected 0 items\n\nno tests ran in 0.02s",
        ],
    )
    def test_a_run_that_executed_nothing_is_detected(self, output: str) -> None:
        assert HARNESS._nothing_actually_ran(output) is True, output  # noqa: SLF001 - the unit under test

    @pytest.mark.parametrize(
        "output",
        [
            "........                                    [100%]\n8 passed in 1.2s",
            "..s.....                                    [100%]\n7 passed, 1 skipped in 1.2s",
            "..x.....                                    [100%]\n7 passed, 1 xpassed in 1.2s",
        ],
    )
    def test_a_run_that_executed_something_is_not_detected(self, output: str) -> None:
        """The other direction, or the guard would turn every healthy pass into an ERROR."""
        assert HARNESS._nothing_actually_ran(output) is False, output  # noqa: SLF001 - the unit under test


class TestTheGoArgvOrderIsLoadBearing:
    """`-rapid.nofailfile` must follow the package pattern (leaf 7.10).

    It is a flag of the TEST BINARY, not of `go test`. Placed before the package, `go test` stops
    parsing its own flags at the first one it does not recognise and treats the rest as a package
    list — so the pattern was consumed as a flag value, the command resolved to `.`, and the run
    died with `no Go files in <module>` / `FAIL . [setup failed]`. The harness read that non-zero
    exit as "failed as required" and reported the row healthy, so the control had never run.

    Q-01 was the first Go row in `mutations.toml`, which is why nothing had noticed.
    """

    def test_the_source_places_the_binary_flag_after_the_package(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        append_package = source.index('argv.append(row.package or "./...")')
        append_flag = source.index('argv.append("-rapid.nofailfile")')
        assert append_package < append_flag, (
            "-rapid.nofailfile must be appended AFTER the package pattern, or go test consumes "
            "the pattern as a flag value and the mutated build never runs"
        )

    def test_the_nofailfile_flag_is_not_in_the_go_test_flag_list(self) -> None:
        """Asserted on the constructed list rather than by reading the comment above it."""
        source = SCRIPT.read_text(encoding="utf-8")
        construction = source[source.index('argv = ["go", "test"') :]
        first_line = construction.splitlines()[0]
        assert "rapid.nofailfile" not in first_line, first_line

    @pytest.mark.parametrize(
        "shape",
        ["build failed", "cannot find", "syntax error", "setup failed", "no Go files in"],
    )
    def test_every_build_failure_shape_is_reported_as_an_error(self, shape: str) -> None:
        """A mutated build that did not run must never be reported as a healthy failure.

        `setup failed` and `no Go files in` were both produced by the argv defect above, and
        neither was in the guard — which is how the broken run reported OK.
        """
        source = SCRIPT.read_text(encoding="utf-8")
        assert f'"{shape}"' in source, f"{shape!r} is not among the shapes the Go guard reports as ERROR"


class TestTheHarnessLeavesNoTrace:
    def test_the_temp_directory_is_outside_the_repository(self) -> None:
        tmp = HARNESS.make_outside_tempdir()
        try:
            resolved = tmp.resolve()
            assert REPO_ROOT.resolve() not in resolved.parents
            assert resolved != REPO_ROOT.resolve()
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_tmpdir_inside_the_repository_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The temp location is caller-controlled, so it is verified, not trusted.

        `tempfile.gettempdir()` caches its answer, so setting TMPDIR alone would
        not change where `mkdtemp` writes. The cached value is what has to move.
        """
        import shutil
        import tempfile

        inside = REPO_ROOT / "backend" / ".mutation-tmp-probe"
        inside.mkdir(parents=True, exist_ok=True)
        try:
            monkeypatch.setattr(tempfile, "tempdir", str(inside))
            with pytest.raises(SystemExit) as excinfo:
                HARNESS.make_outside_tempdir()
            assert "inside the repository" in str(excinfo.value)
        finally:
            shutil.rmtree(inside, ignore_errors=True)

    def test_the_working_tree_is_clean_after_a_run(self, tmp_path: Path) -> None:
        manifest = _manifest(tmp_path, "Q-96", "test_healthy_property.py", _NO_DECREMENT_PATCH)
        before = HARNESS.git_status_is_clean()
        _run("--manifest", str(manifest), "--all")
        after = HARNESS.git_status_is_clean()
        assert after[0] == before[0], f"the harness changed the working tree: {after[1]}"


class TestAppendixBCompleteness:
    def test_the_authority_is_read_from_the_design_document(self) -> None:
        """Restating the id list here would let the two drift silently."""
        ids = HARNESS.appendix_b_ids()
        assert len(ids) == 31, ids
        assert ids[0] == "Q-01"
        assert ids[-1] == "Q-31"

    def test_the_real_manifest_covers_every_appendix_b_property(self) -> None:
        """Every Appendix B property has a row. This replaces two tests that asserted the negation.

        `test_a_missing_row_fails_the_real_manifest_run` asserted `--all` on the real manifest exits
        1, with the docstring "Until every property lands, `--all` on the real manifest must fail",
        and its sibling asserted the string "INCOMPLETE (allowed)" appeared. Both encoded the
        repository's own incompleteness as an expectation, so both broke the moment leaf 19.2 landed
        the last of the 31 controls — passing only while work was outstanding is the opposite of what
        a regression test is for.

        Neither could be salvaged by pointing at a fixture: the completeness clause in
        `mutation-harness.py` is guarded by `if args.manifest == MUTATIONS_TOML`, deliberately, so a
        constructed shortfall is not reachable through `--manifest`. What remains worth asserting is
        the invariant itself, and it is read from the manifest rather than by executing it — running
        31 mutations to answer a coverage question would put a quarter of an hour inside a meta test.

        `scripts/check-mutation-manifest.py` enforces the same rule in CI, and
        `scripts/check-progress.sh` refuses to let Phase 1 read `completed` while that check fails.
        """
        declared = set(HARNESS.appendix_b_ids())
        present = set(HARNESS.load_rows(HARNESS.MUTATIONS_TOML))
        missing = sorted(declared - present)
        assert not missing, (
            f"{len(missing)} Appendix B propert{'y' if len(missing) == 1 else 'ies'} have no row in "
            f"the real manifest: {', '.join(missing)}. A property with no negative control cannot be "
            "shown to fail, so it is not evidence."
        )

    def test_allow_incomplete_cannot_hide_a_vacuous_row(self, tmp_path: Path) -> None:
        """`--allow-incomplete` suppresses the completeness clause and nothing else.

        This is the load-bearing half of the flag's contract and the half that survives the manifest
        being complete: a flag that also swallowed a VACUOUS row would let a property that cannot
        fail be reported as covered, which is the whole failure mode the harness exists to prevent.
        """
        manifest = _manifest(tmp_path, "Q-98", "test_vacuous_property.py", _NO_DECREMENT_PATCH)
        result = _run("--manifest", str(manifest), "--all", "--allow-incomplete", "--skip-git-check")
        combined = result.stdout + result.stderr
        assert result.returncode == 1, combined
        assert "VACUOUS" in result.stdout, combined

    def test_the_clean_tree_clause_is_enabled_in_ci(self) -> None:
        """`--skip-git-check` is a test affordance and must never reach CI."""
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "--skip-git-check" not in workflow
