#!/usr/bin/env python
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Emit every figure a status report is allowed to quote, read from real artifacts only.

WHY THIS EXISTS. A report claimed the backend suite was "1,715 passed at 73.98% vs 70". The real
suite is 3,135 tests and the real combined coverage is 86.58%: the quoted figures were `tests/unit`
plus five hand-picked integration files, with coverage measured over that subset and compared against
the whole-suite gate. Nobody could have checked it without re-running everything, which is the same
as not being able to check it.

So the numbers stop being typed. Every line below is read from an artifact a command produced:

  * backend per-shard and combined coverage      -> `.coverage.<shard>` data files, via `coverage`
  * backend per-shard test counts                -> `<shard>.junit.xml`, via JUnit's own totals
  * agent coverage and race status               -> `agent/coverage.out`, `agent/race.status`
  * frontend coverage against four thresholds    -> `frontend/coverage/coverage-summary.json`
  * gate reachability                            -> `scripts/check-gate-reachability.py` exit + stdout
  * pre-commit hook results                      -> a `pre-commit run --all-files` log
  * OpenAPI path count and drift                 -> `docs/openapi.json` + `dump-openapi.py --check`
  * the five workflow conclusions                -> the GitHub API, by commit sha

IT FAILS RATHER THAN OMITS. A missing artifact is reported as a failure and exits non-zero, because a
silently absent line is exactly how a partial result gets read as a whole one. `--strict` is the
default; nothing here degrades quietly.

IT NEVER READS PROGRESS.md, a commit message, or a previous report. Those record intent and history.
Only artifacts establish current truth.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
AGENT = ROOT / "agent"

SHARDS = ("unit", "meta", "integration")

#: The gates this project holds, as the configuration files state them.
BACKEND_GATE = 70
AGENT_GATE = 70
FRONTEND_GATES = {"statements": 90.0, "lines": 90.0, "functions": 90.0, "branches": 80.0}
EXPECTED_WORKFLOWS = (
    "ci",
    "End-to-End Journey CI",
    "Mutation Testing CI Pipeline",
    "Kubernetes & SPIRE CI",
    "Templates Library Validation Pipeline",
)


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    def head(self, text: str) -> None:
        self.say()
        self.say(text)
        self.say("-" * len(text))

    def missing(self, what: str, how: str) -> None:
        """Record an absent source as a FAILURE, never as a blank line."""
        message = f"MISSING: {what}"
        self.say(f"  {message}")
        self.say(f"           produce it with: {how}")
        self.failures.append(message)


def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)  # noqa: S603 - fixed argv


def head_sha() -> str:
    proc = run(["git", "rev-parse", "HEAD"], ROOT)
    return proc.stdout.strip()


# ── backend ──────────────────────────────────────────────────────────────────────────────────────


