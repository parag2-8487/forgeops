#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Refuse a phase reference that names a phase or subsection `phases.md` does not have.

WHY THIS EXISTS. Phase 3 (Observe, Troubleshoot & Self-Heal) was merged into Phase 2, the former
Phase 4 became Phase 3 and the former Phase 5 became Phase 4. Every document that said "deferred to
Phase 5", "OTel SDK is Phase 3" or "Phase 4 §4.4" then pointed somewhere else — and a five-phase plan
whose 2.x and 3.x sections overlap misleads the next reader far more effectively than a missing note
would. Renumbering is a one-line edit in each place and impossible to do exhaustively by hand, which
is exactly the kind of thing a gate is for.

WHAT IT CHECKS. `phases.md` is the authority: this reads the phase headings and the `#### N.M`
subsection headings out of it, then fails any reference elsewhere in the repository that names a phase
number or a `N.M` subsection id that does not exist there.

WHAT IT DELIBERATELY ALLOWS.
  * A line containing the word `former` or `formerly`. The merge note itself has to be able to say
    "the former Phase 4 became Phase 3", and a check that forbade its own explanation would be
    replaced by a `# noqa` within a week.
  * `phases.md` itself is scanned, because a stale internal cross-reference is the worst kind.
  * `P<phase>+` and `Phase <phase>+` forms, as long as the phase exists.
  * Anything under `.git/`, build output, or a lockfile: those record history or third-party content.

WHAT IT DOES NOT CHECK. Whether a reference points at the RIGHT phase — only that the phase and
subsection exist. "Deferred to Phase 4" for something Phase 2 delivers is a content error a human has
to see; this gate closes the mechanical half so the human half is visible.

    python scripts/check-phase-references.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PHASES = REPO / "phases.md"

#: `## Phase 2: Deploy, Manage, Observe & Self-Heal`, and `## Phase 0.5` style half-phases.
_PHASE_HEADING = re.compile(r"^##\s+Phase\s+(\d+(?:\.\d+)?)\s*[:—-]")
#: `#### 2.4a Inngest Integration`
_SUBSECTION = re.compile(r"^####\s+(\d+\.\d+[a-z]?)\s")

#: A reference to a whole phase: `Phase 3`, `Phase 3+`, `phase-3`.
_PHASE_REF = re.compile(r"\bPhase[\s-](\d+)(\+)?\b", re.IGNORECASE)

#: A subsection reference, in either the `P2.4a` form the dependency notes use or the `§2.4a` /
#: `2.4a` form a prose cross-reference uses.
#:
#: ATTRIBUTED TO A DOCUMENT, because `§` in this repository means `design.md` far more often than it
#: means `phases.md`: `design §2.2.1`, `PRD §3.17` and `phases.md §2.3` all appear, sometimes on the
#: same line. Checking every `§N.M` against the phase plan reported nine false positives on the first
#: run — a gate that cries wolf about `PRD §3.17` is one nobody reads. So a token counts only when
#: the nearest preceding document marker on the line is the phase plan, or when it is written in the
#: unambiguous `P<phase>.<subsection>` form.
_SUBSECTION_TOKEN = re.compile(r"(?:\bP|§|\bsection\s+)(\d+\.\d+[a-z]?)\b")
_DOC_MARKER = re.compile(
    r"phases\.md|PRD|design(?:\.md)?|Tech-Stack|research|README", re.IGNORECASE
)
_PHASES_MARKER = re.compile(r"phases\.md", re.IGNORECASE)

#: Files whose phase numbers are not claims about this plan.
_SKIP_SUFFIXES = (".lock", ".sum", ".png", ".svg", ".ico", ".woff", ".woff2")
_SKIP_PARTS = (".git", "node_modules", ".next", ".venv", "dist", "coverage", "htmlcov")

#: Lines that are allowed to name a phase that no longer exists.
_HISTORICAL = re.compile(r"\bformer(ly)?\b", re.IGNORECASE)

#: THIS GATE AND ITS TEST ARE EXEMPT, and the reason is not convenience. Both have to quote invalid
#: references verbatim — the failure message shows what a stale one looks like, and the test constructs
#: several and requires them to be refused. Scanning them would make the gate fail on its own evidence,
#: and the only way to keep it green would be to stop stating what it catches. The exemption is exactly
#: two paths rather than a pattern, so it cannot quietly widen.
_SELF_EXEMPT = frozenset(
    {
        "scripts/check-phase-references.py",
        "backend/tests/meta/test_check_phase_references.py",
    }
)


