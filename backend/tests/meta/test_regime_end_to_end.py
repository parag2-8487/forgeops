# SPDX-License-Identifier: FSL-1.1-ALv2
"""The §0.4 regime, exercised end to end (design.md §0.4, §8.3, §8.4).

Tasks 1.1-1.7 each ship their own tests. This file is the seam between them, and it
asserts the three things no individual check can assert about itself:

1. **every** check passes on the real tree and fails on its negative fixture, run
   together rather than one at a time — a lint whose failure path has never fired is
   not a lint;
2. no check **writes** to the repository while running, so the regime cannot be the
   thing that dirties the tree it polices;
3. the four authoritative root documents stay outside every mutating pre-commit hook
   while remaining inside Gitleaks — the contract §0.3 states and
   `scripts/check-hygiene.sh` enforces, restated here so a hook added in a later
   group cannot quietly break it.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

AUTHORITATIVE_DOCUMENTS = (
    "AI-Powered-DevOps-Platform-Complete-Technical-Research.md",
    "PRD.md",
    "Tech-Stack-Analysis.md",
    "phases.md",
)


def _python(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


# ── clause 1: every check fires in both directions ───────────────────────────

#: (name, argv-that-must-pass, argv-that-must-fail).
#:
#: Task 1.4's Go checker is exercised by its own `go test` suite rather than from
#: here, because invoking the Go toolchain inside a pytest run would make this file
#: depend on a toolchain the `backend` job does not install.
REGIME_CHECKS: tuple[tuple[str, list[str], list[str]], ...] = (
    (
        "1.2 call-site inventory",
        [str(SCRIPTS / "collect_call_sites.py")],
        # No negative argv: emptiness is the only failure mode, and it is asserted
        # directly by test_contract_conformance.py's INVENTORY_FLOOR.
        [],
    ),
    (
        "1.3 test doubles",
        [str(SCRIPTS / "check-test-doubles.py"), "backend/tests"],
        [str(SCRIPTS / "check-test-doubles.py"), str(FIXTURES / "bad_double.py")],
    ),
    (
        "1.5 no silent skips",
        [str(SCRIPTS / "check-no-skips.py"), str(FIXTURES / "noskips" / "clean.jsonl")],
        [str(SCRIPTS / "check-no-skips.py"), str(FIXTURES / "noskips" / "skipped.jsonl")],
    ),
    (
        "1.6 mutation harness",
        [str(SCRIPTS / "mutation-harness.py"), "--all", "--allow-incomplete", "--skip-git-check"],
        [
            str(SCRIPTS / "mutation-harness.py"),
            "--manifest",
            str(FIXTURES / "mutation" / "vacuous-manifest.toml"),
            "--all",
            "--skip-git-check",
        ],
    ),
    (
        "1.7 CI job existence",
        [str(SCRIPTS / "check-ci-jobs.py")],
        [
            str(SCRIPTS / "check-ci-jobs.py"),
            str(FIXTURES / "ci" / "workflow-missing-job.yml"),
            str(FIXTURES / "ci" / "design-excerpt.md"),
            "--no-baseline",
        ],
    ),
)


#: Only the rows that have a negative argv. Filtering rather than skipping is
#: deliberate: this module carries the `mandatory` marker, and
#: `scripts/check-no-skips.py` fails the build on any skip in that selection. A
#: `pytest.skip` here would make the regime violate its own §0.4.4 clause.
NEGATIVE_CHECKS = tuple(row for row in REGIME_CHECKS if row[2])


@pytest.mark.parametrize("name,passing,_failing", REGIME_CHECKS, ids=[c[0] for c in REGIME_CHECKS])
def test_every_check_passes_on_the_real_tree(name: str, passing: list[str], _failing: list[str]) -> None:
    result = _python(*passing)
    assert result.returncode == 0, f"{name} failed on the real tree:\n{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize("name,_passing,failing", NEGATIVE_CHECKS, ids=[c[0] for c in NEGATIVE_CHECKS])
def test_every_check_fails_on_its_negative_fixture(name: str, _passing: list[str], failing: list[str]) -> None:
    result = _python(*failing)
    assert result.returncode == 1, (
        f"{name} did NOT fail on its negative fixture. A lint whose failure path has "
        f"never fired is not a lint.\n{result.stdout}\n{result.stderr}"
    )


def test_every_check_without_a_negative_argv_is_accounted_for() -> None:
    """No check may quietly lack a negative fixture.

    The one exception is task 1.2's collector, whose only failure mode is an empty
    inventory; that is asserted directly by
    `tests/unit/test_contract_conformance.py::test_inventory_is_not_empty_and_grows_with_the_code`
    against the committed `INVENTORY_FLOOR`. Naming it here means a second
    unexplained exception cannot appear without this assertion failing.
    """
    without = {row[0] for row in REGIME_CHECKS if not row[2]}
    assert without == {"1.2 call-site inventory"}, sorted(without)


# ── clause 2: no check writes to the repository ──────────────────────────────


def _tracked_files() -> list[Path]:
    listing = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(REPO_ROOT),
    )
    return [REPO_ROOT / name for name in listing.stdout.split("\0") if name]


def _digest_tree() -> str:
    """One digest over every tracked file's path and content."""
    accumulator = hashlib.sha256()
    for path in sorted(_tracked_files()):
        accumulator.update(path.as_posix().encode("utf-8"))
        accumulator.update(b"\0")
        if path.is_file():
            accumulator.update(hashlib.sha256(path.read_bytes()).digest())
    return accumulator.hexdigest()


