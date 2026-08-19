# SPDX-License-Identifier: FSL-1.1-ALv2
"""The mutation manifest must describe every Appendix B property, and its counters must
match the rows that actually exist.

WHY THIS FILE EXISTS
--------------------
The `Mutation Testing CI Pipeline` workflow used to consist of exactly one substantive
step::

    python -c "import tomllib; data = tomllib.load(open('mutations.toml','rb')); \
               assert data['mutations']['completed_count'] == 31"

That is pattern B in its purest form: a green gate that inspects a hand-written integer in
a TOML file and calls it mutation testing. It ran no mutation, executed no property, and
opened no other file. It passed in 11 seconds on every commit, including the commits where
the real manifest carried 14 rows and the integer said 31.

So the number was wrong by 17 and nothing could tell. This gate reads the real manifest,
counts what is in it, checks the files each row points at, and compares the result against
Appendix B's declared property set. It is deliberately cheap -- it applies no mutation and
runs no test, because `scripts/mutation-harness.py` does that and needs a Go toolchain, a
database and a Redis. What it does is make the COUNTERS unable to lie, which is the exact
failure that let "31 of 31 mutations complete" survive in the record.

WHAT IT CHECKS
--------------
1. Every `[Q-NN]` row in the real manifest carries the keys its runtime requires.
2. Every file a row references exists: the property test, and the Go overlay/original pair.
3. The root `mutations.toml` counters agree with the real manifest: `total_count` equals the
   Appendix B property count, and `completed_count` equals the number of rows present.
4. Every Appendix B property has a row. A property with no negative control is reported by
   name, because "a property that cannot fail is not a property" (design Appendix B) and an
   uncontrolled property is indistinguishable from one that always passes.

Usage:
    python scripts/check-mutation-manifest.py
    python scripts/check-mutation-manifest.py --allow-missing-controls  # report, do not fail
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The real manifest, the one the harness reads. The root `mutations.toml` holds only
#: counters; keeping them in two files is pre-existing, so this gate reconciles them rather
#: than quietly preferring one.
REAL_MANIFEST = REPO_ROOT / "backend" / "tests" / "mutation" / "mutations.toml"
ROOT_COUNTERS = REPO_ROOT / "mutations.toml"
DESIGN = REPO_ROOT / ".antigravity" / "specs" / "phase-1-mvp-core" / "design.md"

#: Appendix B declares Q-01 … Q-31. The count is asserted against the design document
#: rather than restated, because a restated total is the thing that rotted last time.
APPENDIX_B_HEADING = "## Appendix B — Correctness Properties for Property-Based Testing"


def appendix_b_properties() -> set[str]:
    """Every `Q-NN` id Appendix B defines, read out of design.md."""
    if not DESIGN.is_file():
        raise SystemExit(f"design.md not found: {DESIGN}")
    text = DESIGN.read_text(encoding="utf-8")
    start = text.find(APPENDIX_B_HEADING)
    if start == -1:
        raise SystemExit(f"Appendix B heading not found in {DESIGN}")
    # Appendix B runs until the next top-level appendix heading.
    end = text.find("\n## Appendix C", start)
    section = text[start : end if end != -1 else len(text)]
    return set(re.findall(r"\bQ-(\d{2})\b", section))


def manifest_rows() -> dict[str, dict]:
    """The `[Q-NN]` tables of the real manifest, keyed by bare number."""
    if not REAL_MANIFEST.is_file():
        raise SystemExit(f"mutation manifest not found: {REAL_MANIFEST}")
    with REAL_MANIFEST.open("rb") as handle:
        data = tomllib.load(handle)
    rows: dict[str, dict] = {}
    for key, value in data.items():
        match = re.fullmatch(r"Q-(\d{2})", key)
        if match and isinstance(value, dict):
            rows[match.group(1)] = value
    return rows


def check_row(qid: str, row: dict) -> list[str]:
    """Structural and on-disk problems with one row."""
    problems: list[str] = []
    runtime = row.get("runtime")
    if runtime not in {"python", "go"}:
        problems.append(f"Q-{qid}: runtime must be 'python' or 'go', got {runtime!r}")
        return problems

    for required in ("property", "mutation", "description"):
        if not str(row.get(required, "")).strip():
            problems.append(f"Q-{qid}: missing or empty `{required}`")

    prop = row.get("property")
    if prop and not (REPO_ROOT / prop).is_file():
        problems.append(f"Q-{qid}: property file does not exist: {prop}")

    if runtime == "python":
        if not str(row.get("patch", "")).strip():
            problems.append(f"Q-{qid}: a python row needs a `patch`")
    else:
        for required in ("module_dir", "package", "test_run", "original", "overlay"):
            if not str(row.get(required, "")).strip():
                problems.append(f"Q-{qid}: a go row needs `{required}`")
        for key in ("original", "overlay"):
            ref = row.get(key)
            if ref and not (REPO_ROOT / ref).is_file():
                problems.append(f"Q-{qid}: `{key}` does not exist: {ref}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-missing-controls",
        action="store_true",
        help="report uncontrolled Appendix B properties without failing on them",
    )
    args = parser.parse_args(argv)

    declared = appendix_b_properties()
    rows = manifest_rows()
    print(f"==> Appendix B declares {len(declared)} properties")
    print(f"==> {REAL_MANIFEST.relative_to(REPO_ROOT).as_posix()} carries {len(rows)} rows")

    failures: list[str] = []

    for qid in sorted(rows):
        failures.extend(check_row(qid, rows[qid]))
    if not failures:
        print(f"ok:   all {len(rows)} rows are structurally complete and their files exist")

    # ── the counters must not be able to lie ──────────────────────────────────
    if not ROOT_COUNTERS.is_file():
        failures.append(f"root counter file not found: {ROOT_COUNTERS}")
    else:
        with ROOT_COUNTERS.open("rb") as handle:
            counters = tomllib.load(handle).get("mutations", {})
        total = counters.get("total_count")
        completed = counters.get("completed_count")
        if total != len(declared):
            failures.append(
                f"mutations.toml total_count is {total} but Appendix B declares {len(declared)}"
            )
        else:
            print(f"ok:   total_count {total} matches Appendix B")
        if completed != len(rows):
            failures.append(
                f"mutations.toml completed_count is {completed} but the manifest carries "
                f"{len(rows)} row(s) -- this is the counter that read 31 while 14 existed"
            )
        else:
            print(f"ok:   completed_count {completed} matches the rows that exist")

    # ── every declared property needs a control ───────────────────────────────
    missing = sorted(declared - set(rows))
    if missing:
        names = ", ".join(f"Q-{m}" for m in missing)
        message = (
            f"{len(missing)} Appendix B propert{'y' if len(missing) == 1 else 'ies'} have no "
            f"negative control: {names}"
        )
        if args.allow_missing_controls:
            print(f"WARN: {message}")
        else:
            failures.append(message)
    else:
        print(f"ok:   every one of the {len(declared)} declared properties has a control")

    if failures:
        print("\nFAIL: the mutation manifest does not describe Appendix B", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nA property with no negative control cannot be shown to fail, so it is not\n"
            "evidence of anything. Add its row to\n"
            f"{REAL_MANIFEST.relative_to(REPO_ROOT).as_posix()} with the mutation that breaks it.",
            file=sys.stderr,
        )
        return 1

    print("\ncheck-mutation-manifest: the manifest describes Appendix B and the counters agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