def backend_counts(rep: Report) -> None:
    rep.head("BACKEND — per-shard test counts (source: <shard>.junit.xml)")
    total = passed = failed = errors = skipped = 0
    seen = 0
    for shard in SHARDS:
        path = BACKEND / f"{shard}.junit.xml"
        if not path.exists():
            rep.missing(
                f"{path.relative_to(ROOT)}",
                f'cd backend; pytest <{shard} paths> -m "not oidc and not infisical" --junitxml={shard}.junit.xml',
            )
            continue
        suite = ET.parse(path).getroot()
        node = suite if suite.tag == "testsuite" else suite.find("testsuite")
        if node is None:
            rep.missing(f"a <testsuite> element in {path.name}", "re-run the shard")
            continue
        t = int(node.get("tests", 0))
        f = int(node.get("failures", 0))
        e = int(node.get("errors", 0))
        s = int(node.get("skipped", 0))
        p = t - f - e - s
        total += t
        passed += p
        failed += f
        errors += e
        skipped += s
        seen += 1
        rep.say(f"  {shard:<12} tests={t:<6} passed={p:<6} failed={f:<3} errors={e:<3} skipped={s}")

    if seen != len(SHARDS):
        rep.failures.append(f"only {seen} of {len(SHARDS)} backend shards reported a JUnit result")
        return
    rep.say(
        f"  {'COMBINED':<12} tests={total:<6} passed={passed:<6} "
        f"failed={failed:<3} errors={errors:<3} skipped={skipped}"
    )
    if failed or errors:
        rep.failures.append(f"backend suite has {failed} failure(s) and {errors} error(s)")

    # The count must match what the shards COLLECT, or a shard ran a subset of its own paths.
    proc = run([sys.executable, str(ROOT / "scripts" / "check-shard-collection.py")], ROOT)
    collected = None
    m = re.search(r"account for all (\d+) collected tests", proc.stdout)
    if m:
        collected = int(m.group(1))
    rep.say(f"  collection cross-check: check-shard-collection.py exit={proc.returncode}")
    if proc.returncode != 0:
        rep.failures.append("check-shard-collection.py failed; the shards do not account for the tree")
    elif collected is not None and collected != total:
        rep.say(f"  MISMATCH: shards collect {collected} but ran {total}")
        rep.failures.append(f"backend ran {total} tests but the shards collect {collected}")
    elif collected is not None:
        rep.say(f"  ok: ran {total} == collected {collected}")


def backend_coverage(rep: Report) -> None:
    rep.head(f"BACKEND — coverage (source: .coverage.<shard>, combined; gate {BACKEND_GATE})")
    present = [s for s in SHARDS if (BACKEND / f".coverage.{s}").exists()]
    for shard in SHARDS:
        path = BACKEND / f".coverage.{shard}"
        if path.exists():
            rep.say(f"  shard data present: {path.name}  ({path.stat().st_size} bytes)")
        else:
            rep.missing(
                f"backend/.coverage.{shard}",
                f'cd backend; $env:COVERAGE_FILE=".coverage.{shard}"; pytest <{shard} paths>',
            )
    if len(present) != len(SHARDS):
        rep.failures.append("cannot combine backend coverage: not every shard contributed data")
        return

    combine = run([sys.executable, "-m", "coverage", "combine", "--keep"], BACKEND)
    rep.say(f"  coverage combine: {combine.stdout.strip() or combine.stderr.strip()}")
    rep.say(f"  coverage combine exit={combine.returncode}")

    jsonp = BACKEND / "coverage.verify.json"
    made = run([sys.executable, "-m", "coverage", "json", "-o", jsonp.name, "--quiet"], BACKEND)
    if made.returncode != 0 or not jsonp.exists():
        rep.missing("a combined coverage JSON report", "cd backend; coverage combine; coverage json")
        return
    totals = json.loads(jsonp.read_text(encoding="utf-8"))["totals"]
    pct = totals["percent_covered"]
    rep.say(f"  statements={totals['num_statements']}  missing={totals['missing_lines']}")
    rep.say(f"  branches={totals['num_branches']}  partial={totals['num_partial_branches']}")
    rep.say(f"  COMBINED COVERAGE = {pct:.2f}%   gate {BACKEND_GATE}  -> {'PASS' if pct >= BACKEND_GATE else 'FAIL'}")
    if pct < BACKEND_GATE:
        rep.failures.append(f"backend combined coverage {pct:.2f}% is below {BACKEND_GATE}")

    report = run([sys.executable, "-m", "coverage", "report"], BACKEND)
    rep.say(f"  coverage report exit={report.returncode}  (enforces fail_under from pyproject.toml)")
    if report.returncode != 0:
        rep.failures.append("coverage report exited non-zero: the configured fail_under was not met")


# ── agent ────────────────────────────────────────────────────────────────────────────────────────