def _all_passing_invocations() -> Iterator[tuple[str, list[str]]]:
    for name, passing, _ in REGIME_CHECKS:
        yield name, passing


def test_no_check_modifies_a_tracked_file() -> None:
    """The regime must not be what dirties the tree it polices.

    §0.4.5 requires the mutation harness to mutate from outside the repository; this
    generalises the guarantee to every check and verifies it by content rather than
    trusting each script's intentions.
    """
    before = _digest_tree()
    for name, argv in _all_passing_invocations():
        result = _python(*argv)
        assert result.returncode == 0, f"{name}: {result.stdout}\n{result.stderr}"
    after = _digest_tree()
    assert before == after, "a regime check modified a tracked file while running"


def test_no_check_leaves_untracked_files_behind() -> None:
    """A check that dropped an artifact would fail the harness's clean-tree clause."""

    def untracked() -> set[str]:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(REPO_ROOT),
        )
        return {line[3:] for line in result.stdout.splitlines() if line.startswith("??")}

    before = untracked()
    for _name, argv in _all_passing_invocations():
        _python(*argv)
    assert untracked() == before


# ── clause 3: the authoritative documents' hook contract ─────────────────────


def _pre_commit_config() -> dict:
    return yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))


def test_the_four_authoritative_documents_are_excluded_from_mutating_hooks() -> None:
    """They are read-only inputs (design.md §0.3); no hook may rewrite them."""
    config = _pre_commit_config()
    exclude = config.get("exclude", "")
    for document in AUTHORITATIVE_DOCUMENTS:
        stem = document.replace(".", r"\.")
        assert stem in exclude, f"{document} is not in the top-level pre-commit exclude"


def test_gitleaks_still_scans_them() -> None:
    """The exclusion must not become a hole in secret scanning.

    Gitleaks is structurally immune to the top-level exclusion because it takes no
    filenames. That is the property being asserted, rather than the mere presence of
    the hook.
    """
    config = _pre_commit_config()
    hooks = [hook for repo in config["repos"] for hook in repo.get("hooks", [])]
    gitleaks = [hook for hook in hooks if hook.get("id") == "gitleaks"]
    assert gitleaks, "the gitleaks hook is gone"
    for hook in gitleaks:
        assert hook.get("pass_filenames") is False, "gitleaks must take no filenames"
        assert hook.get("always_run") is True, "gitleaks must run unconditionally"


def test_the_new_regime_hooks_are_read_only() -> None:
    """Every §0.4 / §8.4 hook reads; none passes a rewriting flag.

    These take fixed arguments rather than a filename list, so they run with
    `pass_filenames: false` — which means the top-level four-document exclusion does
    not apply to them. `scripts/check-hygiene.sh` grants that exemption only to a
    documented list, and this assertion is the evidence the exemption rests on.

    `check-chokepoint` joined the list with leaf 7.3. Reachability is a property of the
    whole import and call graph, so it cannot be answered from a filename list at all;
    it parses `backend/src/**` and runs `go list -deps -json`, and writes nothing.
    """
    config = _pre_commit_config()
    expected = {
        "check-test-doubles",
        "check-ci-jobs",
        "check-no-latest",
        "check-gitleaks-config",
        "check-chokepoint",
    }
    hooks = {
        hook["id"]: hook for repo in config["repos"] for hook in repo.get("hooks", []) if hook.get("id") in expected
    }
    assert set(hooks) == expected, sorted(hooks)
    for identifier, hook in hooks.items():
        entry = hook.get("entry", "")
        for rewriting in ("--fix", "-w", "--write", "--in-place"):
            assert rewriting not in entry.split(), f"{identifier} passes a rewriting flag: {entry}"


def test_the_authoritative_documents_are_unmodified() -> None:
    """They must be byte-identical to their committed state at all times."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "status", "--porcelain", "--", *AUTHORITATIVE_DOCUMENTS],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(REPO_ROOT),
    )
    assert not result.stdout.strip(), f"an authoritative document was modified:\n{result.stdout}"
