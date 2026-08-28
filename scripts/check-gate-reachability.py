#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every gate script must be reachable from CI, pre-commit or the Makefile.

What this gates, and the incident that produced it
-------------------------------------------------
``scripts/check-docs.sh`` asserts the documentation contract. It ran in no workflow, no hook and
no Makefile target, and it had been failing for a week: a staleness notice had been inserted
*ahead* of ``docs/deployment.md``'s "must never be exposed to a network" warning, which is the
precise thing the gate exists to prevent. Nothing noticed, because nothing ran it.

That was not an incident, it was a category. A sweep found twelve such scripts, and two of them
were failing on rules that a later phase had superseded — ``check-structure.sh`` still required
``frontend/features`` to be empty, and ``check-go-module.sh`` still forbade ``.go`` files under
``internal/validator`` and ``internal/policy``. Had either been wired, the work that populated
those directories could not have been committed. A check that exists and never executes is worse
than no check, because it reads as coverage.

So this asserts the meta-property: for every gate script in the tree there is at least one path
by which it actually runs.

How reachability is decided
---------------------------
A script is *directly* reachable if a CI workflow, ``.pre-commit-config.yaml`` or the ``Makefile``
names it outside a comment. It is *indirectly* reachable if a script that is itself reachable
names it — computed as a fixed point, so a chain of any length counts but a closed ring of
orphans does not. That distinction matters: it is what correctly credits ``chokepoint_graph.py``,
which only ever runs via ``check-chokepoint.sh``, while correctly refusing to credit
``check-structure.sh`` back when its only caller ``check-area1.sh`` was itself unreachable.

Two rules keep the answer honest rather than merely textual:

* **Comments do not count.** ``dump-openapi.py --check`` was named as the OpenAPI drift gate in a
  comment in ``.pre-commit-config.yaml`` and in ``docs/api.md``, and was invoked nowhere. A
  mention is not an invocation.
* **A swallowed exit code does not count.** ``check-lock-freshness.sh`` ran as
  ``bash scripts/check-lock-freshness.sh || true`` under a step named "no diff allowed", so it
  could not fail the build, and it was hiding a real drift. A reference whose line ends in
  ``|| true``, ``|| :``, ``|| exit 0`` or which carries ``continue-on-error`` is not a gate.

Deliberate exemptions live in ``scripts/gate-reachability-baseline.txt`` and must each name a
reason. As in ``check-ci-jobs.py``, a baseline entry that has since become reachable is itself an
error, so the file cannot accumulate stale excuses.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Gate scripts are discovered rather than listed, so a new one is covered the day it lands.
#: `check-*` is the naming convention; the extras are gates that predate it.
GATE_GLOBS = ("scripts/check-*.sh", "scripts/check-*.py", "scripts/ci/check-*.py")
GATE_EXTRAS = (
    "scripts/control-of-the-control.py",
    "scripts/chokepoint_graph.py",
    "scripts/mutation-harness.py",
    "scripts/verify-release.sh",
    "scripts/policy-test.sh",
    "scripts/sbom.sh",
    "scripts/sbom-merge.py",
    "scripts/collect_call_sites.py",
    "scripts/audit-chain-smoke.py",
    "scripts/go-vet-changed.sh",
    "scripts/dump-openapi.py",
    "scripts/gen-envelope-fixtures.py",
    "scripts/gen-governance-fixtures.py",
)

#: Where a direct reference may appear.
ROOT_CALLERS = (".pre-commit-config.yaml", "Makefile")

#: A trailing `|| true` and friends turn a gate into decoration. So does continue-on-error.
_SWALLOWED = re.compile(r"\|\|\s*(true|:|exit\s+0)\s*$|continue-on-error")


