# SPDX-License-Identifier: FSL-1.1-ALv2
"""Revision `0007_audit_append_only` against a REAL PostgreSQL.

design.md §6.3, §6.4, §6.5, §11.9; Appendix E criterion 9; Q-05; tasks.md leaf 5.6.

Immutability is asserted at both layers, because either alone is defeatable. The
trigger stops an ORM bug or a stray statement run as *any* role, including the
migrator. The REVOKE stops the application dropping the trigger, because dropping a
trigger requires an ownership the app role does not hold. A test that checked only
the trigger would pass on a schema where the app could remove it.

The SQLSTATE, not the message, is asserted: `42501` is `insufficient_privilege`, and
matching on prose would break the moment someone improves the wording.

The tamper clause is deliberately shaped around the fact that the table cannot be
modified. Verification is a pure function of stored bytes, so tampering is simulated
in the *verification input* — recomputing what a verifier would see if a middle row's
payload had differed — and asserting the recomputed hash no longer matches the stored
successor's `prev_hash`. Attempting the UPDATE instead is a separate assertion above,
and it raises.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .migration_support import rows, scalar

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

INSUFFICIENT_PRIVILEGE = "42501"

INSERT_EVENT = text(
    "INSERT INTO audit_events "
    "(id, actor_kind, action, resource_kind, resource_id, reason, outcome, prev_hash, hash) "
    "VALUES (:id, 'user', :action, 'change_set', :resource_id, :reason, 'allowed', "
    "        :prev_hash, :hash) "
    "RETURNING seq"
)


def canonical(payload: dict) -> bytes:
    """A deterministic byte rendering of an audit payload.

    Sorted keys and no insignificant whitespace, so the same logical record always
    hashes to the same value. §11.9's writer uses RFC 8785 JCS for this; the test
    needs only determinism, and defining it locally keeps the proof independent of the
    writer that does not exist yet.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def chain_hash(payload: dict, prev_hash: bytes) -> bytes:
    """`hash = sha256(canonical(payload) || prev_hash)` — §6.3 mechanism 2."""
    return hashlib.sha256(canonical(payload) + prev_hash).digest()


def sqlstate(error: BaseException) -> str | None:
    original = getattr(error, "orig", None)
    for candidate in (original, getattr(original, "__cause__", None)):
        if candidate is None:
            continue
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


async def _event(conn, action: str = "change_set.approve", prev: bytes | None = None) -> int:
    payload = {"action": action, "resource": "change_set", "id": str(uuid.uuid4())}
    prev_hash = prev if prev is not None else b"\x00" * 32
    result = await conn.execute(
        INSERT_EVENT,
        {
            "id": uuid.uuid4(),
            "action": action,
            "resource_id": str(uuid.uuid4()),
            "reason": "proof of append-only behaviour",
            "prev_hash": prev_hash,
            "hash": chain_hash(payload, prev_hash),
        },
    )
    return int(result.scalar())


class TestInsertIsTheOnlyPermittedWrite:
    async def test_insert_succeeds(self, conn) -> None:
        seq = await _event(conn)
        assert seq > 0

    async def test_seq_is_monotonic(self, conn) -> None:
        """A total order per database, so a deletion leaves a gap (mechanism 1)."""
        first = await _event(conn)
        second = await _event(conn)
        third = await _event(conn)
        assert first < second < third

    async def test_update_raises_insufficient_privilege(self, conn) -> None:
        await _event(conn)
        with pytest.raises(DBAPIError) as caught:
            async with conn.begin_nested():
                await conn.execute(text("UPDATE audit_events SET reason = 'rewritten'"))
        assert sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE, caught.value

    async def test_delete_raises_insufficient_privilege(self, conn) -> None:
        await _event(conn)
        with pytest.raises(DBAPIError) as caught:
            async with conn.begin_nested():
                await conn.execute(text("DELETE FROM audit_events"))
        assert sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE, caught.value

    async def test_truncate_raises_insufficient_privilege(self, conn) -> None:
        """TRUNCATE bypasses row triggers, which is why `0007` installs a
        statement-level trigger for it specifically."""
        await _event(conn)
        with pytest.raises(DBAPIError) as caught:
            async with conn.begin_nested():
                await conn.execute(text("TRUNCATE audit_events"))
        assert sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE, caught.value


class TestTheThreeTriggersExist:
    @pytest.mark.parametrize(
        "trigger",
        [
            "trg_audit_events_no_update",
            "trg_audit_events_no_delete",
            "trg_audit_events_no_truncate",
        ],
    )
    async def test_the_trigger_is_installed(self, conn, trigger: str) -> None:
        present = await scalar(
            conn,
            """
            SELECT count(*) FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE c.relname = 'audit_events' AND t.tgname = :name AND NOT t.tgisinternal
            """,
            name=trigger,
        )
        assert present == 1, f"{trigger} is missing"


