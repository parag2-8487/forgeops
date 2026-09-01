#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every command string the UI or the docs render must be composed of flags the CLI accepts.

WHY THIS GATE EXISTS

A rendered command carried `scan --path`, a flag `forgeops-agent scan` has never accepted. Nothing
caught it: the frontend builds a string, the CLI parses a string, and no test compares the two. It
was found by running it.

The same class of defect is why the Windows first run failed at step three. The UI printed

    forgeops-agent pair --code BYDPQC

with no `--backend`, which `pair` refuses, and with a bare program name that PowerShell will not
execute. Every one of those is a claim about the CLI made in a file that never sees the CLI.

WHAT IT CHECKS

1. Every verb in `frontend/features/agent/commands.ts`'s `AGENT_COMMANDS` is registered in
   `agent/internal/app`.
2. Every flag it lists for a verb is registered on that verb.
3. Every flag the CLI registers is either listed or explicitly declared as not rendered, so a new
   flag is a decision rather than an omission.
4. Every `forgeops-agent` command string in the docs and the UI parses into a known verb and known
   flags.
5. The archive names the UI builds match GoReleaser's `name_template`.

WHAT IT DELIBERATELY DOES NOT DO. It does not run the CLI. Parsing the source is what lets it work
on a machine with no Go toolchain and in a pre-commit hook, and the registration calls it reads are
the same lines cobra reads.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_APP = ROOT / "agent" / "internal" / "app"
COMMANDS_TS = ROOT / "frontend" / "features" / "agent" / "commands.ts"
GORELEASER = ROOT / "agent" / ".goreleaser.yaml"

#: Files whose rendered `forgeops-agent ...` strings are checked.
RENDERED_SOURCES = (
    ROOT / "frontend" / "app",
    ROOT / "frontend" / "features",
    ROOT / "docs",
    ROOT / "README.md",
)

#: Flags the CLI has that the UI deliberately never renders, with the reason.
#:
#: Explicit so that adding a flag to the CLI forces a decision here rather than silently passing.
NOT_RENDERED: dict[tuple[str, str], str] = {
    ("pair", "wipe"): "destructive; recovering from a half-paired state is an operator action, "
    "not something to put a button next to a pairing code",
    ("watch", "debounce"): "tuning, not part of any first run",
    ("watch", "once"): "a testing aid",
}

#: Verbs that exist but are never rendered by the UI.
NOT_RENDERED_VERBS: dict[str, str] = {
    "mcp-serve": "started by an MCP client through its own configuration, never typed by a user",
}


def fail(message: str) -> None:
    print(f"check-rendered-commands: FAIL {message}")
    sys.exit(1)


