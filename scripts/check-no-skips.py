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

Platform-conditional skips, Go side only (D-68)
-----------------------------------------------
Some assertions cannot hold on some platforms — POSIX mode bits, symlinks, a
read-only directory that refuses a write. §0.4.4's remedy ("provide the capability
in CI") is not available for those, so on Windows this gate could never pass, which
is the shape D-51 rejects. A Go test may therefore DECLARE the platform it needs in
its own skip message:

    t.Skip("platform-only: posix - NTFS uses ACLs, so a mode-bit assertion is meaningless")

The gate checks the declaration against the platform the report came from. An
undeclared skip still fails; a declaration outside the closed vocabulary fails; and
a declaration whose requirement the reporting platform SATISFIES fails, so the tag
cannot be used as a blanket exemption. On Linux, where CI runs, `posix` is satisfied
and every one of those tests must run. See `classify_platform_skips`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

MANDATORY_MARKER = "mandatory"


#: Marker a Go test writes into its own skip message to declare that the skip is a property of
#: the PLATFORM rather than of a missing capability. See D-68 and `classify_platform_skips`.
PLATFORM_TAG = "platform-only:"

#: The closed vocabulary of platform requirements. Closed on purpose: a typo must fail the gate
#: rather than silently exempt a test, which is how an allowlist rots into a blanket pass.
#: Each entry answers "does the platform this report came from satisfy the requirement?"
PLATFORM_REQUIREMENTS: dict[str, object] = {
    # POSIX file semantics: mode bits that mean something, symlinks, a read-only directory that
    # actually refuses a write. Windows has none of these in the form the assertions need.
    "posix": lambda goos: goos != "windows",
    "windows": lambda goos: goos == "windows",
}


@dataclass(frozen=True)
class Outcome:
    """One test node's result, normalised across the two runners."""

    node_id: str
    outcome: str  # "passed" | "failed" | "skipped"
    reason: str = ""

    def platform_requirement(self) -> str | None:
        """The platform this node declared it requires, or None if it declared nothing.

        Read out of the skip message rather than from a list in this file, so the declaration
        lives beside the guard that produces it and the two cannot drift (the rot recorded as
        finding 49).
        """
        if PLATFORM_TAG not in self.reason:
            return None
        tail = self.reason.split(PLATFORM_TAG, 1)[1].strip()
        # The requirement is the first word; anything after it is prose for a human.
        word = ""
        for character in tail:
            if character.isalnum() or character in {"_", "-"}:
                word += character
            else:
                break
        return word.lower()


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
    """Collect per-test outcomes from `go test -json` events.

    Every output line for a test is kept, not only the lines containing the word "skip". The
    earlier version matched on "skip", which captured the `--- SKIP: TestX (0.00s)` BANNER and
    threw away the line above it — the one carrying the reason the test gave. So the gate that
    exists to report skips could not report why any of them skipped, and D-68's platform
    declaration, which lives in that message, would have been invisible to it.

    Framing lines are dropped because they are noise: `=== RUN`, `=== PAUSE`, `=== CONT` and the
    `--- PASS/FAIL/SKIP` banners say nothing a caller cannot read from the outcome itself.
    """
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
            if not text or text.startswith(("=== ", "--- ")):
                continue
            reasons.setdefault(node_id, []).append(text)
        elif action in {"pass", "fail", "skip"}:
            mapped = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
            outcomes[node_id] = Outcome(node_id, mapped, " ".join(reasons.get(node_id, []))[:400])
    return sorted(outcomes.values(), key=lambda o: o.node_id)