def tracked_files() -> list[Path]:
    """Every tracked file, because an untracked scratch file is not a claim the repository makes."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [REPO / name for name in out.split("\0") if name]


def known() -> tuple[set[str], set[str]]:
    phases: set[str] = set()
    subsections: set[str] = set()
    for line in PHASES.read_text(encoding="utf-8").splitlines():
        heading = _PHASE_HEADING.match(line)
        if heading:
            phases.add(heading.group(1))
            # `Phase 0.5` also makes `0` a legitimate phase to name.
            phases.add(heading.group(1).split(".")[0])
        sub = _SUBSECTION.match(line)
        if sub:
            subsections.add(sub.group(1))
            subsections.add(sub.group(1).rstrip("abcdefghijklmnopqrstuvwxyz"))
    if not phases:
        raise SystemExit("check-phase-references: phases.md yielded no phase headings")
    return phases, subsections


def interesting(path: Path) -> bool:
    if path.suffix.lower() in _SKIP_SUFFIXES:
        return False
    if any(part in _SKIP_PARTS for part in path.parts):
        return False
    return True


def _owned_by_the_phase_plan(line: str, match: re.Match[str]) -> bool:
    """Whether this subsection token is a reference to `phases.md` rather than to another document.

    The `P2.4a` form is unambiguous — nothing else in this repository is numbered that way. For the
    `§2.4a` and `section 2.4a` forms, the owner is the nearest document marker to the LEFT on the
    same line, which is how these cross-references are actually written: `phases.md §2.3, §2.6 and
    §2.8` attributes three tokens to one marker, and `PRD §3.17 … phases.md §2.3` switches owner
    mid-line. With no marker at all the token is not attributed and is left alone, because an
    unqualified `§2.3` in a design document means that document's own section 2.3.
    """
    if line[match.start() : match.start() + 1] == "P":
        return True
    markers = list(_DOC_MARKER.finditer(line[: match.start()]))
    if not markers:
        return False
    return bool(_PHASES_MARKER.fullmatch(markers[-1].group(0)))


def main() -> int:
    # A Windows console defaults to cp1252, and the failure lines quote source text that contains
    # arrows and section signs. Without this the gate crashes on the very output that explains it.
    for stream in (sys.stdout, sys.stderr):
        with_reconfigure = getattr(stream, "reconfigure", None)
        if with_reconfigure is not None:
            with_reconfigure(encoding="utf-8", errors="replace")

    phases, subsections = known()
    # The subsection check only applies to phases that HAVE subsections in `phases.md`; Phase 0.5 and
    # any phase documented without `####` headings would otherwise fail every reference to it.
    with_subsections = {sub.split(".")[0] for sub in subsections}

    failures: list[str] = []
    for path in tracked_files():
        if not interesting(path):
            continue
        if path.relative_to(REPO).as_posix() in _SELF_EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _HISTORICAL.search(line):
                continue
            relative = path.relative_to(REPO).as_posix()
            for match in _PHASE_REF.finditer(line):
                if match.group(1) not in phases:
                    failures.append(
                        f"{relative}:{number}: names 'Phase {match.group(1)}', "
                        f"which phases.md does not have (phases: {sorted(phases)})\n"
                        f"    {line.strip()[:160]}"
                    )
            for match in _SUBSECTION_TOKEN.finditer(line):
                identifier = match.group(1)
                phase = identifier.split(".")[0]
                if phase not in with_subsections:
                    continue
                if not _owned_by_the_phase_plan(line, match):
                    continue
                if identifier not in subsections:
                    failures.append(
                        f"{relative}:{number}: names phase subsection '{identifier}', "
                        f"which phases.md does not have\n    {line.strip()[:160]}"
                    )

    if failures:
        print(f"check-phase-references: {len(failures)} stale reference(s)\n")
        for failure in failures:
            print(f"  {failure}")
        print(
            "\nEach one names a phase or subsection that does not exist. Renumber it, or — if the "
            "line is deliberately describing the old plan — say 'former' in it."
        )
        return 1

    print(
        f"check-phase-references: ok — every phase reference resolves "
        f"({len(phases)} phases, {len(subsections)} subsection ids)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
