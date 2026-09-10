#!/usr/bin/env python
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every test the whole tree collects must be collected by exactly one shard.

WHY THIS EXISTS. `ci.yml` splits the backend suite into three matrix jobs — `unit`, `meta`,
`integration` — and the coverage gate is enforced in `backend-coverage` over their combined data.
Two guards already stood there: `test_shard_paths_cover_every_test_directory` asserts the union of
shard PATHS covers every test directory, and the combine job refuses to report when fewer than three
coverage files arrive.

Neither notices a shard that collects FEWER TESTS THAN ITS PATHS CONTAIN. A directory can be in a
shard's path list while a collection error, a stray `pytest.ini` setting, a renamed marker or a
broken import silently drops half the files inside it. The union-of-paths assertion still passes, the
three coverage files still arrive, the gate is still computed — over a suite that quietly shrank.

THE FAILURE THIS WAS WRITTEN AFTER was the same shape one layer up: a report presented
`tests/unit` plus five hand-picked integration files — 1,722 of 3,135 collected tests — as the whole
backend suite, with the coverage measured over that subset quoted against the whole-suite gate. A
number nobody could check without re-running everything. This makes the arithmetic mechanical: the
shards must sum to the tree, and the script says the numbers out loud so a report can quote them.

WHAT IT DOES NOT DO. It does not run the tests, so it is fast and needs no database. Collection is
enough to establish that no test is unreachable from the shard split, which is the property the
coverage gate depends on.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: The shard definitions, which MUST match `ci.yml`'s `strategy.matrix.include`.
#:
#: Duplicated rather than parsed out of the workflow, and the duplication is checked: `--verify-ci`
#: reads `ci.yml` and fails when the two disagree. Parsing YAML to discover the split would make this
#: script depend on the workflow's shape; asserting the two agree keeps one authority and no drift.
SHARDS: dict[str, tuple[str, ...]] = {
    "unit": ("tests/unit", "tests/property", "tests/generation", "tests/secrets"),
    "meta": ("tests/meta",),
    "integration": ("tests/integration",),
}

#: The marker selection `ci.yml` runs the suite under.
MARKER_EXPR = "not oidc and not infisical"

BACKEND = Path(__file__).resolve().parent.parent / "backend"

_COLLECTED = re.compile(r"(\d+)(?:/(\d+))? tests? collected(?: \((\d+) deselected\))?")


def collect(paths: tuple[str, ...]) -> int:
    """How many tests pytest collects under `paths` with CI's marker selection."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            *paths,
            "-m",
            MARKER_EXPR,
            "--collect-only",
            "-q",
            "--no-cov",
            "-p",
            "no:randomly",
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    match = None
    for match in _COLLECTED.finditer(combined):  # noqa: B007 - the LAST match is the summary
        pass
    if match is None:
        print(f"FAILED: pytest reported no collection summary for {' '.join(paths)}", file=sys.stderr)
        print(combined[-4000:], file=sys.stderr)
        raise SystemExit(1)
    # `N/M tests collected (D deselected)` on a filtered run, `N tests collected` otherwise. The
    # SELECTED count is the first group in both forms.
    return int(match.group(1))


def verify_against_ci() -> None:
    """Fail when `ci.yml`'s shard paths and this script's copy disagree."""
    workflow = (BACKEND.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for shard, paths in SHARDS.items():
        expected = f"paths: {' '.join(paths)}"
        if expected not in workflow:
            print(
                f"FAILED: ci.yml does not contain `{expected}` for shard {shard!r}; the split has "
                f"changed and this script's copy is stale",
                file=sys.stderr,
            )
            raise SystemExit(1)
    # A shard added to the workflow but not here would make the sum pass while leaving a shard
    # unaccounted for, so the count is asserted too.
    declared = len(re.findall(r"^\s*- shard: ", workflow, flags=re.MULTILINE))
    if declared != len(SHARDS):
        print(
            f"FAILED: ci.yml declares {declared} shard(s) but this script knows {len(SHARDS)}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"ok:   ci.yml and this script agree on {len(SHARDS)} shards and their paths")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-ci",
        action="store_true",
        help="also assert ci.yml's shard definitions match this script's copy",
    )
    args = parser.parse_args()

    if args.verify_ci:
        verify_against_ci()

    whole = collect(("tests",))
    print(f"      whole tree collects: {whole}")

    total = 0
    for shard, paths in SHARDS.items():
        n = collect(paths)
        total += n
        print(f"      shard {shard:<12} collects: {n:>5}   ({' '.join(paths)})")
        if n == 0:
            print(
                f"FAILED: shard {shard!r} collects nothing; its tests would never run and the "
                f"coverage gate would be computed over a suite missing them",
                file=sys.stderr,
            )
            return 1

    print(f"      shards sum to:       {total}")

    if total != whole:
        print(
            f"FAILED: the shards collect {total} tests but the tree collects {whole}. "
            f"{'Some tests are in no shard and never run.' if total < whole else 'Some tests are in more than one shard and are counted twice.'} "
            f"Reconcile `SHARDS` here with `ci.yml`'s matrix.",
            file=sys.stderr,
        )
        return 1

    print(f"ok:   the three shards account for all {whole} collected tests, with no overlap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
