# SPDX-License-Identifier: FSL-1.1-ALv2
"""Revision `0004_change_sets_and_approvals` against a REAL PostgreSQL.

design.md §6.2, §6.3, §6.5, §3.6; tasks.md leaf 5.3.

The allowed-value tuples are imported from `src.governance.models`, the same tuples
the migration renders its check constraints from. A test that retyped them could
pass while the constraint said something else, which is the only failure this class
of test exists to catch.

The cascade assertions come in pairs — the declared `confdeltype` and the observed
behaviour. Either alone is weaker than it looks: a declaration can be right while a
trigger or a second constraint overrides the effect, and an observation can be right
by accident when no dependent row exists.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from src.governance.models import (
    APPROVAL_STATUSES,
    CHANGE_ITEM_ACTIONS,
    CHANGE_SET_ORIGINS,
    CHANGE_SET_STATUSES,
)

from .migration_support import (
    fk_delete_action,
    make_project,
    make_user,
    scalar,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

INSERT_CHANGE_SET = text(
    "INSERT INTO change_sets "
    "(id, project_id, status, origin, blast_radius_score, blast_radius_verdict, "
    " policy_bundle_digest) "
    "VALUES (:id, :project_id, :status, :origin, 0, 'low', :digest)"
)

INSERT_CHANGE_ITEM = text(
    "INSERT INTO change_items (id, change_set_id, file_path, action, ordinal) "
    "VALUES (:id, :cs, :path, :action, :ordinal)"
)

DIGEST = "sha256:" + "0" * 64


async def _change_set(conn, project_id: uuid.UUID, status: str = "draft", origin: str = "generation") -> uuid.UUID:
    change_set_id = uuid.uuid4()
    await conn.execute(
        INSERT_CHANGE_SET,
        {
            "id": change_set_id,
            "project_id": project_id,
            "status": status,
            "origin": origin,
            "digest": DIGEST,
        },
    )
    return change_set_id


class TestTheStatusCheckConstraint:
    @pytest.mark.parametrize("status", CHANGE_SET_STATUSES)
    async def test_every_declared_state_is_accepted(self, conn, status: str) -> None:
        """Parametrised over the model's own tuple, so the constraint cannot become
        narrower than the state machine without failing here."""
        project_id = await make_project(conn, "cs")
        await _change_set(conn, project_id, status=status)

    async def test_an_unknown_state_is_rejected(self, conn) -> None:
        project_id = await make_project(conn, "cs-bad")
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _change_set(conn, project_id, status="not_a_state")

    @pytest.mark.parametrize("origin", CHANGE_SET_ORIGINS)
    async def test_every_declared_origin_is_accepted(self, conn, origin: str) -> None:
        project_id = await make_project(conn, "cs-origin")
        await _change_set(conn, project_id, origin=origin)

    async def test_an_unknown_origin_is_rejected(self, conn) -> None:
        project_id = await make_project(conn, "cs-origin-bad")
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _change_set(conn, project_id, origin="cosmic_ray")


class TestOptimisticConcurrencyColumn:
    async def test_version_defaults_to_one(self, conn) -> None:
        project_id = await make_project(conn, "version")
        change_set_id = await _change_set(conn, project_id)
        version = await scalar(conn, "SELECT version FROM change_sets WHERE id = :id", id=change_set_id)
        assert version == 1


class TestChangeItemOrdinals:
    async def test_a_duplicate_ordinal_in_one_change_set_is_rejected(self, conn) -> None:
        project_id = await make_project(conn, "ordinal")
        change_set_id = await _change_set(conn, project_id)
        await conn.execute(
            INSERT_CHANGE_ITEM,
            {
                "id": uuid.uuid4(),
                "cs": change_set_id,
                "path": "a.yml",
                "action": "create",
                "ordinal": 0,
            },
        )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    INSERT_CHANGE_ITEM,
                    {
                        "id": uuid.uuid4(),
                        "cs": change_set_id,
                        "path": "b.yml",
                        "action": "create",
                        "ordinal": 0,
                    },
                )

    async def test_the_same_ordinal_in_a_different_change_set_is_fine(self, conn) -> None:
        """Guards against the constraint being global rather than per change set —
        ordinals are an ordering within one change set, not a database-wide sequence."""
        project_id = await make_project(conn, "ordinal-two")
        first = await _change_set(conn, project_id)
        second = await _change_set(conn, project_id)
        for change_set_id in (first, second):
            await conn.execute(
                INSERT_CHANGE_ITEM,
                {
                    "id": uuid.uuid4(),
                    "cs": change_set_id,
                    "path": "same.yml",
                    "action": "create",
                    "ordinal": 0,
                },
            )

    @pytest.mark.parametrize("action", CHANGE_ITEM_ACTIONS)
    async def test_every_declared_action_is_accepted(self, conn, action: str) -> None:
        project_id = await make_project(conn, "action")
        change_set_id = await _change_set(conn, project_id)
        await conn.execute(
            INSERT_CHANGE_ITEM,
            {
                "id": uuid.uuid4(),
                "cs": change_set_id,
                "path": "x.yml",
                "action": action,
                "ordinal": 1,
            },
        )

    async def test_an_unknown_action_is_rejected(self, conn) -> None:
        project_id = await make_project(conn, "action-bad")
        change_set_id = await _change_set(conn, project_id)
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    INSERT_CHANGE_ITEM,
                    {
                        "id": uuid.uuid4(),
                        "cs": change_set_id,
                        "path": "x.yml",
                        "action": "chmod",
                        "ordinal": 2,
                    },
                )

    async def test_the_stale_apply_hashes_exist(self, conn) -> None:
        """`old_hash` is what lets the agent refuse a diff whose world has moved."""
        project_id = await make_project(conn, "hashes")
        change_set_id = await _change_set(conn, project_id)
        item_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO change_items "
                "(id, change_set_id, file_path, action, old_hash, new_hash, ordinal) "
                "VALUES (:id, :cs, 'd.yml', 'update', :old, :new, 3)"
            ),
            {"id": item_id, "cs": change_set_id, "old": "a" * 64, "new": "b" * 64},
        )
        stored = await scalar(conn, "SELECT old_hash FROM change_items WHERE id = :id", id=item_id)
        assert stored == "a" * 64


class TestCascadeBehaviour:
    async def test_deleting_a_change_set_removes_its_dependents(self, conn) -> None:
        project_id = await make_project(conn, "cascade")
        user_id = await make_user(conn)
        change_set_id = await _change_set(conn, project_id)

        item_id = uuid.uuid4()
        await conn.execute(
            INSERT_CHANGE_ITEM,
            {
                "id": item_id,
                "cs": change_set_id,
                "path": "c.yml",
                "action": "create",
                "ordinal": 0,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO validations "
                "(id, change_item_id, validator, passed, blocking, output, iteration) "
                "VALUES (:id, :item, 'yamllint', true, true, '', 0)"
            ),
            {"id": uuid.uuid4(), "item": item_id},
        )
        await conn.execute(
            text("INSERT INTO approvals (id, change_set_id, approver_id, status) VALUES (:id, :cs, :user, 'approved')"),
            {"id": uuid.uuid4(), "cs": change_set_id, "user": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO rollback_handles "
                "(id, change_set_id, backup_manifest, agent_device_id, consumed, expires_at) "
                "VALUES (:id, :cs, '{}'::jsonb, 'dev-1', false, now() + interval '1 day')"
            ),
            {"id": uuid.uuid4(), "cs": change_set_id},
        )

        await conn.execute(text("DELETE FROM change_sets WHERE id = :id"), {"id": change_set_id})

        for table, column in (
            ("change_items", "change_set_id"),
            ("approvals", "change_set_id"),
            ("rollback_handles", "change_set_id"),
        ):
            remaining = await scalar(conn, f"SELECT count(*) FROM {table} WHERE {column} = :cs", cs=change_set_id)
            assert remaining == 0, f"{table} survived its change set"
        orphan_validations = await scalar(conn, "SELECT count(*) FROM validations WHERE change_item_id = :i", i=item_id)
        assert orphan_validations == 0, "validations survived their change item"

    async def test_created_by_is_set_null_not_cascade(self, conn) -> None:
        """A change set must outlive the person who created it; deleting a user must
        not delete the proposal."""
        assert await fk_delete_action(conn, "fk_change_sets_created_by_users") == "n"

    async def test_the_approver_reference_is_restrict(self, conn) -> None:
        assert await fk_delete_action(conn, "fk_approvals_approver_id_users") == "r"

    async def test_deleting_an_approver_is_refused(self, conn) -> None:
        """RESTRICT proved behaviourally, not just declared. Deleting a user who has
        approved something would erase the record of the decision — the same reasoning
        as the audit log's deliberately missing foreign keys (§6.3)."""
        project_id = await make_project(conn, "restrict")
        user_id = await make_user(conn)
        change_set_id = await _change_set(conn, project_id)
        await conn.execute(
            text("INSERT INTO approvals (id, change_set_id, approver_id, status) VALUES (:id, :cs, :user, 'approved')"),
            {"id": uuid.uuid4(), "cs": change_set_id, "user": user_id},
        )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