def reporting_platform(explicit: str | None) -> str:
    """The GOOS the report was produced on.

    Taken from `--os` when given, otherwise from `go env GOOS`. It is DERIVED rather than
    defaulted: a wrong platform would silently flip every verdict below, so if neither source
    answers, the caller is told to say which platform rather than being given a guess.
    """
    if explicit:
        return explicit.strip().lower()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["go", "env", "GOOS"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise SystemExit(
            f"ERROR: cannot determine the platform this report came from ({exc}). "
            "Pass --os explicitly; a guess would silently flip every platform verdict."
        ) from exc
    if completed.returncode != 0 or not completed.stdout.strip():
        raise SystemExit(
            "ERROR: `go env GOOS` produced nothing, so the platform this report came from is "
            "unknown. Pass --os explicitly."
        )
    return completed.stdout.strip().lower()


def classify_platform_skips(
    skipped: list[Outcome], *, goos: str
) -> tuple[list[Outcome], list[tuple[Outcome, str]]]:
    """Split skips into (permitted platform guards, failures with the reason each failed).

    THE PROBLEM THIS SOLVES (D-68). Six Go tests assert POSIX file semantics that Windows cannot
    express — symlink escape, a read-only directory refusing a write, owner-only mode bits under
    NTFS ACLs. They guard on `runtime.GOOS` and skip on Windows. §0.4.4 says a skip inside a green
    run is indistinguishable from coverage, so the gate failed; but the remedy §0.4.4 offers —
    "provide the capability in CI" — cannot be carried out, because you cannot give Windows POSIX
    mode bits. A developer therefore had a gate that could never pass on their machine, which is
    the shape D-51 rejects, and the pressure that creates is to stop running it.

    THE DISCRIMINATOR. A capability skip and a platform skip differ in one respect that matters:
    the capability could have been supplied and was not, while the platform cannot be. So the test
    declares which it is, in its own skip message, and the gate checks the declaration against the
    platform the report came from:

      * no declaration                       -> FAILURE. Silence is a capability skip; §0.4.4.
      * declaration outside the vocabulary   -> FAILURE. A typo must not exempt anything.
      * declared requirement IS satisfied    -> FAILURE. The platform can run it and it skipped
                                                anyway, so the message is not the real reason.
                                                This is the clause that stops the tag being used
                                                as a blanket exemption.
      * declared requirement is NOT satisfied -> permitted, and printed.

    WHY NOT AN ALLOWLIST OF TEST NAMES. Because it would be data restated away from the guard that
    produces it, which is finding 49's rot in a new place: rename a test and the entry is dead
    weight, delete the guard and the entry silently keeps exempting a name that no longer skips.
    The declaration travels with the guard, so it cannot survive it.

    WHAT THIS DOES NOT WEAKEN. On Linux — which is what CI runs — a `posix` requirement is
    satisfied, so all six of those tests run and any skip among them is a failure exactly as
    before. The permitted set on CI is empty today, and the gate prints it either way, so it
    becoming non-empty is visible rather than assumed.
    """
    permitted: list[Outcome] = []
    failures: list[tuple[Outcome, str]] = []
    for outcome in skipped:
        requirement = outcome.platform_requirement()
        if requirement is None:
            failures.append((outcome, "no platform declaration; treated as a capability skip"))
            continue
        satisfied = PLATFORM_REQUIREMENTS.get(requirement)
        if satisfied is None:
            known = ", ".join(sorted(PLATFORM_REQUIREMENTS))
            failures.append(
                (outcome, f"declares an unknown platform requirement {requirement!r}; known: {known}")
            )
            continue
        if satisfied(goos):  # type: ignore[operator]
            failures.append(
                (
                    outcome,
                    f"declares {requirement!r}, which {goos} SATISFIES, so the platform is not "
                    "why it skipped",
                )
            )
            continue
        permitted.append(outcome)
    return permitted, failures


def check(
    outcomes: list[Outcome],
    *,
    source: str,
    allow_empty: bool,
    platform: Callable[[], str] | None = None,
) -> int:
    """Report and gate.

    `platform` is supplied in Go mode only, and is a CALLABLE rather than a string because
    resolving it may shell out to `go env GOOS`. A report with no skips needs no platform, and
    making the call lazy keeps this gate runnable wherever the report is — the backend test job
    has no Go toolchain, and a check that demanded one to confirm a clean run would be a gate
    that cannot pass in a job that has every right to run it.
    """
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

    permitted: list[Outcome] = []
    failures: list[tuple[Outcome, str]] = []
    if platform is None:
        failures = [(o, "") for o in skipped]
    elif not skipped:
        print("check-no-skips: no skips, so no platform verdict was needed")
    else:
        goos = platform()
        print(f"check-no-skips: the report was produced on GOOS={goos}")
        permitted, failures = classify_platform_skips(skipped, goos=goos)
        # Printed unconditionally, including when it is empty. A permitted set that grows has to
        # be visible in the CI log, or "the platform could not run it" becomes a place to hide a
        # capability skip — which is the pressure D-68 accepts and this line contains.
        print(f"check-no-skips: {len(permitted)} skip(s) permitted as declared platform guards")
        for outcome in permitted:
            print(f"  PERMITTED {outcome.node_id} - requires {outcome.platform_requirement()!r}")

    if failures:
        print("ERROR: mandatory tests skipped in CI:", file=sys.stderr)
        for outcome, why in failures:
            detail = f" - {why}" if why else ""
            suffix = f" [{outcome.reason}]" if outcome.reason else ""
            print(f"  {outcome.node_id}{detail}{suffix}", file=sys.stderr)
        print(
            "\nA skip inside a green run is indistinguishable from coverage "
            "(design.md §0.4.4, D-26). Either provide the capability in CI, or "
            "remove the `mandatory` marker deliberately, or — on the Go side only — declare a "
            f"platform the assertion genuinely cannot hold on with `{PLATFORM_TAG} "
            f"<{'|'.join(sorted(PLATFORM_REQUIREMENTS))}>` in the skip message (D-68).",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on any skip in the mandatory selection")
    parser.add_argument("report", type=Path, help="pytest --report-log JSONL, or go test -json output")
    parser.add_argument("--go", action="store_true", help="parse `go test -json` events instead of pytest")
    parser.add_argument(
        "--os",
        dest="goos",
        default=None,
        help="the GOOS the report was produced on; defaults to `go env GOOS`. Go mode only.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="permit an empty selection; for the script's own meta tests only, never in CI",
    )
    args = parser.parse_args(argv)

    if args.goos and not args.go:
        print("ERROR: --os applies to --go reports only; pytest reports carry no GOOS", file=sys.stderr)
        return 2

    if not args.report.is_file():
        print(f"ERROR: no such report file: {args.report}", file=sys.stderr)
        return 2

    if args.go:
        return check(
            parse_go(args.report),
            source=str(args.report),
            allow_empty=args.allow_empty,
            platform=lambda: reporting_platform(args.goos),
        )
    return check(parse_pytest(args.report), source=str(args.report), allow_empty=args.allow_empty)


if __name__ == "__main__":
    raise SystemExit(main())
