#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every property ships with an executable negative control (design.md §0.4.5).

Why
---
`REVIEW-PHASE-0.md` Pass 8 emptied **both** of P-09's redaction pattern lists and
all thirteen of its tests stayed green. The clause was decorative, and nothing in
CI could tell. This script promotes that one-off review experiment into a job: for
every property, apply the specific mutation that must break it, and fail the build
if the property survives.

A property that passes under its own negative control is reported `VACUOUS`.

How it avoids touching the repository
-------------------------------------
No tracked file is ever edited. For Python rows the mutation is installed by a
generated pytest plugin written into a `tempfile.mkdtemp()` directory that is
**asserted to lie outside the repository tree**; for Go rows it is installed
through `go build -overlay`, which redirects the compiler to a replacement file in
that same temp directory. After the run the harness asserts `git status
--porcelain` is empty, so the guarantee is verified rather than promised.

Schema (`backend/tests/mutation/mutations.toml`)
-----------------------------------------------
    [Q-08]
    runtime     = "python"                       # or "go"
    property    = "backend/tests/property/test_q08_iteration_bound.py"
    target      = "src.generation.loop.FeedbackLoop._next"
    mutation    = "return replace(state, attempts_remaining=state.attempts_remaining)"
    description = "removes the decrement that guarantees termination"
    patch       = '''
    from src.generation import loop
    _original = loop.FeedbackLoop._next
    def _mutated(self, state, findings):
        ...
    loop.FeedbackLoop._next = _mutated
    '''

`mutation` is the prose control exactly as Appendix B words it, so a reviewer can
compare the two without reading code. `patch` is the executable half: the
statements that install the mutation. Both are required, because a prose-only
control is what made P-09 look tested.

Go rows carry `module_dir`, `package`, `test_run` and `overlay` instead of `patch`;
`overlay` names a replacement file for `original`, applied via `-overlay`.

Usage
-----
    python scripts/mutation-harness.py --all
    python scripts/mutation-harness.py --only Q-08 Q-13
    make mutation

Exit status
-----------
1 if any row is `VACUOUS`, if `mutations.toml` lacks a row for a `Q-` id defined in
Appendix B, or if `git status --porcelain` is non-empty afterwards.

`--allow-incomplete` suppresses **only** the Appendix-B completeness clause, and
exists solely because the properties land leaf by leaf across groups 2-19. Task
19.2 removes it from the `mutation` CI job, at which point all 31 rows must exist.
It never suppresses a `VACUOUS` row or a dirty tree.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
MUTATIONS_TOML = BACKEND_ROOT / "tests" / "mutation" / "mutations.toml"
DESIGN_DOC = REPO_ROOT / ".antigravity" / "specs" / "phase-1-mvp-core" / "design.md"

#: Rows in Appendix B's property table look like `| **Q-08** ★ | ... |`.
_APPENDIX_B_ROW = re.compile(r"^\|\s*\*\*(Q-\d{2})\*\*")

REQUIRED_KEYS = ("property", "mutation", "description")

OK = "OK"
VACUOUS = "VACUOUS"
ERROR = "ERROR"


@dataclass
class Row:
    """One negative control."""

    ident: str
    runtime: str
    property_path: str
    mutation: str
    description: str
    target: str = ""
    patch: str = ""
    module_dir: str = ""
    package: str = ""
    test_run: str = ""
    original: str = ""
    overlay: str = ""


@dataclass
class Result:
    row: Row
    status: str
    detail: str = ""


# ── Appendix B completeness ──────────────────────────────────────────────────


def appendix_b_ids(doc: Path = DESIGN_DOC) -> list[str]:
    """Every `Q-` id defined by Appendix B's property table.

    Read from the design document rather than restated here, so the completeness
    check cannot drift from the authority it is checking against.
    """
    if not doc.is_file():
        raise SystemExit(f"design document not found: {doc}")
    ids: list[str] = []
    for line in doc.read_text(encoding="utf-8").splitlines():
        match = _APPENDIX_B_ROW.match(line)
        if match and match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


# ── loading ──────────────────────────────────────────────────────────────────


