#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every `shell: pwsh` block in a workflow must parse as PowerShell.

WHY THIS GATE EXISTS

The Windows host-binary job is a page of PowerShell embedded in YAML, and a syntax error in it is
invisible until a runner reaches that step -- several minutes into a run, on a platform no other job
uses. The first version of that job contained `Join-Path $PWD (if ($IsWindows) { ... })`, which is not
valid PowerShell: `if` is a statement, so in an argument position it needs `$(...)`. Nothing in the
repository could have caught it.

WHAT IT DOES

Extracts every `run:` script whose effective shell is `pwsh` or `powershell` and parses it with the
PowerShell parser itself, through `powershell -Command`. Parsing rather than running: these scripts
build binaries and write to credential stores, and the question here is only whether they are
syntactically valid.

WHEN POWERSHELL IS ABSENT -- on the Linux runner where CI executes this -- it uses `pwsh` if present
and otherwise reports that it could not check, WITHOUT failing. That is a deliberate limit and not a
silent pass: the same gate runs on the Windows host job, where a parser is guaranteed, so the check
has a home that cannot skip. `check-gate-reachability.py` sees it either way.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

#: GitHub's own default shell on Windows runners is `pwsh`, so a step with no explicit shell on a
#: Windows job is PowerShell too. Both spellings are checked.
POWERSHELL_SHELLS = {"pwsh", "powershell"}


def powershell_executable() -> str | None:
    for candidate in ("pwsh", "powershell"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def job_is_windows(job: dict) -> bool:
    runs_on = json.dumps(job.get("runs-on", ""))
    matrix = json.dumps(job.get("strategy", {}).get("matrix", {}))
    return "windows" in runs_on.lower() or "windows" in matrix.lower()


def powershell_blocks() -> list[tuple[str, str, str, str]]:
    """Return (workflow, job, step name, script) for every PowerShell `run:`."""
    blocks: list[tuple[str, str, str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (document.get("jobs") or {}).items():
            default_shell = (job.get("defaults") or {}).get("run", {}).get("shell")
            for step in job.get("steps") or []:
                script = step.get("run")
                if not script:
                    continue
                shell = step.get("shell") or default_shell
                # A Windows job with no declared shell runs pwsh, which is the case most likely to
                # hide a syntax error: nothing in the YAML says "PowerShell" at all.
                if shell is None and job_is_windows(job):
                    shell = "pwsh"
                if shell not in POWERSHELL_SHELLS:
                    continue
                blocks.append((path.name, job_name, step.get("name", "(unnamed)"), script))
    return blocks


def parse_error(executable: str, script: str) -> str | None:
    """Parse `script` and return the first error, or None."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "block.ps1"
        # `write_bytes` rather than `write_text`: on Windows the latter translates newlines, and a
        # here-string in a script would then be parsed with different line endings than CI sees.
        target.write_bytes(script.encode("utf-8"))
        checker = Path(tmp) / "check.ps1"
        checker.write_bytes(
            (
                "$errors = $null\n"
                "$null = [System.Management.Automation.Language.Parser]::ParseFile("
                f"'{target.as_posix()}', [ref]$null, [ref]$errors)\n"
                "if ($errors -and $errors.Count -gt 0) {\n"
                "  foreach ($e in $errors) { Write-Output \"$($e.Extent.StartLineNumber): $($e.Message)\" }\n"
                "  exit 1\n"
                "}\n"
            ).encode("utf-8")
        )
        result = subprocess.run(  # noqa: S603 - a fixed interpreter and a generated script
            [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(checker)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return None
        return (result.stdout + result.stderr).strip()


def check_the_construct_that_broke() -> list[str]:
    """`if` in an argument position, which is the specific error this gate was written for.

    A textual check as well as a parse, because it is cheap and because it works on a machine with no
    PowerShell at all -- so the one mistake already made here cannot come back even where the parser
    is unavailable.
    """
    problems: list[str] = []
    # A `(` that opens an `if`, NOT preceded by `$`. The lookbehind is the whole check: `$(if ...)` is
    # correct and `(if ...)` is not, and they differ by one character.
    pattern = re.compile(r"(?<!\$)\(\s*if\s*\(")
    for workflow, job, step, script in powershell_blocks():
        for number, line in enumerate(script.splitlines(), start=1):
            if pattern.search(line):
                problems.append(
                    f"{workflow}:{job}:{step} line {number}: `if` is used in an argument position. "
                    f"PowerShell needs `$(if ...)` there, not `(if ...)`:\n      {line.strip()}"
                )
    return problems


def main() -> int:
    blocks = powershell_blocks()
    if not blocks:
        print("check-powershell-syntax: FAIL no PowerShell blocks found; the extraction is broken")
        return 1

    problems = check_the_construct_that_broke()

    executable = powershell_executable()
    if executable is None:
        for problem in problems:
            print(f"  - {problem}")
        if problems:
            print(f"check-powershell-syntax: FAIL {len(problems)} problem(s)")
            return 1
        print(
            f"check-powershell-syntax: {len(blocks)} block(s) pass the textual check; no PowerShell "
            f"parser on this machine, so the full parse runs in the agent-host job instead"
        )
        return 0

    for workflow, job, step, script in blocks:
        error = parse_error(executable, script)
        if error:
            problems.append(f"{workflow}:{job}:{step} does not parse:\n      {error}")

    if problems:
        for problem in problems:
            print(f"  - {problem}")
        print(f"check-powershell-syntax: FAIL {len(problems)} problem(s)")
        return 1

    print(f"check-powershell-syntax: ok, {len(blocks)} PowerShell block(s) parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