def agent_section(rep: Report) -> None:
    rep.head(f"AGENT — coverage and race (source: agent/coverage.out, agent/race.status; gate {AGENT_GATE})")
    profile = AGENT / "coverage.out"
    if not profile.exists():
        rep.missing("agent/coverage.out", "cd agent; go test ./... -coverprofile=coverage.out")
    else:
        func = run(["go", "tool", "cover", "-func", profile.name], AGENT)
        if func.returncode != 0:
            rep.missing("a readable coverage profile", "cd agent; go test ./... -coverprofile=coverage.out")
        else:
            last = [ln for ln in func.stdout.splitlines() if ln.startswith("total:")]
            if not last:
                rep.missing("a `total:` line from go tool cover", "re-run go test with -coverprofile")
            else:
                pct = float(last[-1].split()[-1].rstrip("%"))
                rep.say(f"  {last[-1].strip()}")
                rep.say(
                    f"  AGENT COVERAGE = {pct:.1f}%   gate {AGENT_GATE}  -> {'PASS' if pct >= AGENT_GATE else 'FAIL'}"
                )
                if pct < AGENT_GATE:
                    rep.failures.append(f"agent coverage {pct:.1f}% is below {AGENT_GATE}")

    status = AGENT / "race.status"
    if not status.exists():
        rep.missing("agent/race.status", 'cd agent; go test -race ./... -count=1; "exit=$LASTEXITCODE" > race.status')
    else:
        text = status.read_text(encoding="utf-8").strip()
        rep.say(f"  go test -race: {text}")
        if "exit=0" not in text:
            rep.failures.append(f"agent race run did not pass: {text}")


# ── frontend ─────────────────────────────────────────────────────────────────────────────────────


def frontend_section(rep: Report) -> None:
    rep.head("FRONTEND — coverage against four thresholds (source: coverage/coverage-summary.json)")
    path = FRONTEND / "coverage" / "coverage-summary.json"
    if not path.exists():
        rep.missing(
            "frontend/coverage/coverage-summary.json",
            "cd frontend; npx vitest run --coverage (with the json-summary reporter enabled)",
        )
        return
    total = json.loads(path.read_text(encoding="utf-8"))["total"]
    for metric, gate in FRONTEND_GATES.items():
        got = total[metric]["pct"]
        verdict = "PASS" if got >= gate else "FAIL"
        rep.say(f"  {metric:<12} {got:>6.2f}%   gate {gate:>5.1f}  -> {verdict}")
        if got < gate:
            rep.failures.append(f"frontend {metric} {got:.2f}% is below {gate}")

    junit = FRONTEND / "vitest.junit.xml"
    if not junit.exists():
        rep.missing(
            "frontend/vitest.junit.xml", "cd frontend; npx vitest run --reporter=junit --outputFile=vitest.junit.xml"
        )
        return
    root = ET.parse(junit).getroot()
    tests = sum(int(s.get("tests", 0)) for s in root.iter("testsuite"))
    fails = sum(int(s.get("failures", 0)) for s in root.iter("testsuite"))
    errs = sum(int(s.get("errors", 0)) for s in root.iter("testsuite"))
    rep.say(f"  tests={tests}  failures={fails}  errors={errs}")
    if fails or errs:
        rep.failures.append(f"frontend has {fails} failure(s) and {errs} error(s)")


# ── repository gates ─────────────────────────────────────────────────────────────────────────────