class TestApprovalStatus:
    @pytest.mark.parametrize("status", APPROVAL_STATUSES)
    async def test_the_two_decisions_are_accepted(self, conn, status: str) -> None:
        project_id = await make_project(conn, "approval")
        user_id = await make_user(conn)
        change_set_id = await _change_set(conn, project_id)
        await conn.execute(
            text("INSERT INTO approvals (id, change_set_id, approver_id, status) VALUES (:id, :cs, :user, :status)"),
            {"id": uuid.uuid4(), "cs": change_set_id, "user": user_id, "status": status},
        )

    async def test_a_third_decision_is_rejected(self, conn) -> None:
        """There is no "maybe" in an approval record."""
        project_id = await make_project(conn, "approval-bad")
        user_id = await make_user(conn)
        change_set_id = await _change_set(conn, project_id)
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "INSERT INTO approvals (id, change_set_id, approver_id, status) "
                        "VALUES (:id, :cs, :user, 'pending')"
                    ),
                    {"id": uuid.uuid4(), "cs": change_set_id, "user": user_id},
                )


class TestRollbackHandleUniqueness:
    async def test_at_most_one_handle_per_change_set(self, conn) -> None:
        project_id = await make_project(conn, "rollback")
        change_set_id = await _change_set(conn, project_id)
        insert = text(
            "INSERT INTO rollback_handles "
            "(id, change_set_id, backup_manifest, agent_device_id, consumed, expires_at) "
            "VALUES (:id, :cs, '{}'::jsonb, 'dev-1', false, now() + interval '1 day')"
        )
        await conn.execute(insert, {"id": uuid.uuid4(), "cs": change_set_id})
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(insert, {"id": uuid.uuid4(), "cs": change_set_id})