def go_cli_surface() -> dict[str, set[str]]:
    """Parse the verbs and flags cobra registers, from the Go source.

    Each `newXxxCmd` function is read as a unit: the `Use:` inside it names the verb, and the
    `cmd.Flags().XxxVar(..., "name"` calls inside it name that verb's flags. Splitting on the
    function boundary is what keeps one verb's flags from being attributed to another.
    """
    surface: dict[str, set[str]] = {}
    for path in sorted(AGENT_APP.glob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        source = path.read_text(encoding="utf-8")
        # Split into constructor functions. The root command is not a verb and has no `Use:` of the
        # bare-word form the pattern below matches, so it drops out naturally.
        for block in re.split(r"\nfunc new(?=[A-Z])", source):
            use = re.search(r'Use:\s+"([a-z][a-z-]*)"', block)
            if use is None:
                continue
            verb = use.group(1)
            if verb == "forgeops-agent":
                continue
            flags = set(re.findall(r'cmd\.Flags\(\)\.\w+Var\(&\w+,\s*"([a-z][a-z-]*)"', block))
            surface.setdefault(verb, set()).update(flags)
    if not surface:
        fail(f"no verbs were parsed out of {AGENT_APP}; the parse is broken, not the code")
    return surface


def declared_surface() -> dict[str, set[str]]:
    """Parse `AGENT_COMMANDS` out of the TypeScript module."""
    source = COMMANDS_TS.read_text(encoding="utf-8")
    body = re.search(r"export const AGENT_COMMANDS = \{(.*?)\n\} as const;", source, re.DOTALL)
    if body is None:
        fail(f"AGENT_COMMANDS was not found in {COMMANDS_TS}")
        raise AssertionError("unreachable")

    declared: dict[str, set[str]] = {}
    for line in body.group(1).splitlines():
        entry = re.search(r'verb:\s*"([a-z][a-z-]*)",\s*flags:\s*\[([^\]]*)\]', line)
        if entry is None:
            continue
        declared[entry.group(1)] = set(re.findall(r'"([a-z][a-z-]*)"', entry.group(2)))
    if not declared:
        fail("no verbs were parsed out of AGENT_COMMANDS")
    return declared


def check_declaration_matches_cli(cli: dict[str, set[str]], declared: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []

    for verb, flags in declared.items():
        if verb not in cli:
            problems.append(
                f"the UI declares the verb `{verb}` but the CLI registers no such command; "
                f"the CLI has: {', '.join(sorted(cli))}"
            )
            continue
        for flag in sorted(flags - cli[verb]):
            problems.append(
                f"the UI declares `{verb} --{flag}` but the CLI does not accept it. This is the "
                f"`scan --path` defect: a flag invented in the UI reaches a user as a command that "
                f"fails. `{verb}` accepts: {', '.join('--' + f for f in sorted(cli[verb])) or '(no flags)'}"
            )

    for verb, flags in cli.items():
        if verb not in declared:
            if verb in NOT_RENDERED_VERBS:
                continue
            problems.append(
                f"the CLI registers `{verb}` but the UI neither declares it nor records why not. "
                f"Add it to AGENT_COMMANDS, or to NOT_RENDERED_VERBS with a reason"
            )
            continue
        for flag in sorted(flags - declared[verb]):
            if (verb, flag) in NOT_RENDERED:
                continue
            problems.append(
                f"the CLI accepts `{verb} --{flag}` and the UI neither renders it nor records why "
                f"not. Add it to that verb's flags, or to NOT_RENDERED with a reason"
            )

    return problems


def check_rendered_strings(cli: dict[str, set[str]]) -> list[str]:
    """Every literal `forgeops-agent ...` string in the UI and docs must name real flags."""
    problems: list[str] = []
    # The verb, then EVERYTHING to the end of the string literal or line.
    #
    # Matching only flags that immediately follow the verb was not enough, and a control test proved
    # it: `forgeops-agent scan --project ${projectId} --path .` slipped through, because the
    # template interpolation ended the match before `--path`. Anything up to the closing backtick,
    # quote or newline is part of the command the user sees, so that is what gets scanned.
    pattern = re.compile(r"forgeops-agent(?:\.exe)?\s+([a-z][a-z-]*)([^`\"'\n<]*)")

    files: list[Path] = []
    for source in RENDERED_SOURCES:
        if source.is_file():
            files.append(source)
        elif source.is_dir():
            files.extend(
                p
                for p in source.rglob("*")
                if p.is_file() and p.suffix in {".tsx", ".ts", ".md"} and "node_modules" not in p.parts
            )

    for path in sorted(files):
        # The command builder itself names flags inside its own declaration, and the gate above
        # already checks that declaration against the CLI. Reading it here would double-report.
        if path == COMMANDS_TS:
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            verb, tail = match.group(1), match.group(2)
            if verb not in cli:
                problems.append(
                    f"{path.relative_to(ROOT)}: renders `forgeops-agent {verb}`, which is not a "
                    f"command. The CLI has: {', '.join(sorted(cli))}"
                )
                continue
            for flag in re.findall(r"--([a-z][a-z-]+)", tail):
                if flag not in cli[verb]:
                    problems.append(
                        f"{path.relative_to(ROOT)}: renders `{verb} --{flag}`, which the CLI does "
                        f"not accept. `{verb}` accepts: "
                        f"{', '.join('--' + f for f in sorted(cli[verb])) or '(no flags)'}"
                    )
    return problems


def check_archive_names() -> list[str]:
    """The UI's archive names must match what GoReleaser actually produces."""
    if not GORELEASER.exists():
        return [f"{GORELEASER} is missing, so the download links cannot be checked"]

    goreleaser = GORELEASER.read_text(encoding="utf-8")
    # Scoped to the `archives:` block. Reading the first `name_template:` in the file finds
    # `checksum:`'s, which is `checksums.txt` and has no fields at all — a false failure that says
    # nothing about the download links.
    archives = re.search(r"\narchives:\n(.*?)(?=\n[a-z_]+:\n)", goreleaser, re.DOTALL)
    if archives is None:
        return ["no archives: block found in .goreleaser.yaml"]
    template = re.search(r'name_template:\s*"([^"]+)"', archives.group(1))
    if template is None:
        return [
            "the archives: block declares no name_template, so the archive names come from a "
            "GoReleaser default that has changed capitalisation between major versions. Declare it, "
            "or a toolchain upgrade turns every download link into a 404"
        ]

    rendered = COMMANDS_TS.read_text(encoding="utf-8")
    if "forgeops-agent_" not in rendered:
        return ["the UI's archiveName no longer builds a forgeops-agent_ prefixed name"]

    # The UI builds `<project>_<version>_<goos>_<arch>.<ext>` with a lowercase OS. Every part of
    # that has to be true of the template, in that order.
    expected = "{{ .ProjectName }}_{{ .Version }}_{{ .Os }}_{{ .Arch }}"
    if template.group(1) != expected:
        return [
            f"the archive name_template is {template.group(1)!r} but the UI's archiveName builds "
            f"{expected!r}; a download link built from it would not resolve"
        ]
    if "title" in template.group(1).lower():
        return [
            "the archive name_template title-cases a field, but the UI builds lowercase names; the "
            "download links would 404"
        ]
    return []


def main() -> int:
    if not COMMANDS_TS.exists():
        fail(f"{COMMANDS_TS} is missing; it is the single source for rendered commands")

    cli = go_cli_surface()
    declared = declared_surface()

    problems = check_declaration_matches_cli(cli, declared)
    problems += check_rendered_strings(cli)
    problems += check_archive_names()

    if problems:
        for problem in problems:
            print(f"  - {problem}")
        fail(f"{len(problems)} problem(s) in rendered commands")

    flag_count = sum(len(f) for f in cli.values())
    print(
        f"check-rendered-commands: ok, {len(cli)} verb(s) and {flag_count} flag(s) agree between "
        f"the CLI, the UI and the docs"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