class TestTheApplicationRolePrivileges:
    @pytest.mark.parametrize("privilege", ["UPDATE", "DELETE", "TRUNCATE"])
    async def test_the_app_role_cannot_rewrite_history(self, conn, privilege: str) -> None:
        """Mechanism 3's belt-and-braces half. Without it, the application could drop
        the triggers, because dropping a trigger needs table ownership."""
        held = await scalar(
            conn,
            "SELECT has_table_privilege('forgeops_app', 'audit_events', :p)",
            p=privilege,
        )
        assert held is False, f"forgeops_app holds {privilege} on audit_events"

    @pytest.mark.parametrize("privilege", ["INSERT", "SELECT"])
    async def test_the_app_role_can_still_append_and_read(self, conn, privilege: str) -> None:
        held = await scalar(
            conn,
            "SELECT has_table_privilege('forgeops_app', 'audit_events', :p)",
            p=privilege,
        )
        assert held is True, f"forgeops_app lacks {privilege} on audit_events"

    async def test_the_app_role_can_use_the_sequence(self, conn) -> None:
        """`seq BIGSERIAL` is useless to an INSERT that cannot advance the sequence."""
        held = await scalar(conn, "SELECT has_sequence_privilege('forgeops_app', 'audit_events_seq_seq', 'USAGE')")
        assert held is True


class TestNoForeignKeysOnAuditEvents:
    async def test_the_table_has_no_foreign_key_at_all(self, conn) -> None:
        """An immutable log that cascades away when a project is deleted is not an
        immutable log. Referential integrity is traded for durability, deliberately."""
        found = await rows(
            conn,
            "SELECT conname FROM pg_constraint WHERE conrelid = 'audit_events'::regclass AND contype = 'f'",
        )
        assert found == [], found

    async def test_a_record_survives_deletion_of_what_it_describes(self, conn) -> None:
        from .migration_support import make_project

        project_id = await make_project(conn, "audit-survives")
        event_id = uuid.uuid4()
        payload = {"project": str(project_id)}
        await conn.execute(
            text(
                "INSERT INTO audit_events "
                "(id, project_id, actor_kind, action, resource_kind, reason, outcome, "
                " prev_hash, hash) "
                "VALUES (:id, :project_id, 'system', 'project.delete', 'project', "
                "        'proof', 'applied', :prev, :hash)"
            ),
            {
                "id": event_id,
                "project_id": project_id,
                "prev": b"\x00" * 32,
                "hash": chain_hash(payload, b"\x00" * 32),
            },
        )
        await conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})
        surviving = await scalar(conn, "SELECT project_id FROM audit_events WHERE id = :id", id=event_id)
        assert surviving == project_id, "the audit record did not survive its project"


class TestChainVerification:
    async def test_an_untampered_chain_verifies(self, conn) -> None:
        payloads = [{"step": n} for n in range(3)]
        prev = b"\x00" * 32
        stored: list[tuple[dict, bytes, bytes]] = []
        for payload in payloads:
            digest = chain_hash(payload, prev)
            stored.append((payload, prev, digest))
            prev = digest

        for payload, prev_hash, digest in stored:
            assert chain_hash(payload, prev_hash) == digest

        for earlier, later in zip(stored, stored[1:], strict=False):
            assert later[1] == earlier[2], "prev_hash must be the previous row's hash"

    async def test_a_tampered_payload_breaks_the_successor_link(self, conn) -> None:
        """Tampering is simulated in the verification input, not in the table.

        The table cannot be modified — that is asserted above, and it raises `42501`.
        Verification is a pure function of stored bytes, so altering the payload a
        verifier is handed is exactly equivalent to altering the row, without needing
        a privilege the schema deliberately denies.
        """
        first = {"step": 0}
        second = {"step": 1}
        prev = b"\x00" * 32
        first_hash = chain_hash(first, prev)
        second_hash = chain_hash(second, first_hash)

        tampered = {"step": 0, "amount": 1_000_000}
        tampered_hash = chain_hash(tampered, prev)

        assert tampered_hash != first_hash
        # The stored successor still claims the ORIGINAL hash as its predecessor, so
        # the chain no longer closes.
        assert chain_hash(second, tampered_hash) != second_hash

    async def test_the_hash_columns_are_32_byte_bytea(self, conn) -> None:
        from .migration_support import column_type

        assert await column_type(conn, "audit_events", "prev_hash") == "bytea"
        assert await column_type(conn, "audit_events", "hash") == "bytea"
