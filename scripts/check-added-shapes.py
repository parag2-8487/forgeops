# SPDX-License-Identifier: FSL-1.1-ALv2
"""FO-SEC001 at commit time: no credential SHAPE may enter a commit (secret-safety.md).

`scripts/secret-gate.ps1` already answers this question, but it answers it at push time. Three
times now a shape has reached a commit and been discovered by the pre-push gate, and the only
remedy available at that point is a history rewrite — cheap while nothing is published, a
force-push over published history once it is. A gate positioned after the point where the damage
becomes expensive is the same shape as journal pattern O: the check exists, it is correct, and it
runs too late to be the cheap one. This hook is the same grep moved to the cheap position. It costs
seconds; the rewrite it prevents costs a session.

The rule is shape, not sensitivity, and that is deliberate. Every one of the five hits that forced
the last rewrite was harmless — an authorisation scheme constant, a PEM armour line whose body was
the word `nope`, a needle that IS the assertion that no key is transmitted, and prose. A scanner
cannot read intent, and this repository has already collected a GitGuardian incident for a harmless
placeholder. So there is no allowlist and no severity dial: the remedy is the one the repository
already uses everywhere else — assemble the shape from fragments so no source line carries it
(`backend/tests/synthetic_secrets.py`, `scripts/secret-gate.ps1`'s own pattern table, and this
file's).

EVERY REGEX HERE IS ASSEMBLED, NEVER WRITTEN AS A LITERAL, and each `name` is a rule name rather
than the token the rule looks for. Otherwise this file matches itself and the hook blocks the commit
that adds it — which is finding 58's lesson, delivered by the gate against itself.

Usage:
    python scripts/check-added-shapes.py                 # staged change (the pre-commit hook)
    python scripts/check-added-shapes.py --range A..B     # every commit in a range, separately
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass

_GH = "gh"

#: The pattern list of `.kiro/steering/secret-safety.md`, one row per rule, mirroring
#: `scripts/secret-gate.ps1`'s `$Patterns`. `backend/tests/meta/test_check_added_shapes.py`
#: asserts the two tables agree row for row, so the pre-commit hook and the pre-push gate cannot
#: drift apart — a hook that is weaker than the gate it front-runs would restore exactly the
#: failure this file exists to remove.
#:
#: Case sensitivity is per rule and deliberate: provider-literal shapes (AWS key ids, PEM armour)
#: only occur uppercase, while the things we write ourselves (a password assignment, an api-key
#: field) occur in any casing. Folding the list to one flag is what made finding 59's
#: mis-attribution possible.
PATTERNS: tuple[tuple[str, str, bool], ...] = (
    ("bearer-clause", "Bear" + "er ", True),
    ("authz-header", "Author" + "ization:", True),
    ("gh-pat-classic", _GH + "p_", True),
    ("gh-pat-fine", _GH + "ithub_" + "pat_", True),
    ("gh-oauth", _GH + "o_", True),
    ("gh-server", _GH + "s_", True),
    ("openai-key", "s" + "k-", True),
    ("google-key", "AI" + "za", True),
    ("aws-akid", "AK" + "IA", True),
    ("aws-temp-akid", "AS" + "IA", True),
    ("slack-bot", "xo" + "xb-", True),
    ("slack-user", "xo" + "xp-", True),
    ("jwt-header", "ey" + "J", True),
    ("pem-armour", ("-" * 5) + "BE" + "GIN", True),
    ("private-key", "PRIV" + "ATE " + "KEY", True),
    ("client-secret", "client" + "_sec" + "ret", False),
    ("api-key-snake", "api" + "_k" + "ey", False),
    ("api-key-flat", "api" + "key", False),
    ("pw=", "pass" + "word=", False),
    ("pwd-alt", "pass" + "wd=", False),
    ("credential-dsn", "://[^/\\s:@]+:[^/\\s@]+@", False),
)

_COMPILED = tuple((name, re.compile(rx, 0 if case else re.IGNORECASE)) for name, rx, case in PATTERNS)


@dataclass(frozen=True)
class AddedLine:
    """One added line of a unified diff, carrying its new-side position."""

    path: str
    line: int
    text: str


def shapes(text: str) -> list[str]:
    """Every rule that matches `text`, by name.

    All of them rather than the first: an authorisation header carrying a bearer token trips two
    rules, and whoever clears one of them should see the other.
    """
    return [name for name, rx in _COMPILED if rx.search(text)]


def added_lines(diff: str) -> list[AddedLine]:
    """Parse a `--unified=0` diff into its ADDED lines only.

    Context and removed lines never enter the result, so they can never be reported. That is
    finding 58's fix, kept here rather than re-derived: a gate that reports a line the commit
    DELETED teaches its operator to skim it.
    """
    out: list[AddedLine] = []
    path: str | None = None
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            candidate = raw[4:]
            if candidate == "/dev/null":
                path = None
            elif candidate.startswith("b/"):
                path = candidate[2:]
            else:
                path = candidate
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("@@"):
            match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if match:
                line_no = int(match.group(1))
            continue
        if raw.startswith("+"):
            if path is not None:
                out.append(AddedLine(path=path, line=line_no, text=raw[1:]))
            line_no += 1
            continue
        # A `-` line consumes no new-side number; a ' ' context line does, though `--unified=0`
        # emits none. Anything else is diff metadata.
        if raw.startswith(" "):
            line_no += 1
    return out


def findings_for(diff: str) -> tuple[list[str], int]:
    """Return (findings, added-line count) for one unified diff."""
    lines = added_lines(diff)
    found: list[str] = []
    for item in lines:
        hits = shapes(item.text)
        if hits:
            found.append(f"{item.path}:{item.line}: credential shape [{', '.join(hits)}]")
    return found, len(lines)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--range",
        dest="rev_range",
        default=None,
        help="scan every commit in BASE..TIP separately instead of the staged change",
    )
    parser.add_argument("filenames", nargs="*", help="ignored; pre-commit may pass filenames")
    args = parser.parse_args(argv)

    blocked = 0
    if args.rev_range:
        revs = [r for r in _git("rev-list", args.rev_range).split() if r]
        if not revs:
            print(f"check-added-shapes: no commits in {args.rev_range}; nothing scanned")
            return 0
        for rev in revs:
            diff = _git("show", "--format=", "--unified=0", "--no-color", rev)
            found, count = findings_for(diff)
            subject = _git("log", "-1", "--format=%h %s", rev).strip()
            print(f"  {subject} -- {count} added line(s)")
            for finding in found:
                print(f"    {finding}")
            blocked += len(found)
    else:
        diff = _git("diff", "--cached", "--unified=0", "--no-color")
        found, count = findings_for(diff)
        print(f"check-added-shapes: {count} added line(s) considered")
        for finding in found:
            print(f"  {finding}")
        blocked += len(found)

    if blocked:
        print(
            f"\nBLOCKED: {blocked} credential shape(s) in added lines.\n"
            "Shape is the violation, not sensitivity -- a scanner cannot read intent, and an\n"
            "exemption per harmless hit puts a human back in the loop for every future one.\n"
            "Assemble the shape from fragments so no source line carries it; see\n"
            "backend/tests/synthetic_secrets.py. In prose, name the thing instead of spelling it.",
            file=sys.stderr,
        )
        return 1
    print("check-added-shapes: no credential shapes in added lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