def tracked(pattern: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", pattern],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def discover_gates() -> list[str]:
    found: set[str] = set()
    for glob in GATE_GLOBS:
        found.update(tracked(glob))
    for extra in GATE_EXTRAS:
        found.update(tracked(extra))
    # scripts/tests/** are tests OF the gates, not gates. They are held by their own runner.
    return sorted(p for p in found if not p.startswith("scripts/tests/"))


#: Triple-quoted blocks in Python. A docstring is prose, and prose naming a script is a mention
#: rather than an invocation -- `test_regime_end_to_end.py`'s module docstring names
#: `scripts/check-hygiene.sh` while the test itself runs something else entirely.
_PY_STRING_BLOCK = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')


def strip_comments(text: str, path: Path) -> str:
    """Remove comment and docstring content so a mention cannot be read as an invocation.

    Shell, YAML, Make and Python all comment with `#`, and none of the callers scanned here put a
    `#` inside a string on a line that also invokes a script, so a whole-line and trailing-`#`
    strip is accurate for this corpus and errs toward reporting an orphan rather than hiding one.

    Python additionally gets its triple-quoted blocks removed. Without that, this check credited
    `check-hygiene.sh`, `check-structure.sh` and `check-makefile.sh` purely because a test's
    docstring explained what they enforce -- which is exactly the "reads as coverage" error the
    check exists to catch, committed by the check itself.
    """
    if path.suffix == ".py":
        text = _PY_STRING_BLOCK.sub("", text)
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        head = line.split(" #", 1)[0]
        out.append(head)
    return "\n".join(out)


def read_caller(path: Path) -> str:
    try:
        return strip_comments(path.read_text(encoding="utf-8", errors="replace"), path)
    except OSError:
        return ""


def references(body: str, script: str) -> bool:
    """True when `body` invokes `script` on a line whose exit code is not swallowed.

    The basename is matched after any path separator rather than only after a literal
    ``scripts/``, because two real invocation forms do not carry that prefix: CI bind-mounts the
    directory elsewhere and runs ``python /smoke/audit-chain-smoke.py``, and a sibling script
    refers to ``"$SCRIPT_DIR/check-docs.sh"``. These basenames are distinctive enough that a
    boundary match does not collide with prose.
    """
    name = script.removeprefix("scripts/")
    needle = re.compile(rf"[/\"'\s]{re.escape(name)}\b")
    for line in body.splitlines():
        if needle.search(line) and not _SWALLOWED.search(line.strip()):
            return True
    return False


def load_baseline(path: Path) -> dict[str, str]:
    exempt: dict[str, str] = {}
    if not path.exists():
        return exempt
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        script, _, reason = line.partition(":")
        script, reason = script.strip(), reason.strip()
        if not reason:
            raise SystemExit(
                f"{path.name}: '{script}' has no reason. Every exemption must say why the "
                "script runs nowhere, e.g. "
                "'scripts/foo.sh: developer aggregate, superseded by the audit job'."
            )
        exempt[script] = reason
    return exempt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=REPO_ROOT / "scripts" / "gate-reachability-baseline.txt",
    )
    args = parser.parse_args()

    gates = discover_gates()
    if not gates:
        print(
            "ERROR: no gate scripts were discovered. This check would pass vacuously, which is "
            "the failure mode it exists to prevent.",
            file=sys.stderr,
        )
        return 1

    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    entry_bodies = [read_caller(p) for p in workflows]
    entry_bodies += [read_caller(REPO_ROOT / name) for name in ROOT_CALLERS]

    # A gate driven by a test that CI runs is genuinely executed, and refusing to count that would
    # push two working gates toward being wired a second time or exempted for no reason.
    # `check-compose-validate.py` is applied to the real `docker-compose.yml` by
    # `tests/meta/test_check_compose_validate.py`, and `collect_call_sites.py` is both imported by
    # `test_contract_conformance.py` and run as a subprocess by `test_regime_end_to_end.py` — both
    # inside the `backend` job's `pytest tests/`. The distinction this keeps is between a test that
    # exercises a gate and a mere mention: a fixture comment naming the script is stripped as a
    # comment, so only real imports and invocations count.
    for test_file in tracked("backend/tests/*"):
        entry_bodies.append(read_caller(REPO_ROOT / test_file))

    # Direct reachability from an entry point.
    reachable: set[str] = {g for g in gates if any(references(b, g) for b in entry_bodies)}

    # Indirect reachability, as a fixed point over reachable scripts' own bodies. Any tracked
    # script may be an intermediate hop, not only a gate.
    all_scripts = tracked("scripts/*")
    bodies = {s: read_caller(REPO_ROOT / s) for s in all_scripts}
    frontier = {s for s in all_scripts if any(references(b, s) for b in entry_bodies)}
    seen: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        body = bodies.get(current, "")
        for candidate in all_scripts:
            # A script naming itself -- in a usage line, or an error message quoting its own
            # invocation -- must not count as a caller, or any orphan that documents how to run
            # itself would appear reachable.
            if candidate != current and candidate not in seen and references(body, candidate):
                frontier.add(candidate)
    reachable |= {g for g in gates if g in seen}

    exempt = load_baseline(args.baseline)
    failed = False

    orphans = sorted(g for g in gates if g not in reachable and g not in exempt)
    if orphans:
        failed = True
        print(
            f"ERROR: {len(orphans)} gate script(s) run nowhere — not in a CI workflow, not in "
            ".pre-commit-config.yaml, not in the Makefile, and not called by anything that does:",
            file=sys.stderr,
        )
        for orphan in orphans:
            print(f"  {orphan}", file=sys.stderr)
        print(
            "\nWire each one in, delete it, or add it to "
            f"{args.baseline.name} with a reason. A check that never executes reads as coverage "
            "and is not.",
            file=sys.stderr,
        )

    stale = sorted(set(exempt) & reachable)
    if stale:
        failed = True
        print(
            f"\nERROR: {len(stale)} script(s) are exempted in {args.baseline.name} but are now "
            f"reachable: {', '.join(stale)}. Remove those lines.",
            file=sys.stderr,
        )

    unknown = sorted(set(exempt) - set(gates))
    if unknown:
        failed = True
        print(
            f"\nERROR: {len(unknown)} exemption(s) in {args.baseline.name} name a script that is "
            f"not a discovered gate: {', '.join(unknown)}.",
            file=sys.stderr,
        )

    if not failed:
        print(
            f"ok:   {len(reachable)} of {len(gates)} gate scripts reachable, "
            f"{len(exempt)} exempted with a stated reason"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