def load_rows(path: Path = MUTATIONS_TOML) -> dict[str, Row]:
    if not path.is_file():
        raise SystemExit(f"mutations manifest not found: {path}")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))

    rows: dict[str, Row] = {}
    for ident, body in raw.items():
        if not isinstance(body, dict):
            continue
        missing = [k for k in REQUIRED_KEYS if not body.get(k)]
        if missing:
            raise SystemExit(f"{path}: [{ident}] is missing required key(s): {', '.join(missing)}")
        runtime = body.get("runtime", "python")
        if runtime not in {"python", "go"}:
            raise SystemExit(f"{path}: [{ident}] has unknown runtime {runtime!r}")
        if runtime == "python" and not body.get("patch"):
            raise SystemExit(
                f"{path}: [{ident}] has no `patch`. A prose-only control is exactly "
                "what made P-09 look tested; the mutation must be executable."
            )
        if runtime == "go" and not (body.get("overlay") and body.get("original")):
            raise SystemExit(f"{path}: [{ident}] is a go row and needs both `original` and `overlay`")
        rows[ident] = Row(
            ident=ident,
            runtime=runtime,
            property_path=body["property"],
            mutation=body["mutation"],
            description=body["description"],
            target=body.get("target", ""),
            patch=body.get("patch", ""),
            module_dir=body.get("module_dir", "agent"),
            package=body.get("package", ""),
            test_run=body.get("test_run", ""),
            original=body.get("original", ""),
            overlay=body.get("overlay", ""),
        )
    return rows


# ── the temp directory guarantee ─────────────────────────────────────────────


def make_outside_tempdir() -> Path:
    """A temp directory asserted to lie OUTSIDE the repository tree.

    §0.4.5 requires the mutation to be applied from outside the repository so no
    tracked file can be touched even transiently. `mkdtemp` normally honours TMPDIR,
    which a caller could point inside the repo, so the location is verified rather
    than trusted.
    """
    tmp = Path(tempfile.mkdtemp(prefix="forgeops-mutation-")).resolve()
    repo = REPO_ROOT.resolve()
    if tmp == repo or repo in tmp.parents:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(
            f"refusing to run: the temp directory {tmp} is inside the repository "
            f"{repo}. Unset TMPDIR/TEMP or point it outside the tree."
        )
    return tmp


# ── running one row ──────────────────────────────────────────────────────────

_PLUGIN_TEMPLATE = '''\
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Generated negative control for {ident}. Written outside the repository.

{description}

Appendix B control: {mutation}
"""


def pytest_sessionstart(session):
    """Install the mutation before any test in the property file is collected."""
{patch_body}
'''


def _indent(text: str, spaces: int = 4) -> str:
    pad = " " * spaces
    lines = [line for line in text.strip("\n").splitlines()]
    if not lines:
        return pad + "pass"
    # Strip a common leading indent so TOML multi-line strings can be readable.
    common = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
    return "\n".join(pad + line[common:] if line.strip() else "" for line in lines)


def _nothing_actually_ran(output: str) -> bool:
    """Whether a zero-exit pytest run executed no test at all.

    Reads the terminal summary rather than a `--report-log`, because the harness deliberately
    invokes plain pytest and adding a log file would put a writable artifact inside the run it
    asserts leaves the tree clean.

    The counts are PARSED rather than substring-matched. A first version asked whether the word
    "passed" appeared, which reads `1 skipped, 0 passed` as a healthy run — the one summary shape
    that means the opposite.
    """
    lowered = output.lower()
    if "no tests ran" in lowered:
        return True
    executed = 0
    for outcome in ("passed", "xpassed", "failed", "xfailed", "error", "errors"):
        for count in re.findall(rf"(\d+)\s+{outcome}\b", lowered):
            executed += int(count)
    skipped = sum(int(count) for count in re.findall(r"(\d+)\s+skipped\b", lowered))
    return skipped > 0 and executed == 0


# The summary shapes that constitute proof a test actually failed. Matched against the whole
# captured output, not just the last line, because `-q` puts the counts in the summary line while an
# internal error can push text after it.
#
# The counts are REQUIRED rather than looking for the word "failed": a run can print
# "0 failed" in some summary shapes, and the word also appears in unrelated text such as
# "Coverage failure" or a warning about a failed import. `[1-9]` is the whole point -- a control
# that broke nothing must not satisfy this.
_FAILED_SUMMARY = re.compile(r"\b[1-9]\d*\s+(failed|error|errors)\b", re.IGNORECASE)


