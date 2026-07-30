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
DESIGN_DOC = REPO_ROOT / ".kiro" / "specs" / "phase-1-mvp-core" / "design.md"

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
        ],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    if completed.returncode == 0:
        tail = (completed.stdout or "").strip().splitlines()[-4:]
        return Result(row, VACUOUS, "the property PASSED under its own negative control: " + " | ".join(tail))
    if completed.returncode >= 4:
        # 4 = usage error, 5 = no tests collected. Either means the control never
        # ran, which must not be reported as a healthy failure.
        tail = ((completed.stdout or "") + (completed.stderr or "")).strip().splitlines()[-6:]
        return Result(row, ERROR, f"pytest exited {completed.returncode} (control never ran): " + " | ".join(tail))
    return Result(row, OK, f"failed as required (exit {completed.returncode})")


def run_go_row(row: Row, tmp: Path) -> Result:
    module_dir = REPO_ROOT / row.module_dir
    original = REPO_ROOT / row.original
    replacement = REPO_ROOT / row.overlay
    for label, path in (("original", original), ("overlay", replacement)):
        if not path.is_file():
            return Result(row, ERROR, f"{label} file not found: {path}")

    # `go build -overlay` redirects the compiler at a replacement file, so the
    # mutation is never written into the tracked tree.
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

    argv = ["go", "test", f"-overlay={overlay_json}", "-count=1"]
    if row.test_run:
        argv += ["-run", row.test_run]
    argv.append(row.package or "./...")

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
    if "build failed" in combined or "cannot find" in combined or "syntax error" in combined:
        return Result(row, ERROR, "the overlay did not compile, so the control never ran: " + combined.strip()[-300:])
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


def print_table(results: list[Result]) -> None:
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
    print_table(results)

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
