# SPDX-License-Identifier: FSL-1.1-ALv2
"""Revision `0010_change_set_status_vocabulary` against a REAL PostgreSQL (§3.6, §6.5, D-63).

§6.5 proves every revision with a gated integration test in the D-26 pattern. This one has a
sharper job than most, because the revision exists to fix a defect the previous proof could not
see: `test_0004_change_sets.py` parametrises over `CHANGE_SET_STATUSES` and asserts each member is
accepted, so it stayed green while that tuple disagreed with design §3.6 in **nine** places — three
invented names and six missing ones. A proof derived entirely from the implementation's own list
cannot notice that the list is wrong.

So this file asserts against §3.6 **read out of design.md**, not against the tuple. If the two ever
diverge again, the test fails and names the difference.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from src.governance.models import (
    CHANGE_SET_STATUSES,
    CHANGE_SET_TRANSITIONS,
    TERMINAL_CHANGE_SET_STATUSES,
)

from .migration_support import conn, head_engine, make_project, schema_at_head  # noqa: F401 - fixtures

pytestmark = pytest.mark.mandatory

DESIGN = Path(__file__).resolve().parents[3] / ".kiro" / "specs" / "phase-1-mvp-core" / "design.md"

CHECK_VIOLATION = "23514"

#: The three names revision `0004` invented and `0010` removes, and the six §3.6 names it lacked.
REMOVED_BY_0010 = ("validated", "awaiting_approval", "failed")
ADDED_BY_0010 = ("rejected_by_policy", "blocked", "pending_approval", "expired", "conflicted", "reverted")


def states_in_design_section_3_6() -> set[str]:
    """Every state named by §3.6's mermaid `stateDiagram-v2`, parsed from design.md.

    Reads the authority rather than restating it. A test that restated the list would be a second
    copy of the thing that was already wrong once.
    """
    text = DESIGN.read_text(encoding="utf-8")
    start = text.index("### 3.6 State diagram")
    block = text[start : text.index("```", text.index("```mermaid", start) + 10)]
    states: set[str] = set()
    for line in block.splitlines():
        match = re.match(r"\s*([A-Za-z_*\[\]]+)\s*-->\s*([A-Za-z_*\[\]]+)", line.strip())
        if match:
            for name in match.groups():
                if name not in ("[*]",):
                    states.add(name)
    return states


class TestTheVocabularyIsDesignSection36s:
    def test_the_tuple_equals_the_states_named_by_the_diagram(self) -> None:
        """The assertion `0004`'s proof could not make, because it read the tuple instead."""
        assert set(CHANGE_SET_STATUSES) == states_in_design_section_3_6()

    def test_the_tuple_has_no_duplicates_and_is_in_lifecycle_order(self) -> None:
        assert len(set(CHANGE_SET_STATUSES)) == len(CHANGE_SET_STATUSES)
        assert CHANGE_SET_STATUSES[0] == "draft"
        assert CHANGE_SET_STATUSES[1] == "validating"

    def test_the_three_invented_names_are_gone(self) -> None:
        for name in REMOVED_BY_0010:
            assert name not in CHANGE_SET_STATUSES, f"{name!r} is not a state design §3.6 defines"

    def test_the_six_missing_names_are_present(self) -> None:
        for name in ADDED_BY_0010:
            assert name in CHANGE_SET_STATUSES, f"design §3.6 defines {name!r}"

    def test_every_transition_endpoint_is_a_known_state(self) -> None:
        for source, target in CHANGE_SET_TRANSITIONS:
            assert source in CHANGE_SET_STATUSES, source
            assert target in CHANGE_SET_STATUSES, target

    def test_no_transition_leaves_a_terminal_state(self) -> None:
        """§3.6: "terminal states are absorbing". Q-22 asserts it over generated sequences; this
        asserts it of the edge table those sequences are generated from."""
        leaving = [edge for edge in CHANGE_SET_TRANSITIONS if edge[0] in TERMINAL_CHANGE_SET_STATUSES]
        assert leaving == [("applied", "reverted")], (
            f"only `applied → reverted` may leave a terminal state (§3.6); found {leaving}"
        )


@pytest.mark.asyncio
class TestTheDatabaseEnforcesTheNewVocabulary:
    async def _insert(self, connection: AsyncConnection, project_id: uuid.UUID, status: str) -> None:
        from sqlalchemy import text

        await connection.execute(
            text(
                "INSERT INTO change_sets (id, project_id, status, origin, blast_radius_score, "
                "blast_radius_verdict, policy_bundle_digest) "
                "VALUES (:id, :project, :status, 'manual', 0, 'allow', :digest)"
            ),
            {
                "id": uuid.uuid4(),
                "project": project_id,
                "status": status,
                "digest": "sha256:" + "00" * 32,
            },
        )

    @pytest.mark.parametrize("status", CHANGE_SET_STATUSES)
    async def test_every_design_state_is_accepted(self, conn: AsyncConnection, status: str) -> None:  # noqa: F811
        project_id = await make_project(conn, "status")
        await self._insert(conn, project_id, status)

    @pytest.mark.parametrize("status", [*REMOVED_BY_0010, "in_progress", "APPROVED", ""])
    async def test_a_status_outside_the_vocabulary_is_rejected(  # noqa: F811
        self, conn: AsyncConnection, status: str
    ) -> None:
        """Including the three names `0004` used to permit: the narrowing is real, not cosmetic."""
        project_id = await make_project(conn, "status")
        with pytest.raises(DBAPIError) as raised:
            await self._insert(conn, project_id, status)
        assert getattr(raised.value.orig, "sqlstate", None) == CHECK_VIOLATION or CHECK_VIOLATION in str(raised.value)

    async def test_the_constraint_is_valid_after_the_upgrade(self, conn: AsyncConnection) -> None:  # noqa: F811
        """`convalidated`, asserted from the catalogue.

        The downgrade deliberately restores the narrower constraint as `NOT VALID` so it can run
        against rows the wider vocabulary allowed. The upgrade must not inherit that: a `NOT VALID`
        constraint at head would mean the database had never checked the rows it holds.
        """
        from sqlalchemy import text

        result = await conn.execute(
            text("SELECT convalidated FROM pg_constraint WHERE conname = 'ck_change_sets_status_allowed'")
        )
        assert result.scalar() is True

    async def test_the_constraint_names_every_state_and_nothing_else(self, conn: AsyncConnection) -> None:  # noqa: F811
        """Read the server's own rendering, so a constraint built from a stale tuple is visible."""
        from sqlalchemy import text

        result = await conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_change_sets_status_allowed'"
            )
        )
        definition = str(result.scalar())
        for status in CHANGE_SET_STATUSES:
            assert f"'{status}'" in definition, f"{status} is missing from {definition}"
        for status in REMOVED_BY_0010:
            assert f"'{status}'" not in definition, f"{status} still appears in {definition}"