def gates_section(rep: Report) -> None:
    rep.head("REPOSITORY GATES — run now, exit codes reported")
    reach = run([sys.executable, str(ROOT / "scripts" / "check-gate-reachability.py")], ROOT)
    line = next((ln.strip() for ln in reach.stdout.splitlines() if "gate scripts reachable" in ln), "")
    rep.say(f"  check-gate-reachability.py exit={reach.returncode}  {line}")
    if reach.returncode != 0:
        rep.failures.append("check-gate-reachability.py failed")

    openapi = ROOT / "docs" / "openapi.json"
    if not openapi.exists():
        rep.missing("docs/openapi.json", "python scripts/dump-openapi.py")
    else:
        paths = len(json.loads(openapi.read_text(encoding="utf-8"))["paths"])
        drift = run([sys.executable, str(ROOT / "scripts" / "dump-openapi.py"), "--check"], ROOT)
        rep.say(f"  docs/openapi.json paths={paths}")
        tail = drift.stdout.strip().splitlines()[-1] if drift.stdout.strip() else ""
        rep.say(f"  dump-openapi.py --check exit={drift.returncode}  {tail}")
        if drift.returncode != 0:
            rep.failures.append("docs/openapi.json has drifted from the live schema")

    log = ROOT / "precommit.log"
    if not log.exists():
        rep.missing("precommit.log", "python -m pre_commit run --all-files > precommit.log 2>&1")
    else:
        text = log.read_text(encoding="utf-8", errors="replace")
        passed = len(re.findall(r"\.\.+Passed", text))
        failed = len(re.findall(r"\.\.+Failed", text))
        skipped = len(re.findall(r"\.\.+Skipped", text))
        rep.say(f"  pre-commit hooks: passed={passed} failed={failed} skipped={skipped}")
        if failed:
            rep.failures.append(f"{failed} pre-commit hook(s) failed")
        if passed == 0:
            rep.failures.append("precommit.log records no passing hooks; it is probably not a real run")


# ── workflows ────────────────────────────────────────────────────────────────────────────────────


def workflows_section(rep: Report, sha: str, repo: str | None) -> None:
    rep.head(f"WORKFLOWS — conclusions for {sha[:8]} (source: GitHub API via gh)")
    proc = run(
        ["gh", "run", "list", "--commit", sha, "--limit", "40", "--json", "name,conclusion,databaseId,status"]
        + (["--repo", repo] if repo else []),
        ROOT,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        rep.missing(
            f"workflow runs for {sha[:8]} from the GitHub API",
            "push the commit, then: gh run list --commit <sha>",
        )
        return
    runs = json.loads(proc.stdout)
    by_name: dict[str, dict] = {}
    for r in runs:
        by_name.setdefault(r["name"], r)
    for name in EXPECTED_WORKFLOWS:
        r = by_name.get(name)
        if r is None:
            rep.missing(f"a run of {name!r} for {sha[:8]}", "push the commit and wait for it to be scheduled")
            continue
        rep.say(f"  {name:<40} {str(r['conclusion'] or r['status']):<12} run id {r['databaseId']}")
        if r["conclusion"] != "success":
            rep.failures.append(f"workflow {name!r} concluded {r['conclusion'] or r['status']!r}")


# ── main ─────────────────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit the verification set from real artifacts.")
    parser.add_argument("--sha", default=None, help="commit to read workflow conclusions for (default: HEAD)")
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "owner/name to read workflow runs from. Required when the checkout has more than one "
            "remote, because `gh` would otherwise guess and a wrong guess reports NO RUNS, which this "
            "script correctly treats as a missing source rather than a pass."
        ),
    )
    parser.add_argument(
        "--skip-workflows",
        action="store_true",
        help="omit the workflow section, for use before the commit has been pushed",
    )
    args = parser.parse_args()

    sha = args.sha or head_sha()
    rep = Report()
    rep.say("=" * 96)
    rep.say("VERIFICATION SET - every figure below was read from an artifact, none typed by hand")
    rep.say(f"commit {sha}")
    rep.say("=" * 96)

    backend_counts(rep)
    backend_coverage(rep)
    agent_section(rep)
    frontend_section(rep)
    gates_section(rep)
    if args.skip_workflows:
        rep.head("WORKFLOWS")
        rep.say("  skipped by --skip-workflows")
    else:
        workflows_section(rep, sha, args.repo)

    rep.head("RESULT")
    if rep.failures:
        rep.say(f"  {len(rep.failures)} problem(s):")
        for f in rep.failures:
            rep.say(f"    - {f}")
    else:
        rep.say("  every source was present and every gate was met")

    print("\n".join(rep.lines))
    return 1 if rep.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
