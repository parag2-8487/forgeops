# SPDX-License-Identifier: FSL-1.1-ALv2
"""`max_write_targets` bounds the number of FILES. Phase 2 completion criteria.

WHY THIS FILE EXISTS
--------------------
The cap was written to stop a small model being asked for more artifacts than it can emit in one
answer -- the comment beside it says so in files throughout: "eleven artifacts", "cannot emit eleven
complete valid files", "six artifacts delivered beats eleven requested and none delivered".

It counted INSTRUCTION SECTIONS instead. A section carries companions -- the Deployment section also
names a Service and an Ingress -- so the bound was loose by exactly the companions. **Measured before
the fix: a cap of 6 produced 8 write targets and a cap of 8 produced 10.**

Every assertion below reads `len(compiled.write_targets)`, which is the quantity the parser accepts
and the model is asked for. None of them looks at the loop, the section count or `kept`: a test that
asserted `len(kept) <= cap` passed throughout the defect, because that was the thing being bounded
and it was the wrong thing.
"""

from __future__ import annotations

import pytest
from src.core.index_evidence import IndexEvidence
from src.core.readiness import ReadinessEngine
from src.generation.prompt_compiler import DEFAULT_MAX_WRITE_TARGETS, compile_prompt

# A project that fails many checks at once, so the compiler has more to ask for than any cap allows.
# A project failing only one check could not distinguish a working cap from an absent one.
PATHS = ("app/main.py", "app/__init__.py", "requirements.txt", "README.md", "tests/test_main.py")
CONTENTS = {
    "app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    "requirements.txt": "fastapi==0.139.2\n",
    "README.md": "# checkout\n",
    "tests/test_main.py": "def test_x():\n    assert True\n",
}


def _compile(cap: int):
    report = ReadinessEngine().evaluate(IndexEvidence(paths=PATHS, contents=dict(CONTENTS)))
    return compile_prompt(
        checks=report.checks,
        paths=PATHS,
        contents=dict(CONTENTS),
        inventory={},
        project_name="checkout",
        max_write_targets=cap,
    )


class TestTheCapBoundsFilesAndNotSections:
    @pytest.mark.parametrize("cap", [4, 5, 6, 7, 8, 9, 10])
    def test_no_cap_is_exceeded(self, cap: int) -> None:
        """The regression, stated as the bound itself.

        6 -> 8 and 8 -> 10 were the measured values before the fix, so 6 and 8 are the two rows that
        would have failed. The others are here because a fix that happened to work at two values and
        not at the rest would be a coincidence rather than a correction.
        """
        compiled = _compile(cap)
        assert len(compiled.write_targets) <= cap, (
            f"cap={cap} produced {len(compiled.write_targets)} write targets: {list(compiled.write_targets)}"
        )

    def test_the_fixture_actually_strains_the_cap(self) -> None:
        """NON-VACUITY. If this project asked for few artifacts, every assertion above would pass on a
        compiler with no cap at all -- which is precisely how the defect survived."""
        uncapped = _compile(99)
        assert len(uncapped.write_targets) > DEFAULT_MAX_WRITE_TARGETS, (
            "the fixture no longer asks for more artifacts than the default cap, so these tests prove "
            "nothing about the cap"
        )

    def test_the_default_cap_delivers_its_named_number(self) -> None:
        """6 means 6. The value the shipped configuration uses, checked at that value."""
        compiled = _compile(DEFAULT_MAX_WRITE_TARGETS)
        assert len(compiled.write_targets) == DEFAULT_MAX_WRITE_TARGETS

    def test_a_lower_cap_delivers_no_more_than_a_higher_one(self) -> None:
        """Monotonic, so the cap is a bound and not an unrelated knob."""
        counts = [len(_compile(cap).write_targets) for cap in (4, 6, 8, 10)]
        assert counts == sorted(counts), counts


class TestDeferringStaysTruthful:
    def test_what_is_dropped_is_reported_rather_than_lost(self) -> None:
        """A deferred recommendation the user is never told about is indistinguishable from one the
        product cannot make."""
        tight = _compile(4)
        generous = _compile(99)
        assert tight.deferred_checks, "artifacts were dropped and nothing was recorded as deferred"
        # Nothing vanishes: every check the generous compile addressed is either addressed or deferred.
        assert set(generous.addressed_checks) <= set(tight.addressed_checks) | set(tight.deferred_checks)

    def test_a_section_larger_than_the_cap_survives_and_says_so(self) -> None:
        """The k8s section is three files. Against a cap of 1 it cannot fit, and deferring it would
        leave the run with NO instruction -- so it is kept and the strategy names the overrun. A run
        that quietly claimed 1 and asked for 3 is how the original defect stayed invisible."""
        compiled = _compile(1)
        assert compiled.write_targets, "a cap of 1 left the run with nothing to ask for"
        assert len(compiled.write_targets) > 1
        assert "exceeds" in compiled.budget_strategy or "on its own" in compiled.budget_strategy, (
            compiled.budget_strategy
        )
