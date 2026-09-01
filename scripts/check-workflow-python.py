#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every workflow runs the Python the project targets (design.md §8.3).

Why
---
`mutation-ci.yml` pinned `python-version: "3.11"` while every other workflow used 3.13 and
`backend/Dockerfile` builds on `python:3.13-slim`. Nothing compared them.

That was not a cosmetic inconsistency. `backend/tests/property/test_q19_route_coverage.py` uses
`sys.monitoring` to prove a protected handler's body never executed, and `sys.monitoring` arrived in
Python **3.12** — so under 3.11 four of its eleven tests raised
`AttributeError: module 'sys' has no attribute 'monitoring'` on every run, mutated or not. The mutation
harness saw a non-zero exit with a named failing test and read it as "the control bit". Only
`control-of-the-control.py` noticed, and only because it neutralises the mutation and demands the verdict
flip: it reported that Q-19's OK verdict was not attributable to its mutation, which was exactly right and
said nothing about why.

So a version skew in a runner definition silently disabled a negative control, and the thing that caught it
was a meta-check, three layers up, reporting a symptom. This makes the skew itself a build failure.

The authority is `backend/Dockerfile`, not a constant here: the image the backend actually ships on is the
version its tests must run under, and reading it means this gate cannot drift from the thing it checks.

Invocation
----------
    python scripts/check-workflow-python.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: `FROM python:3.13-slim AS runtime` — the runtime stage, which is what ships.
_BASE_IMAGE = re.compile(r"^FROM\s+python:(\d+\.\d+)(?:[.-][\w.-]+)?\s+AS\s+runtime\s*$", re.MULTILINE)

#: `python-version: "3.13"` or `python-version: ${{ env.PYTHON_VERSION }}`.
_PIN = re.compile(r"^\s*python-version:\s*(.+?)\s*$", re.MULTILINE)

#: `PYTHON_VERSION: 3.13` in a workflow's `env:` block.
_ENV_DECL = re.compile(r"^\s*PYTHON_VERSION:\s*[\"']?(\d+\.\d+)[\"']?\s*$", re.MULTILINE)


def target_version(dockerfile: Path = DOCKERFILE) -> str:
    """The Python the backend image runs, read from its own runtime stage."""
    if not dockerfile.is_file():
        raise SystemExit(f"backend Dockerfile not found: {dockerfile}")
    match = _BASE_IMAGE.search(dockerfile.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(
            f"{dockerfile} has no `FROM python:<version> AS runtime` line, so this gate cannot "
            "determine the version the project targets. It reads the Dockerfile rather than "
            "hardcoding a constant precisely so the two cannot drift."
        )
    return match.group(1)


def findings(target: str) -> list[str]:
    """Every workflow pin that disagrees with the target, as human-readable lines."""
    out: list[str] = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        text = workflow.read_text(encoding="utf-8")
        # A workflow-level `PYTHON_VERSION` is itself a pin and is checked directly, because every
        # `${{ env.PYTHON_VERSION }}` reference resolves to it.
        for match in _ENV_DECL.finditer(text):
            if match.group(1) != target:
                line = text[: match.start()].count("\n") + 1
                out.append(f"{workflow.name}:{line}: PYTHON_VERSION is {match.group(1)}, target is {target}")

        for match in _PIN.finditer(text):
            raw = match.group(1).strip().strip("\"'")
            line = text[: match.start()].count("\n") + 1
            # An expression referring to the workflow's own env is checked through `_ENV_DECL` above.
            # Accepting it here would double-report, and rejecting it would forbid the indirection
            # `ci.yml` uses deliberately.
            if raw.startswith("${{") and "PYTHON_VERSION" in raw:
                continue
            if raw != target:
                out.append(f"{workflow.name}:{line}: python-version is {raw!r}, target is {target}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)

    target = target_version()
    problems = findings(target)
    workflow_count = len(list(WORKFLOWS.glob("*.yml"))) + len(list(WORKFLOWS.glob("*.yaml")))
    if not workflow_count:
        print(f"ERROR: no workflows found under {WORKFLOWS}; nothing was checked", file=sys.stderr)
        return 1

    if problems:
        print(
            f"ERROR: {len(problems)} workflow Python pin(s) disagree with the project target {target}:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nA skew here is not cosmetic. `mutation-ci.yml` on 3.11 made Q-19's property raise\n"
            "`AttributeError: module 'sys' has no attribute 'monitoring'` on every run, so its negative\n"
            "control reported a kill it had not made. Match the version the backend image ships on.",
            file=sys.stderr,
        )
        return 1

    print(f"check-workflow-python: {workflow_count} workflow(s) all target Python {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