def run_python_row(row: Row, tmp: Path) -> Result:
    property_file = REPO_ROOT / row.property_path
    if not property_file.is_file():
        return Result(row, ERROR, f"property file not found: {row.property_path}")

    plugin_name = f"mutation_{row.ident.replace('-', '_').lower()}"
    plugin_path = tmp / f"{plugin_name}.py"
    plugin_path.write_text(
        _PLUGIN_TEMPLATE.format(
            ident=row.ident,
            description=row.description,
            mutation=row.mutation,
            patch_body=_indent(row.patch),
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp), str(BACKEND_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    # A mutation must not be able to write hypothesis' example database inside the
    # repository, so point it at the temp directory too.
    env["HYPOTHESIS_STORAGE_DIRECTORY"] = str(tmp / "hypothesis")

    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            str(property_file),
            "-p",
            plugin_name,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            # `--no-cov` IS LOAD-BEARING, not a convenience.
            #
            # `backend/pyproject.toml` puts `--cov=src --cov-branch --cov-fail-under=70` in
            # `addopts`, deliberately, so the gate applies to every invocation including a
            # developer's. Its own comment states both the consequence and the remedy: "running a
            # single test file fails the threshold, because one file does not cover the package ...
            # pass `--no-cov` for a targeted run."
            #
            # This harness runs ONE property file, so coverage of `src` is near zero and pytest
            # exits 1. Below, an exit of 1 is what the harness reads as "the property failed", which
            # is how it concludes a control killed it. So without this flag EVERY control appears to
            # kill, including one aimed at a property it cannot touch: the vacuous fixture reported
            # "2 passed" and still exited 1, and the harness recorded OK where it owed VACUOUS.
            # A coverage threshold over the whole package says nothing about whether one property
            # observed one behaviour, and letting it decide that question inverts the answer.
            "--no-cov",
        ],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    if completed.returncode == 0:
        output = completed.stdout or ""
        tail = output.strip().splitlines()[-4:]
        # A run in which everything SKIPPED also exits 0, and reporting that as VACUOUS says
        # "the property survived its own control" — which is false and sends a reader looking
        # for a decorative property instead of a missing service. Observed directly while
        # landing Q-04: the harness was invoked without `FORGEOPS_TEST_DATABASE_URL`, every
        # database-backed clause skipped, and the row was reported VACUOUS.
        #
        # §0.4.4 already forbids silent skips in the mandatory selection; this is the same rule
        # applied to the control's own run, because a control that never executed is an ERROR
        # rather than a finding about the property.
        if _nothing_actually_ran(output):
            return Result(
                row,
                ERROR,
                "the control's run executed no tests (all skipped or none collected), so the "
                "property was never exercised: " + " | ".join(tail),
            )
        return Result(row, VACUOUS, "the property PASSED under its own negative control: " + " | ".join(tail))
    if completed.returncode >= 4:
        # 4 = usage error, 5 = no tests collected. Either means the control never
        # ran, which must not be reported as a healthy failure.
        tail = ((completed.stdout or "") + (completed.stderr or "")).strip().splitlines()[-6:]
        return Result(row, ERROR, f"pytest exited {completed.returncode} (control never ran): " + " | ".join(tail))

    # A NON-ZERO EXIT IS NOT ON ITS OWN EVIDENCE THAT THE PROPERTY FAILED, and treating it as such
    # is how a control comes to look effective when it never was. The coverage gate excluded above
    # is one way to exit 1 with every test passing; a plugin raising during teardown is another.
    # So the summary must actually name a failing test before this is called a kill. Anything else
    # is an ERROR, which stops the run, rather than an OK, which would quietly certify a control
    # that demonstrated nothing.
    combined = (completed.stdout or "") + (completed.stderr or "")
    if not _FAILED_SUMMARY.search(combined):
        tail = combined.strip().splitlines()[-6:]
        return Result(
            row,
            ERROR,
            f"pytest exited {completed.returncode} but reported no failing test, so the control was "
            "not shown to break the property: " + " | ".join(tail),
        )
    # The output is CARRIED on an OK result rather than discarded. `control-of-the-control.py` needs it:
    # for the meta-check an OK verdict on a NEUTRALISED row is a FAILURE, and the property's own output is
    # the only thing that says what it objected to instead of the mutation. Without it a broken control
    # reported `EXPECTED FAIL OBSERVED OK` and nothing else, which costs a round trip through CI to
    # diagnose.
    #
    # From `combined` rather than `tail`: `tail` is bound only on the branches above, and reading it here
    # raised `UnboundLocalError` the first time this ran. Thirty lines, because a pytest failure's useful
    # part — the assertion and the values — sits above the summary line.
    return Result(
        row,
        OK,
        f"failed as required (exit {completed.returncode})\n" + "\n".join(combined.strip().splitlines()[-30:]),
    )


def run_go_row(row: Row, tmp: Path) -> Result:
    module_dir = REPO_ROOT / row.module_dir
    original = REPO_ROOT / row.original
    replacement = REPO_ROOT / row.overlay
    for label, path in (("original", original), ("overlay", replacement)):
        if not path.is_file():
            return Result(row, ERROR, f"{label} file not found: {path}")

    # `go build -overlay` redirects the compiler at a replacement file, so the
    # mutation is never written into the tracked tree.
    #
    # The "from" path must be spelled exactly as the go command refers to the file, or
    # the overlay is SILENTLY IGNORED and the mutated build is byte-identical to the
    # real one. Observed directly while verifying P-07's deadline clause: an overlay
    # written with MSYS-style `/c/...` paths produced a passing run and therefore a
    # false VACUOUS report. `Path` here yields the native form, which is correct on
    # both platforms.
    #
    # The failure direction is at least the safe one: a non-applying overlay makes the
    # property pass and the row is reported VACUOUS, which is a false alarm demanding
    # investigation rather than a false pass hiding a decorative property.
    staged = tmp / f"{row.ident}-{replacement.name}"
    staged.write_bytes(replacement.read_bytes())
    overlay_json = tmp / f"{row.ident}-overlay.json"
    overlay_json.write_text(
        '{"Replace": {%s: %s}}'
        % (
            _json_string(str(original)),
            _json_string(str(staged)),
        ),
        encoding="utf-8",
    )

    # Argument ORDER is load-bearing and was wrong until leaf 7.10 added the first Go row.
    #
    # `-rapid.nofailfile` is a flag of the TEST BINARY, not of `go test`. Placed before the
    # package pattern, `go test` stops parsing its own flags at the first one it does not
    # recognise and treats everything after it as a package list — so the pattern was consumed as
    # a flag value and the command resolved to `.`, producing `no Go files in <module>` and
    # `FAIL . [setup failed]`. The harness read that non-zero exit as "failed as required" and
    # reported the row healthy, which means the control had never run at all. Exactly the Q-04
    # lesson in the other runtime: a control that CRASHES is indistinguishable from one that bites
    # if you only read the exit code.
    #
    # `-rapid.nofailfile` itself is needed because a failing rapid property writes a reproduction
    # file under `testdata/rapid/`, and every Go row here is EXPECTED to fail. Left on, the harness
    # would dirty the tree it asserts is clean, for a reason unrelated to the mutation.
    argv = ["go", "test", f"-overlay={overlay_json}", "-count=1"]
    if row.test_run:
        argv += ["-run", row.test_run]
    argv.append(row.package or "./...")
    argv.append("-rapid.nofailfile")

    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        cwd=str(module_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return Result(row, VACUOUS, "the Go property PASSED under its own negative control")
    combined = (completed.stdout or "") + (completed.stderr or "")
    # A Go build or setup failure also exits non-zero, and reporting it as "failed as required"
    # is the same dishonesty the Python side's skipped-run guard closes: the control never ran.
    # `setup failed` and `no Go files in` were both produced while verifying Q-01 by hand — the
    # first from a mis-ordered `-run`/package argv, the second from a package pattern the module
    # could not resolve.
    for shape in ("build failed", "cannot find", "syntax error", "setup failed", "no Go files in"):
        if shape in combined:
            return Result(
                row,
                ERROR,
                f"the mutated build did not run ({shape}), so the control never ran: " + combined.strip()[-300:],
            )
    return Result(row, OK, f"failed as required (exit {completed.returncode})")


def _json_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ── the working-tree guarantee ───────────────────────────────────────────────


def git_status_is_clean() -> tuple[bool, str]:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "status", "--porcelain"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return False, f"git status failed: {completed.stderr.strip()}"
    return not completed.stdout.strip(), completed.stdout.strip()


# ── reporting ────────────────────────────────────────────────────────────────


def print_table(results: list[Result], *, show_output: bool = False) -> None:
    if not results:
        print("(no rows ran)")
        return
    width_id = max(len(r.row.ident) for r in results)
    width_prop = max(len(r.row.property_path) for r in results)
    header = f"{'ID'.ljust(width_id)}  {'PROPERTY'.ljust(width_prop)}  EXPECTED  OBSERVED"
    print(header)
    print("-" * len(header))
    for result in results:
        print(f"{result.row.ident.ljust(width_id)}  {result.row.property_path.ljust(width_prop)}  FAIL      {result.status}")
    print()
    for result in results:
        if result.status != OK:
            print(f"{result.status} {result.row.ident}: {result.row.mutation}")
            print(f"    {result.detail}")

    # An OK row's detail is normally uninteresting — the property failed, which is what was wanted, and
    # printing its traceback for thirty-one passing controls would bury the table.
    #
    # It is exactly what `control-of-the-control.py` needs, though, and that is why this flag exists. For
    # the meta-check an OK verdict on a NEUTRALISED row is a FAILURE: the property objected to something
    # other than the mutation, and the only thing that says what is the property's own output. Without
    # this, a failing control reported `EXPECTED FAIL OBSERVED OK` and twelve lines of the harness's own
    # table — enough to know a row broke and not enough to know why, which cost a debugging round trip
    # through CI.
    if show_output:
        for result in results:
            if result.status == OK and result.detail:
                print(f"\n--- {result.row.ident} property output ---")
                print(result.detail)


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run every property's negative control (design.md §0.4.5)")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="run every row in mutations.toml")
    selection.add_argument("--only", nargs="+", metavar="Q-NN", help="run just these rows")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=MUTATIONS_TOML,
        help="alternative mutations.toml; for the harness's own meta tests",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="skip the Appendix B completeness clause only (task 19.2 removes this from CI)",
    )
    parser.add_argument(
        "--skip-git-check",
        action="store_true",
        help="skip the clean-tree assertion; for the harness's own meta tests only",
    )
    parser.add_argument(
        "--show-output",
        action="store_true",
        help=(
            "print the property's own output for rows that PASSED their control. Used by "
            "control-of-the-control.py, for which an OK verdict on a neutralised row is a failure that "
            "has to be diagnosed"
        ),
    )
    args = parser.parse_args(argv)

    if not args.all and not args.only:
        parser.error("choose --all or --only")

    rows = load_rows(args.manifest)

    exit_code = 0

    # Completeness against the authority, before running anything: a missing row is
    # a missing control, which is indistinguishable from an untested property.
    if args.manifest == MUTATIONS_TOML:
        expected = appendix_b_ids()
        missing = [q for q in expected if q not in rows]
        print(f"mutation-harness: Appendix B defines {len(expected)} properties; mutations.toml carries {len(rows)} rows")
        if missing:
            message = f"{len(missing)} Appendix B propert{'y' if len(missing) == 1 else 'ies'} have no control: {', '.join(missing)}"
            if args.allow_incomplete:
                print(f"INCOMPLETE (allowed): {message}")
            else:
                print(f"ERROR: {message}", file=sys.stderr)
                exit_code = 1

    selected = list(rows) if args.all else list(args.only or [])
    unknown = [q for q in selected if q not in rows]
    if unknown:
        print(f"ERROR: no such row(s) in {args.manifest}: {', '.join(unknown)}", file=sys.stderr)
        return 1

    tmp = make_outside_tempdir()
    print(f"mutation-harness: temp directory {tmp} (outside {REPO_ROOT})")
    results: list[Result] = []
    try:
        for ident in selected:
            row = rows[ident]
            runner = run_go_row if row.runtime == "go" else run_python_row
            results.append(runner(row, tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print_table(results, show_output=args.show_output)

    if any(r.status != OK for r in results):
        exit_code = 1

    if not args.skip_git_check:
        clean, detail = git_status_is_clean()
        if clean:
            print("mutation-harness: working tree is clean after the run")
        else:
            print("ERROR: the working tree is dirty after the run:", file=sys.stderr)
            print(detail, file=sys.stderr)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
