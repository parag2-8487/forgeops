# SPDX-License-Identifier: FSL-1.1-ALv2
"""The phase-reference check's own tests.

The former Phase 3 was merged into Phase 2, the former Phase 4 became Phase 3 and the former Phase 5
became Phase 4. Twelve references across seven files then named phases that no longer exist — "deferred
to the former Phase 5", "OTel SDK is the former Phase 3", "the former Phase 4 §4.4". A renumbering is
trivial per line and impossible to do exhaustively by hand, and a plan whose 2.x and 3.x sections
overlap misleads every later reader.

These tests prove the gate actually fires rather than merely existing, which is this repository's
recurring defect class: the first version of the check reported nine false positives because `§` means
`design.md` far more often than it means `phases.md`, and a gate that cries wolf is a gate nobody reads.
So the attribution rule is pinned here too — `PRD §3.17` must NOT be judged against the phase plan, and
`phases.md §3.17` must be.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-phase-references.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("forgeops_check_phase_references", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECK = _load()


def _token(line: str) -> re.Match[str]:
    match = CHECK._SUBSECTION_TOKEN.search(line)
    assert match is not None, f"no subsection token found in {line!r}"
    return match


class TestTheRealTree:
    def test_the_repository_has_no_stale_phase_reference(self) -> None:
        """The gate's own subject. A failure here is a real stale reference, not a broken test."""
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_phase_plan_is_the_authority_and_yields_both_sets(self) -> None:
        phases, subsections = CHECK.known()

        # Read rather than asserted as literals where it can be: the point is that the sets come from
        # `phases.md`. The two anchors are the merge's own outcome and are worth pinning.
        assert "5" not in phases, "a fifth phase would mean the merge was reverted"
        assert {"0", "1", "2", "3", "4"} <= phases
        assert "2.14" in subsections, "the merged Phase 2 ends at 2.14 (knowledge base mode)"
        assert "2.4a" in subsections, "the lettered subsections must be recognised"

    def test_the_self_exemption_is_exactly_the_gate_and_this_test(self) -> None:
        """An exemption list is the cheapest way to make a gate green, so its width is pinned here."""
        assert CHECK._SELF_EXEMPT == {
            "scripts/check-phase-references.py",
            "backend/tests/meta/test_check_phase_references.py",
        }


class TestAttribution:
    """A `§N.M` token belongs to the document named nearest to its left."""

    def test_a_prd_section_is_not_judged_against_the_phase_plan(self) -> None:
        line = "PRD §3.17 assigns FR-97 to a later phase."
        assert not CHECK._owned_by_the_phase_plan(line, _token(line))

    def test_a_phase_plan_section_is_judged(self) -> None:
        line = "`phases.md` §2.3 lists it as a deliverable."
        assert CHECK._owned_by_the_phase_plan(line, _token(line))

    def test_the_owner_switches_mid_line(self) -> None:
        line = "PRD §3.17 disagrees with phases.md §2.6 about the phase."
        tokens = list(CHECK._SUBSECTION_TOKEN.finditer(line))
        assert [match.group(1) for match in tokens] == ["3.17", "2.6"]
        assert not CHECK._owned_by_the_phase_plan(line, tokens[0])
        assert CHECK._owned_by_the_phase_plan(line, tokens[1])

    def test_the_p_form_needs_no_document_marker(self) -> None:
        """`P3.9` is unambiguous — nothing else in this repository is numbered that way."""
        line = "P3.9 DORA Analytics depends on P2.3."
        assert CHECK._owned_by_the_phase_plan(line, _token(line))

    def test_an_unqualified_section_reference_is_left_alone(self) -> None:
        """An unqualified `§2.2` in a design document means that document's own section."""
        line = "The chokepoint is confined by §2.2 and nothing bypasses it."
        assert not CHECK._owned_by_the_phase_plan(line, _token(line))


class TestTheGateFires:
    """Proven by construction: a tree containing a stale reference must fail."""

    def test_a_reference_to_a_removed_phase_is_refused(self, tmp_path: Path) -> None:
        offender = tmp_path / "notes.md"
        offender.write_text("Deferred to Phase 5 for now.\n", encoding="utf-8")

        phases, subsections = CHECK.known()
        failures = _scan(offender, tmp_path, phases, subsections)

        assert failures, "a reference to Phase 5 must be refused"
        assert "Phase 5" in failures[0]

    def test_a_reference_to_a_removed_subsection_is_refused(self, tmp_path: Path) -> None:
        offender = tmp_path / "notes.md"
        # `4.9` was the former Phase 4's DORA analytics. The new Phase 4 stops at 4.6, and DORA is now
        # 3.9 — so this is a live-looking id that resolves to nothing. `3.2` would NOT do: the new
        # Phase 3 has a 3.2, which is exactly why a renumbering is dangerous rather than merely untidy.
        offender.write_text("See phases.md §4.9 for the DORA metrics.\n", encoding="utf-8")

        phases, subsections = CHECK.known()
        failures = _scan(offender, tmp_path, phases, subsections)

        assert failures, "§4.9 no longer exists: the former 4.9 is now 3.9"
        assert "4.9" in failures[0]

    def test_a_line_that_says_former_is_allowed_to_name_the_old_plan(self, tmp_path: Path) -> None:
        offender = tmp_path / "notes.md"
        offender.write_text("The former Phase 5 became Phase 4.\n", encoding="utf-8")

        phases, subsections = CHECK.known()

        assert _scan(offender, tmp_path, phases, subsections) == []

    def test_a_live_reference_is_accepted(self, tmp_path: Path) -> None:
        offender = tmp_path / "notes.md"
        offender.write_text("Self-healing is phases.md §2.12, in Phase 2.\n", encoding="utf-8")

        phases, subsections = CHECK.known()

        assert _scan(offender, tmp_path, phases, subsections) == []


def _scan(path: Path, root: Path, phases: set[str], subsections: set[str]) -> list[str]:
    """The gate's per-line judgement, applied to one file outside the repository.

    The script's `main` walks `git ls-files`, so a constructed offender cannot be fed to it without
    writing into the repository — which a test must not do. This reuses the same two matchers and the
    same attribution rule, so what is proven here is the judgement rather than a paraphrase of it.
    """
    with_subsections = {identifier.split(".")[0] for identifier in subsections}
    failures: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if CHECK._HISTORICAL.search(line):
            continue
        for match in CHECK._PHASE_REF.finditer(line):
            if match.group(1) not in phases:
                failures.append(f"{path.relative_to(root)}:{number}: Phase {match.group(1)}")
        for match in CHECK._SUBSECTION_TOKEN.finditer(line):
            identifier = match.group(1)
            if identifier.split(".")[0] not in with_subsections:
                continue
            if not CHECK._owned_by_the_phase_plan(line, match):
                continue
            if identifier not in subsections:
                failures.append(f"{path.relative_to(root)}:{number}: {identifier}")
    return failures
