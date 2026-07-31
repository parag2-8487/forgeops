# SPDX-License-Identifier: FSL-1.1-ALv2
"""`AuditWriter` against a REAL PostgreSQL (design.md §6.4, §11.9, Appendix A.8; leaf 7.6).

Why these assertions need a database and cannot be unit tests
-------------------------------------------------------------
Every property worth having here is a property of the *transaction*, not of the arithmetic:

* the record and the caller's state change commit or roll back **together** (Q-04);
* the chain is well-defined because appends serialise on a transaction-scoped advisory lock;
* `created_at` comes from the **database's** clock, so two API replicas cannot disagree;
* UPDATE and DELETE raise for the application role, revoked *and* trigger-guarded by `0007`.

A hash chain verified in memory proves the arithmetic and nothing about the append.

How tampering is simulated, and why that is the honest way
----------------------------------------------------------
`0007`'s trigger refuses UPDATE for **every** role, including the migrator, so the tamper test
disables the trigger as the table's owner, edits one row, and re-enables it. That is deliberately
the threat model tamper-evidence exists for: an actor who has already reached the database. A test
that only recomputed the verifier's *input* would prove the comparison and leave the interesting
question — does a real edit get caught — unasked.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.audit.writer import GENESIS_PREV_HASH, AuditDraft, AuditWriter

from .migration_support import head_engine, schema_at_head  # noqa: F401 - fixtures

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

INSUFFICIENT_PRIVILEGE = "42501"


@pytest_asyncio.fixture()
async def sessions(head_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:  # noqa: F811
    return async_sessionmaker(head_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture()
def writer() -> AuditWriter:
    return AuditWriter()


def draft(tenant: uuid.UUID | None, *, action: str = "change_set_auto_approved", **extra: object) -> AuditDraft:
    return AuditDraft(
        action=action,
        resource_kind="change_set",
        reason="a stated reason, because NFR-14 requires one",
        outcome="allowed",
        tenant_id=tenant,
        **extra,  # type: ignore[arg-type]
    )


class TestTheChainIsWellFormed:
    async def test_the_first_record_for_a_tenant_chains_from_genesis(
        self, sessions: async_sessionmaker[AsyncSession], writer: AuditWriter
    ) -> None:
        tenant = uuid.uuid4()
        async with sessions() as session:
            event = await writer.append(session, draft(tenant))
            await session.commit()
        assert event.prev_hash == GENESIS_PREV_HASH
        assert event.seq is not None and event.seq > 0

    async def test_each_record_chains_to_its_predecessor(
        self, sessions: async_sessionmaker[AsyncSession], writer: AuditWriter
    ) -> None:
        tenant = uuid.uuid4()
        hashes: list[bytes] = []
        async with sessions() as session:
            for index in range(5):
                event = await writer.append(session, draft(tenant, resource_id=f"cs-{index}"))
                hashes.append(bytes(event.hash))
                assert bytes(event.prev_hash) == (GENESIS_PREV_HASH if index == 0 else hashes[index - 1])
            await session.commit()
        assert len(set(hashes)) == 5, "five distinct records must produce five distinct hashes"

    async def test_verify_chain_accepts_a_chain_it_wrote(
        self, sessions: async_sessionmaker[AsyncSession], writer: AuditWriter
    ) -> None:
        tenant = uuid.uuid4()
        async with sessions() as session:
            for index in range(4):
                await writer.append(session, draft(tenant, resource_id=f"cs-{index}"))
            await session.commit()
        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        assert result.ok, result.divergence
        assert result.rows_checked == 4

    async def test_tenants_have_independent_chains(
        self, sessions: async_sessionmaker[AsyncSession], writer: AuditWriter
    ) -> None:
        """Interleaved appends for two tenants must each chain within their own tenant.

        The bug this catches is a chain read with `ORDER BY seq DESC LIMIT 1` and no tenant
        predicate: it would still verify, because the writer and the verifier would make the same
        mistake, and the two tenants' histories would be inseparably braided.
        """
        a, b = uuid.uuid4(), uuid.uuid4()
        async with sessions() as session:
            for index in range(3):
                await writer.append(session, draft(a, resource_id=f"a-{index}"))
                await writer.append(session, draft(b, resource_id=f"b-{index}"))
            await session.commit()
        async with sessions() as session:
            for tenant in (a, b):
                result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
                assert result.ok, (tenant, result.divergence)
                assert result.rows_checked == 3

    async def test_the_untenanted_chain_is_its_own_chain(
        self, sessions: async_sessionmaker[AsyncSession], writer: AuditWriter
    ) -> None:
        """`tenant_id` is nullable in Phase 1 (D-35), and `tenant_id = NULL` matches no row — so a
        plain equality would restart this chain at genesis on every append."""
        async with sessions() as session:
            first = await writer.append(session, draft(None, resource_id="sys-1"))
            second = await writer.append(session, draft(None, resource_id="sys-2"))
            await session.commit()
        assert bytes(second.prev_hash) == bytes(first.hash), (
            "the second untenanted record chained from genesis instead of from its predecessor"
        )

    async def test_verification_from_an_arbitrary_start_point_is_sound(
        self, sessions: async_sessionmaker[AsyncSession], writer: AuditWriter
    ) -> None:
        """Appendix A.8: `prev ← (from_seq = 0) ? ZERO32 : HashAt(from_seq − 1)`. This is what makes
        the chain checkable on a large table without reading all of it."""
        tenant = uuid.uuid4()
        seqs: list[int] = []
        async with sessions() as session:
            for index in range(5):
                event = await writer.append(session, draft(tenant, resource_id=f"cs-{index}"))
                assert event.seq is not None
                seqs.append(event.seq)
            await session.commit()
        async with sessions() as session:
            partial = await writer.verify_chain(session, tenant_id=tenant, since_seq=seqs[3])
        assert partial.ok, partial.divergence
        assert partial.rows_checked == 2


class TestTheRecordAndTheTransitCommitTogether:
    async def test_a_rolled_back_transaction_leaves_no_record(
        self, sessions: async_sessionmaker[AsyncSession], writer: AuditWriter
    ) -> None:
        """Q-04's other half. The writer joins the caller's transaction and never commits, so a
        caller that rolls back leaves neither the record nor the state change."""
        tenant = uuid.uuid4()
        async with sessions() as session:
            await writer.append(session, draft(tenant, resource_id="doomed"))
            await session.rollback()
        async with sessions() as session:
            count = await session.execute(text("SELECT count(*) FROM audit_events WHERE tenant_id = :t"), {"t": tenant})
            assert count.scalar_one() == 0

    async def test_an_invalid_draft_writes_nothing(
        self, sessions: async_sessionmaker[AsyncSession], writer: AuditWriter
    ) -> None:
        """A failed audit write must ABORT the mutation (§11.9), so validation raises before the
        insert rather than writing a record that says nothing."""
        tenant = uuid.uuid4()
        async with sessions() as session:
            with pytest.raises(Exception, match="reason is required"):
                await writer.append(
                    session,
                    AuditDraft(action="a", resource_kind="r", reason="  ", outcome="allowed", tenant_id=tenant),
                )
            await session.rollback()
        async with sessions() as session:
            count = await session.execute(text("SELECT count(*) FROM audit_events WHERE tenant_id = :t"), {"t": tenant})
            assert count.scalar_one() == 0

    async def test_created_at_comes_from_the_database_clock(
        self, sessions: async_sessionmaker[AsyncSession], writer: AuditWriter
    ) -> None:
        """Not the app's clock: two API replicas with drifting clocks would otherwise disagree about
        the order of their own records. `clock_timestamp()` rather than `now()`, so two records in
        one transit do not share a timestamp."""
        tenant = uuid.uuid4()
        async with sessions() as session:
            bounds = await session.execute(text("SELECT clock_timestamp()"))
            before = bounds.scalar_one()
            first = await writer.append(session, draft(tenant, resource_id="one"))
            second = await writer.append(session, draft(tenant, resource_id="two"))
            bounds = await session.execute(text("SELECT clock_timestamp()"))
            after = bounds.scalar_one()
            await session.commit()
        assert before <= first.created_at <= after
        assert first.created_at != second.created_at, (
            "two records in one transaction shared a created_at, so now() was used where clock_timestamp() is required"
        )


class TestTamperEvidence:
    async def test_editing_one_rows_semantic_field_is_reported_at_that_seq(
        self,
        sessions: async_sessionmaker[AsyncSession],
        writer: AuditWriter,
        head_engine: AsyncEngine,  # noqa: F811
    ) -> None:
        tenant = uuid.uuid4()
        seqs: list[int] = []
        async with sessions() as session:
            for index in range(4):
                event = await writer.append(session, draft(tenant, resource_id=f"cs-{index}"))
                assert event.seq is not None
                seqs.append(event.seq)
            await session.commit()
        target = seqs[1]

        # The trigger refuses UPDATE for every role, so it is disabled as the table's OWNER for
        # the duration of the edit. That is the threat model: an actor already inside the database.
        async with head_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_update"))
            try:
                await conn.execute(
                    text("UPDATE audit_events SET reason = :r WHERE seq = :s"),
                    {"r": "a reason nobody gave", "s": target},
                )
            finally:
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_update"))

        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        assert not result.ok
        assert result.divergence is not None
        assert result.divergence.seq == target, f"reported {result.divergence.seq}, tampered {target}"
        assert result.divergence.kind == "hash"

    async def test_rewriting_prev_hash_alone_is_also_caught(
        self,
        sessions: async_sessionmaker[AsyncSession],
        writer: AuditWriter,
        head_engine: AsyncEngine,  # noqa: F811
    ) -> None:
        """`prev_hash` is not inside the hashed payload, so the verifier compares it against the
        predecessor's stored `hash` separately. Without that comparison, an attacker who rewrote
        `prev_hash` and recomputed `hash` would produce a chain that is internally consistent and
        no longer describes the history it came from."""
        tenant = uuid.uuid4()
        seqs: list[int] = []
        async with sessions() as session:
            for index in range(3):
                event = await writer.append(session, draft(tenant, resource_id=f"cs-{index}"))
                assert event.seq is not None
                seqs.append(event.seq)
            await session.commit()
        target = seqs[2]

        async with head_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_update"))
            try:
                await conn.execute(
                    text("UPDATE audit_events SET prev_hash = :p WHERE seq = :s"),
                    {"p": bytes(32), "s": target},
                )
            finally:
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_update"))

        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        assert not result.ok
        assert result.divergence is not None
        assert result.divergence.seq == target
        assert result.divergence.kind == "prev_hash"

    async def test_the_trigger_refuses_an_update_even_to_the_owner(
        self,
        sessions: async_sessionmaker[AsyncSession],
        writer: AuditWriter,
        head_engine: AsyncEngine,  # noqa: F811
    ) -> None:
        """The trigger, not just the REVOKE. `0007`'s docstring is explicit that either alone is
        defeatable, and the connection here holds the ownership the REVOKE does not restrain."""
        tenant = uuid.uuid4()
        async with sessions() as session:
            event = await writer.append(session, draft(tenant, resource_id="immutable"))
            await session.commit()
        async with head_engine.begin() as conn:
            with pytest.raises(DBAPIError) as caught:
                await conn.execute(text("UPDATE audit_events SET reason = 'edited' WHERE seq = :s"), {"s": event.seq})
        assert getattr(caught.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE

    async def test_the_trigger_refuses_a_delete(
        self,
        sessions: async_sessionmaker[AsyncSession],
        writer: AuditWriter,
        head_engine: AsyncEngine,  # noqa: F811
    ) -> None:
        tenant = uuid.uuid4()
        async with sessions() as session:
            event = await writer.append(session, draft(tenant, resource_id="undeletable"))
            await session.commit()
        async with head_engine.begin() as conn:
            with pytest.raises(DBAPIError) as caught:
                await conn.execute(text("DELETE FROM audit_events WHERE seq = :s"), {"s": event.seq})
        assert getattr(caught.value.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE


class TestAppendsSerialise:
    async def test_concurrent_appenders_for_one_tenant_do_not_fork_the_chain(
        self, sessions: async_sessionmaker[AsyncSession], writer: AuditWriter
    ) -> None:
        """The reason the advisory lock exists. Without it, two appenders read the same tip and
        produce two rows claiming the same predecessor — a fork that `verify_chain` then reports,
        because exactly one of them can be right.

        Eight concurrent transactions, each on its own connection, so the lock is genuinely
        contended rather than reentrant.
        """
        tenant = uuid.uuid4()

        async def one(index: int) -> None:
            async with sessions() as session:
                await writer.append(session, draft(tenant, resource_id=f"concurrent-{index}"))
                await session.commit()

        await asyncio.gather(*(one(index) for index in range(8)))

        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
            count = await session.execute(text("SELECT count(*) FROM audit_events WHERE tenant_id = :t"), {"t": tenant})
        assert count.scalar_one() == 8
        assert result.ok, f"the chain forked under concurrency: {result.divergence}"
