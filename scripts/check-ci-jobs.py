#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every CI job Appendix E cites must actually exist (design.md §8.3, §15.10).

Why
---
Phase 0's Appendix E cited `build`, `test` and `lint` jobs that never existed. The
evidence column read like proof and named nothing real, and no tool disagreed. This
makes that a build failure.

Invocation
----------
    python scripts/check-ci-jobs.py .github/workflows/ci.yml \\
        .antigravity/specs/phase-1-mvp-core/design.md

Input
-----
The workflow's `jobs:` keys, and every **bold backticked** token inside Appendix E.
Appendix E marks a job citation as ``**`e2e`**`` — bold *and* code — while ordinary
code spans (`kubectl`, `vector(1536)`, `require_capability("postgres")`) and bold
property ids (**Q-17**) use only one of the two. That pairing is what makes the
extraction precise rather than a guess at which backticked token is a job name.

Failure condition
-----------------
Exit 1 naming any job Appendix E cites that the workflow does not define, and exit 1
when the extracted set is empty — a pattern that stopped matching would otherwise
pass forever, the same vacuity trap §0.4.5 closes for the mutation harness.

The staged baseline
-------------------
Six §8.3 jobs (`e2e`, `k8s`, `mutation`, `policy`, `secrets`, `templates`) are added
by task 19.3, so between now and then Appendix E legitimately cites jobs the
workflow has not grown yet. `scripts/ci-jobs-baseline.txt` lists exactly those, each
with the task that owns it. The list is not a suppression:

* a cited job that is neither defined nor baselined fails the check;
* a baselined job that IS now defined **also** fails the check, so the file cannot
  outlive its purpose and 19.3 is forced to empty it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "ci-jobs-baseline.txt"

#: A job citation in Appendix E: bold AND code, e.g. **`compose-smoke`**.
_JOB_CITATION = re.compile(r"\*\*`([a-z][a-z0-9-]*)`\*\*")

#: Appendix E starts at a heading containing "Appendix E" and ends at the next
#: top-level appendix heading, so the scan cannot drift into Appendix D or beyond.
_APPENDIX_E_START = re.compile(r"^#{1,3}\s+Appendix E\b", re.IGNORECASE)
_APPENDIX_START = re.compile(r"^#{1,3}\s+Appendix\s+[A-Z]\b", re.IGNORECASE)


def workflow_jobs(path: Path) -> set[str]:
    """The `jobs:` keys defined by a GitHub Actions workflow."""
    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "jobs" not in document:
        raise SystemExit(f"{path}: no top-level `jobs:` mapping")
    jobs = document["jobs"]
    if not isinstance(jobs, dict) or not jobs:
        raise SystemExit(f"{path}: `jobs:` is empty")
    return set(jobs)


def appendix_e_text(path: Path) -> str:
    """Just Appendix E, so a citation elsewhere in the design is not conflated."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = None
    for index, line in enumerate(lines):
        if _APPENDIX_E_START.match(line):
            start = index + 1
            break
    if start is None:
        raise SystemExit(f"{path}: no 'Appendix E' heading found")

    end = len(lines)
    for index in range(start, len(lines)):
        if _APPENDIX_START.match(lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def cited_jobs(path: Path) -> set[str]:
    return set(_JOB_CITATION.findall(appendix_e_text(path)))


def load_baseline(path: Path) -> dict[str, str]:
    """`{job: owning task}` from the staged-jobs file. Missing file means empty."""
    if not path.is_file():
        return {}
    baseline: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        job, _, owner = (part.strip() for part in line.partition(":"))
        if not job:
            continue
        if not owner:
            raise SystemExit(
                f"{path}: '{job}' has no owning task. Every staged job must name the "
                "task that adds it, e.g. 'e2e: task 18.3'."
            )
        baseline[job] = owner
    return baseline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert Appendix E cites only jobs that exist")
    parser.add_argument("workflow", type=Path, nargs="?", default=REPO_ROOT / ".github/workflows/ci.yml")
    parser.add_argument("design", type=Path, nargs="?", default=REPO_ROOT / ".antigravity/specs/phase-1-mvp-core/design.md")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--no-baseline", action="store_true", help="ignore the staged-jobs file entirely")
    args = parser.parse_args(argv)

    for label, path in (("workflow", args.workflow), ("design", args.design)):
        if not path.is_file():
            print(f"ERROR: {label} not found: {path}", file=sys.stderr)
            return 2

    defined = workflow_jobs(args.workflow)
    cited = cited_jobs(args.design)
    baseline = {} if args.no_baseline else load_baseline(args.baseline)

    if not cited:
        print(
            "ERROR: no job citations were extracted from Appendix E. The pattern is "
            "bold-and-backticked (**`e2e`**); if the document's convention changed, "
            "update the pattern rather than letting the check pass vacuously.",
            file=sys.stderr,
        )
        return 1

    print(f"check-ci-jobs: workflow defines {len(defined)} jobs; Appendix E cites {len(cited)}")

    failed = False

    undefined = sorted(cited - defined - set(baseline))
    if undefined:
        print(
            f"ERROR: Appendix E cites {len(undefined)} job(s) the workflow does not define: "
            f"{', '.join(undefined)}",
            file=sys.stderr,
        )
        print(
            "Phase 0's Appendix E cited `build`, `test` and `lint` jobs that never "
            "existed. Either add the job or record it in scripts/ci-jobs-baseline.txt "
            "with the task that owns it.",
            file=sys.stderr,
        )
        failed = True

    stale = sorted(set(baseline) & defined)
    if stale:
        print(
            f"ERROR: {len(stale)} job(s) are listed in {args.baseline.name} but the workflow "
            f"now defines them: {', '.join(stale)}. Remove those lines.",
            file=sys.stderr,
        )
        failed = True

    staged = sorted(set(baseline) - defined)
    if staged:
        print(f"staged (owned by a later task, not yet defined): {len(staged)}")
        for job in staged:
            print(f"  {job}  <- {baseline[job]}")

    if failed:
        return 1

    print("check-ci-jobs: every job Appendix E cites is defined or explicitly staged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
