#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Fail the build when a mandatory test skipped in CI (design.md §0.4.4).

Why
---
A skip printed inside a green run is indistinguishable from coverage. Phase 0
proved it: `FORGEOPS_TEST_DATABASE_URL` was referenced only by
`tests/integration/conftest.py` and set nowhere, so criterion 14's seven schema
assertions skipped **while the CI job paid to run a real `pgvector/pgvector:pg17`
service beside them**. D-26 made `require_capability` fail when the environment
promised the capability; this script is the other half — it reads what actually
ran and fails if anything in the mandatory selection did not.

The mandatory selection is defined by **marker**, not by path, so moving a file
cannot quietly drop it from the set CI must run.

Invocation
----------
    pytest -m mandatory --report-log=mandatory.jsonl
    python scripts/check-no-skips.py mandatory.jsonl

    go test -json -tags=integration ./... > agent.jsonl
    python scripts/check-no-skips.py --go agent.jsonl

Input is the pytest `--report-log` JSONL or `go test -json` events and nothing
else, so the check cannot disagree with what actually ran.

Failure condition
-----------------
Exit 1 listing every mandatory node whose outcome was `skipped`, **and** exit 1
when the mandatory selection is empty. The second clause is not defensive
programming: `pytest -m mandatory` over a tree with no `mandatory` markers exits 0
having run nothing, so without it the gate would pass forever while proving
nothing. That is the same vacuity trap §0.4.5 closes for the mutation harness.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MANDATORY_MARKER = "mandatory"


@dataclass(frozen=True)
class Outcome:
    """One test node's result, normalised across the two runners."""

    node_id: str
    outcome: str  # "passed" | "failed" | "skipped"
    reason: str = ""


def _read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{number}: not valid JSON: {exc}") from exc


def parse_pytest(path: Path) -> list[Outcome]:
    """Collect per-test outcomes from a pytest `--report-log` file.

    A node is counted once. `--report-log` emits a `TestReport` per phase (setup,
    call, teardown); a skip can be recorded in any of them, so the phases are
    folded together with `skipped` winning over `passed`. Without that fold, a test
    skipped at setup would still show a `passed` teardown phase and the gate would
    miss it.
    """
    outcomes: dict[str, Outcome] = {}
    for event in _read_jsonl(path):
        if event.get("$report_type") != "TestReport":
            continue
        node_id = event.get("nodeid", "")
        if not node_id:
            continue
        outcome = event.get("outcome", "")
        if outcome == "skipped":
            reason = ""
            longrepr = event.get("longrepr")
            if isinstance(longrepr, list | tuple) and len(longrepr) == 3:
                reason = str(longrepr[2])
            elif isinstance(longrepr, str):
                reason = longrepr
            outcomes[node_id] = Outcome(node_id, "skipped", reason)
        elif node_id not in outcomes or outcomes[node_id].outcome == "passed":
            if outcome in {"passed", "failed"}:
                previous = outcomes.get(node_id)
                if previous is None or previous.outcome != "skipped":
                    outcomes[node_id] = Outcome(node_id, outcome)
    return sorted(outcomes.values(), key=lambda o: o.node_id)


def parse_go(path: Path) -> list[Outcome]:
    """Collect per-test outcomes from `go test -json` events."""
    outcomes: dict[str, Outcome] = {}
    reasons: dict[str, list[str]] = {}
    for event in _read_jsonl(path):
        test = event.get("Test")
        if not test:
            continue
        node_id = f"{event.get('Package', '?')}::{test}"
        action = event.get("Action")
        if action == "output":
            text = str(event.get("Output", "")).strip()
            if "SKIP" in text or "skip" in text.lower():
                reasons.setdefault(node_id, []).append(text)
        elif action in {"pass", "fail", "skip"}:
            mapped = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
            outcomes[node_id] = Outcome(node_id, mapped, " ".join(reasons.get(node_id, []))[:400])
    return sorted(outcomes.values(), key=lambda o: o.node_id)


def check(outcomes: list[Outcome], *, source: str, allow_empty: bool) -> int:
    if not outcomes and not allow_empty:
        print(
            f"ERROR: the mandatory selection in {source} is EMPTY. "
            "A selector that matches nothing passes silently, which is the same "
            "vacuity trap the mutation harness closes. Check the `-m mandatory` "
            "selection and the marker registration in backend/pyproject.toml.",
            file=sys.stderr,
        )
        return 1

    skipped = [o for o in outcomes if o.outcome == "skipped"]
    print(f"check-no-skips: {source}: {len(outcomes)} mandatory nodes, {len(skipped)} skipped")

    if skipped:
        print("ERROR: mandatory tests skipped in CI:", file=sys.stderr)
        for outcome in skipped:
            suffix = f" - {outcome.reason}" if outcome.reason else ""
            print(f"  {outcome.node_id}{suffix}", file=sys.stderr)
        print(
            "\nA skip inside a green run is indistinguishable from coverage "
            "(design.md §0.4.4, D-26). Either provide the capability in CI or "
            "remove the `mandatory` marker deliberately.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on any skip in the mandatory selection")
    parser.add_argument("report", type=Path, help="pytest --report-log JSONL, or go test -json output")
    parser.add_argument("--go", action="store_true", help="parse `go test -json` events instead of pytest")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="permit an empty selection; for the script's own meta tests only, never in CI",
    )
    args = parser.parse_args(argv)

    if not args.report.is_file():
        print(f"ERROR: no such report file: {args.report}", file=sys.stderr)
        return 2

    outcomes = parse_go(args.report) if args.go else parse_pytest(args.report)
    return check(outcomes, source=str(args.report), allow_empty=args.allow_empty)


if __name__ == "__main__":
    raise SystemExit(main())
