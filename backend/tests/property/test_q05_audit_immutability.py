# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-05 — audit immutability and tamper evidence (§6.4, §11.9, A.8, Appendix B Q-05).

Property, universally quantified over audit sequences and tamper attempts:

    ∀ audit sequences and ∀ tamper attempts: UPDATE and DELETE raise; recomputing the chain from
    any start point reproduces every stored hash; altering one row's semantic fields makes
    `verify_chain` report that row's `seq` as the first divergence.

Why this needs a real database, and why the tamper is real
----------------------------------------------------------
Immutability here is not a property of the writer. It is a property of migration `0007`: the
application role holds no UPDATE, DELETE or TRUNCATE privilege, and three triggers refuse those
statements for **every** role including the owner. A test that only recomputed hashes in memory
would prove the arithmetic and leave the interesting question — *does a real edit get caught* —
unasked.

So the tamper is performed the way an attacker who has already reached the database would: the
trigger is disabled as the table's OWNER, one row is edited, and the trigger is re-enabled. That
is deliberately the threat model tamper evidence exists for.

The four clauses
----------------
* **Privilege.** UPDATE, DELETE and TRUNCATE are refused, quantified over the statements and over
  the rows they target. Two independent mechanisms, both asserted: the missing GRANT and the
  trigger.
* **Reproducibility.** Recomputing from **any** start point reproduces every stored hash —
  quantified over the start point, because A.8's `from_seq` is what makes verification affordable
  on a large table and it is only sound if the predecessor's hash is read rather than assumed.
* **Localisation.** Tampering with one row makes `verify_chain` report **that** row's `seq`, for
  every semantic field and every position in the chain. "The chain broke" is not the claim; "this
  row broke it" is.
* **Coverage.** Every semantic field is covered by the hash. Quantified over the field set, so a
  column added later without being added to `SEMANTIC_FIELDS` fails here.

Negative control (`mutations.toml` Q-05): drop `prev_hash` from the hashed payload. The property
must then fail, because a chain whose rows do not depend on their predecessor can be reordered
and truncated without any hash changing.
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.audit.models import AuditEvent
from src.audit.writer import GENESIS_PREV_HASH, SEMANTIC_FIELDS, AuditDraft, AuditWriter

#: `asyncio` is applied per CLASS rather than to the whole module, because two clauses here are
#: pure — the hashed-field-set derivation and the tamper-matrix guard — and marking a sync test
#: `asyncio` is a pytest warning that trains a reader to ignore warnings.
pytestmark = pytest.mark.mandatory

INSUFFICIENT_PRIVILEGE = "42501"

#: Modest, because every example writes and verifies real rows. The budget buys breadth of
#: TAMPER POSITION and start point, which is where the interesting failures are.
_SETTINGS = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

#: The semantic text fields a tamper can rewrite in place without violating a constraint.
#:
#: `action`, `resource_kind` and `outcome` are constrained by `0007`'s check constraints, so a
#: tamper has to substitute another legal value rather than arbitrary text — which is a *better*
#: tamper anyway: it is what an attacker rewriting history would actually do.
#:
#: **Every value here must differ from the honest value `draft()` writes.** The first version
#: listed `"allowed"` among the `outcome` variants, which is exactly what the honest row already
#: carries — so that example "tampered" with nothing, the chain correctly still verified, and the
#: property failed. A tamper matrix that can contain a no-op is a matrix that reports the verifier
#: broken when it is right. `test_no_tamper_value_matches_the_honest_row` keeps that closed.
TAMPERABLE: dict[str, tuple[str, ...]] = {
    "reason": ("a reason nobody gave", "approved by nobody", "-"),
    "resource_id": ("cs-forged", "00000000-0000-0000-0000-000000000000"),
    "action": ("change_set_approved", "mutation_denied"),
    "resource_kind": ("project", "secret"),
    "outcome": ("denied", "blocked", "failed"),
    "trace_id": ("forged-trace", "0" * 32),
}


def draft(tenant: uuid.UUID, *, index: int) -> AuditDraft:
    return AuditDraft(
        action="change_set_auto_approved",
        resource_kind="change_set",
        resource_id=f"cs-{index}",
        reason=f"record {index}, written honestly",
        outcome="allowed",
        tenant_id=tenant,
        trace_id=f"trace-{index}",
    )


async def write_chain(sessions: async_sessionmaker[AsyncSession], tenant: uuid.UUID, length: int) -> list[int]:
    """Append `length` records for `tenant` and return their sequence numbers."""
    writer = AuditWriter()
    seqs: list[int] = []
    async with sessions() as session:
        for index in range(length):
            event = await writer.append(session, draft(tenant, index=index))
            assert event.seq is not None
            seqs.append(int(event.seq))
        await session.commit()
    return seqs


async def tamper(engine: AsyncEngine, *, seq: int, column: str, value: str) -> None:
    """Edit one row the way an actor already inside the database would.

    `0007`'s trigger refuses UPDATE for every role including the owner, so it is disabled for the
    duration of the edit and re-enabled in a `finally`. Leaving it disabled would silently weaken
    every later example in the same session, which is the kind of test-order coupling that makes
    a suite pass for the wrong reason.
    """
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_update"))
        try:
            await conn.execute(
                text(f"UPDATE audit_events SET {column} = :value WHERE seq = :seq"),  # noqa: S608 - closed column set
                {"value": value, "seq": seq},
            )
        finally:
            await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_update"))


def _is_refusal(error: BaseException) -> bool:
    """Whether a DBAPI error is `0007`'s refusal rather than some other database complaint.

    Read from asyncpg's `sqlstate` rather than by searching the message text. The message renders
    the exception CLASS (`InsufficientPrivilegeError`) and not the code, so a substring search for
    `42501` finds nothing — and a test that accepted any `DBAPIError` would pass for a syntax
    error, which proves nothing about immutability.
    """
    original = getattr(error, "orig", error)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate == INSUFFICIENT_PRIVILEGE:
        return True
    return "insufficientprivilege" in type(original).__name__.lower()


@pytest.mark.asyncio
class TestUpdateAndDeleteAreRefused:
    @_SETTINGS
    @given(
        column=st.sampled_from(("reason", "action", "outcome", "hash", "prev_hash")),
        length=st.integers(min_value=1, max_value=3),
    )
    async def test_update_is_refused_for_every_column(
        self,
        sessions: async_sessionmaker[AsyncSession],
        column: str,
        length: int,
    ) -> None:
        """Quantified over the column, because a trigger scoped to one column would look identical.

        `hash` and `prev_hash` are in the set on purpose: rewriting those is the *interesting*
        tamper, and a guard that protected only the semantic fields would leave it open.
        """
        tenant = uuid.uuid4()
        seqs = await write_chain(sessions, tenant, length)
        async with sessions() as session:
            with pytest.raises(DBAPIError) as raised:
                await session.execute(
                    text(f"UPDATE audit_events SET {column} = NULL WHERE seq = :seq"),  # noqa: S608 - closed set
                    {"seq": seqs[0]},
                )
            await session.rollback()
        assert _is_refusal(raised.value), f"the UPDATE failed, but not because it was refused: {raised.value}"

    @_SETTINGS
    @given(length=st.integers(min_value=1, max_value=4), which=st.integers(min_value=0, max_value=3))
    async def test_delete_is_refused_for_every_row(
        self,
        sessions: async_sessionmaker[AsyncSession],
        length: int,
        which: int,
    ) -> None:
        tenant = uuid.uuid4()
        seqs = await write_chain(sessions, tenant, length)
        target = seqs[which % len(seqs)]
        async with sessions() as session:
            with pytest.raises(DBAPIError) as raised:
                await session.execute(text("DELETE FROM audit_events WHERE seq = :seq"), {"seq": target})
            await session.rollback()
        assert _is_refusal(raised.value), f"the DELETE failed for the wrong reason: {raised.value}"
        async with sessions() as reader:
            result = await reader.execute(text("SELECT count(*) FROM audit_events WHERE tenant_id = :t"), {"t": tenant})
            assert result.scalar() == length, "a refused DELETE must leave every row in place"

    async def test_truncate_is_refused(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """The statement a DELETE guard alone would miss."""
        async with sessions() as session:
            with pytest.raises(DBAPIError) as raised:
                await session.execute(text("TRUNCATE audit_events"))
            await session.rollback()
        assert _is_refusal(raised.value), f"the TRUNCATE failed for the wrong reason: {raised.value}"

    async def test_the_application_role_holds_none_of_the_three_privileges(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The REVOKE half. §11.9 is explicit that either mechanism alone is insufficient: a
        trigger can be dropped by whoever owns the table, and a GRANT can be re-granted — so both
        are asserted rather than one being taken as evidence of the other."""
        async with sessions() as session:
            for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                result = await session.execute(
                    text("SELECT has_table_privilege('forgeops_app', 'audit_events', :p)"), {"p": privilege}
                )
                assert result.scalar() is False, f"forgeops_app holds {privilege} on audit_events"
            for privilege in ("INSERT", "SELECT"):
                result = await session.execute(
                    text("SELECT has_table_privilege('forgeops_app', 'audit_events', :p)"), {"p": privilege}
                )
                assert result.scalar() is True, f"forgeops_app cannot {privilege} audit_events"


@pytest.mark.asyncio
class TestRecomputingFromAnyStartPointReproducesEveryHash:
    @_SETTINGS
    @given(length=st.integers(min_value=1, max_value=6), start=st.integers(min_value=0, max_value=6))
    async def test_verification_from_any_start_point_is_sound(
        self,
        sessions: async_sessionmaker[AsyncSession],
        length: int,
        start: int,
    ) -> None:
        """A.8: `prev ← (from_seq = 0) ? ZERO32 : HashAt(from_seq − 1)`.

        Quantified over the start point because that parameter is what makes verification
        affordable on a table designed to grow without bound — and it is only sound because the
        predecessor's hash is READ from the database rather than assumed to be genesis.
        """
        writer = AuditWriter()
        tenant = uuid.uuid4()
        seqs = await write_chain(sessions, tenant, length)
        since = 0 if start == 0 else seqs[min(start, len(seqs)) - 1]
        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=since)
        assert result.ok, result.divergence
        expected_rows = length if since == 0 else len([s for s in seqs if s >= since])
        assert result.rows_checked == expected_rows

    @_SETTINGS
    @given(length=st.integers(min_value=2, max_value=5))
    async def test_the_first_record_chains_from_genesis_and_the_rest_from_their_predecessor(
        self,
        sessions: async_sessionmaker[AsyncSession],
        length: int,
    ) -> None:
        """The chain's shape, read back from the database rather than from the writer's return."""
        tenant = uuid.uuid4()
        await write_chain(sessions, tenant, length)
        async with sessions() as session:
            result = await session.execute(
                text("SELECT seq, prev_hash, hash FROM audit_events WHERE tenant_id = :t ORDER BY seq"),
                {"t": tenant},
            )
            rows = result.mappings().all()
        assert bytes(rows[0]["prev_hash"]) == GENESIS_PREV_HASH
        for previous, current in zip(rows, rows[1:], strict=False):
            assert bytes(current["prev_hash"]) == bytes(previous["hash"])
        assert len({bytes(row["hash"]) for row in rows}) == length, "distinct records must hash distinctly"

    @_SETTINGS
    @given(tenants=st.integers(min_value=2, max_value=3), length=st.integers(min_value=1, max_value=3))
    async def test_tenants_have_independent_chains(
        self,
        sessions: async_sessionmaker[AsyncSession],
        tenants: int,
        length: int,
    ) -> None:
        """Interleaved writes across tenants must not chain into each other.

        The lock is per tenant (§11.9), so a chain that read the global tip instead of its own
        would still verify in isolation and break the moment two tenants wrote concurrently.
        """
        writer = AuditWriter()
        identifiers = [uuid.uuid4() for _ in range(tenants)]
        async with sessions() as session:
            for index in range(length):
                for tenant in identifiers:
                    await writer.append(session, draft(tenant, index=index))
            await session.commit()
        async with sessions() as session:
            for tenant in identifiers:
                result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
                assert result.ok, (tenant, result.divergence)
                assert result.rows_checked == length


@pytest.mark.asyncio
class TestTamperingIsLocalisedToTheRowThatWasEdited:
    @_SETTINGS
    @given(
        length=st.integers(min_value=1, max_value=4),
        position=st.integers(min_value=0, max_value=3),
        column=st.sampled_from(tuple(TAMPERABLE)),
        variant=st.integers(min_value=0, max_value=2),
    )
    async def test_an_edited_row_is_reported_at_its_own_seq(
        self,
        sessions: async_sessionmaker[AsyncSession],
        head_engine: AsyncEngine,
        length: int,
        position: int,
        column: str,
        variant: int,
    ) -> None:
        """The localisation clause, over every field, every position and every chain length.

        `kind == "hash"` rather than `"prev_hash"`: the edited row's own semantic fields no longer
        reproduce its stored hash, while its `prev_hash` column is untouched and still equals its
        predecessor's. D-61 added the second comparison precisely so the two are distinguishable,
        and asserting the kind is what keeps that distinction meaningful.
        """
        tenant = uuid.uuid4()
        seqs = await write_chain(sessions, tenant, length)
        target = seqs[position % len(seqs)]
        values = TAMPERABLE[column]
        await tamper(head_engine, seq=target, column=column, value=values[variant % len(values)])

        writer = AuditWriter()
        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        assert not result.ok, f"editing {column} at seq {target} was not detected"
        assert result.divergence is not None
        assert result.divergence.seq == target, f"reported {result.divergence.seq}, tampered {target}"
        assert result.divergence.kind == "hash"
        assert result.rows_checked == seqs.index(target), "rows before the tamper must verify"

    @_SETTINGS
    @given(length=st.integers(min_value=2, max_value=4), position=st.integers(min_value=1, max_value=3))
    async def test_rewriting_prev_hash_alone_is_reported_as_prev_hash(
        self,
        sessions: async_sessionmaker[AsyncSession],
        head_engine: AsyncEngine,
        length: int,
        position: int,
    ) -> None:
        """D-61's clause, quantified. Appendix A.8's verifier as written never read this column.

        An actor who rewrites one row's `prev_hash` and recomputes every later `hash` produces a
        chain that is internally consistent and no longer describes the history it came from. The
        row here is only *partially* forged — the later hashes are left alone — but the assertion
        is about which comparison fires first, and it must be the `prev_hash` one.
        """
        tenant = uuid.uuid4()
        seqs = await write_chain(sessions, tenant, length)
        # Always a SUCCESSOR, computed rather than filtered. An earlier version picked
        # `seqs[position % len(seqs)]` and skipped when that landed on the first row — but the
        # first row's `prev_hash` is genesis, so there was nothing to assert. A `pytest.skip`
        # inside a property is the silent-skip shape §0.4.4 exists to forbid, and here it was
        # avoidable arithmetic rather than a real capability gap.
        target = seqs[1 + (position - 1) % (len(seqs) - 1)]
        assert target != seqs[0]
        async with head_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_update"))
            try:
                await conn.execute(
                    text("UPDATE audit_events SET prev_hash = :zero WHERE seq = :seq"),
                    {"zero": GENESIS_PREV_HASH, "seq": target},
                )
            finally:
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_update"))

        writer = AuditWriter()
        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        assert not result.ok
        assert result.divergence is not None
        assert result.divergence.seq == target
        assert result.divergence.kind == "prev_hash"

    @_SETTINGS
    @given(length=st.integers(min_value=3, max_value=5))
    async def test_an_untampered_chain_of_any_length_verifies(
        self,
        sessions: async_sessionmaker[AsyncSession],
        length: int,
    ) -> None:
        """The control for this whole class. Without it, every tamper assertion above would pass
        for a verifier that reports a divergence unconditionally."""
        writer = AuditWriter()
        tenant = uuid.uuid4()
        await write_chain(sessions, tenant, length)
        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        assert result.ok, result.divergence
        assert result.rows_checked == length


@pytest.mark.asyncio
class TestASplicedChainIsDetected:
    """The clause the `|| prev_hash` concatenation is the *only* defence against.

    D-61 added an explicit comparison of each row's stored `prev_hash` against its predecessor's
    `hash`, and that alone catches a rewritten `prev_hash`. What it cannot catch is a **splice**:
    delete a middle row and re-link the next one to the row before it. After that edit the chain
    is internally consistent by the column comparison — every `prev_hash` really does equal its
    new predecessor's `hash` — and the only thing that still objects is that the surviving row's
    own hash was computed over `payload || hash(the row that is now gone)`.

    Drop the concatenation and a splice becomes invisible. That is why this clause exists, and it
    is the clause Q-05's negative control breaks: without it the control installs cleanly and the
    property passes, which was observed directly while building this leaf.
    """

    @_SETTINGS
    @given(length=st.integers(min_value=3, max_value=5))
    async def test_deleting_a_middle_row_and_relinking_is_caught(
        self,
        sessions: async_sessionmaker[AsyncSession],
        head_engine: AsyncEngine,
        length: int,
    ) -> None:
        """Remove row k, point row k+1 at row k−1, and require the chain to object.

        Both triggers are disabled for the edit, because this attacker has the database. The
        resulting table has no `seq` gap detectable by adjacency alone — `seq` is a `BIGSERIAL`
        and gaps are ordinary — so a verifier that only compared `prev_hash` to its predecessor
        would report the spliced chain healthy.
        """
        writer = AuditWriter()
        tenant = uuid.uuid4()
        seqs = await write_chain(sessions, tenant, length)
        victim = seqs[1]
        successor = seqs[2]

        async with head_engine.begin() as conn:
            hashes = await conn.execute(
                text("SELECT seq, hash FROM audit_events WHERE tenant_id = :t ORDER BY seq"), {"t": tenant}
            )
            by_seq = {int(row[0]): bytes(row[1]) for row in hashes}
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_delete"))
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_update"))
            try:
                await conn.execute(text("DELETE FROM audit_events WHERE seq = :seq"), {"seq": victim})
                await conn.execute(
                    text("UPDATE audit_events SET prev_hash = :prev WHERE seq = :seq"),
                    {"prev": by_seq[seqs[0]], "seq": successor},
                )
            finally:
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_update"))
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_delete"))

        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        assert not result.ok, (
            "a spliced chain verified: row "
            f"{victim} was deleted and row {successor} re-linked to its predecessor, and nothing "
            "objected. The `|| prev_hash` concatenation is the only thing that detects this."
        )
        assert result.divergence is not None
        assert result.divergence.seq == successor, f"reported {result.divergence.seq}, spliced at {successor}"
        # `hash`, not `prev_hash`: the column comparison now AGREES, because the re-link is
        # internally consistent. What fails is that the surviving row's own hash was computed over
        # the predecessor that is now gone.
        assert result.divergence.kind == "hash"

    @_SETTINGS
    @given(length=st.integers(min_value=3, max_value=4))
    async def test_the_relink_alone_is_what_makes_the_splice_subtle(
        self,
        sessions: async_sessionmaker[AsyncSession],
        head_engine: AsyncEngine,
        length: int,
    ) -> None:
        """The control that shows the splice is not caught by the trivial route.

        After the splice, every surviving row's `prev_hash` equals its new predecessor's `hash`.
        Asserted directly, so the test above cannot be passing merely because the re-link was done
        wrong — which would make it a test about a clumsy attacker rather than about the chain.
        """
        tenant = uuid.uuid4()
        seqs = await write_chain(sessions, tenant, length)

        async with head_engine.begin() as conn:
            hashes = await conn.execute(
                text("SELECT seq, hash FROM audit_events WHERE tenant_id = :t ORDER BY seq"), {"t": tenant}
            )
            by_seq = {int(row[0]): bytes(row[1]) for row in hashes}
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_delete"))
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_update"))
            try:
                await conn.execute(text("DELETE FROM audit_events WHERE seq = :seq"), {"seq": seqs[1]})
                await conn.execute(
                    text("UPDATE audit_events SET prev_hash = :prev WHERE seq = :seq"),
                    {"prev": by_seq[seqs[0]], "seq": seqs[2]},
                )
            finally:
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_update"))
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_delete"))
            rows = (
                (
                    await conn.execute(
                        text("SELECT seq, prev_hash, hash FROM audit_events WHERE tenant_id = :t ORDER BY seq"),
                        {"t": tenant},
                    )
                )
                .mappings()
                .all()
            )

        assert len(rows) == length - 1
        for previous, current in zip(rows, rows[1:], strict=False):
            assert bytes(current["prev_hash"]) == bytes(previous["hash"]), (
                "the splice left an inconsistent prev_hash, so the detection test above would be "
                "catching a clumsy edit rather than the chain doing its job"
            )


class TestEverySemanticFieldIsCoveredByTheHash:
    @pytest.mark.parametrize("index", [0, 1, 7])
    def test_no_tamper_value_matches_the_honest_row(self, index: int) -> None:
        """A tamper value equal to the honest value tampers with nothing.

        The first version of this file listed `"allowed"` among the `outcome` variants, which is
        what `draft()` already writes — so that example edited nothing, the chain correctly still
        verified, and the property failed. This closes the hole rather than relying on whoever
        edits the matrix next to notice.
        """
        honest = draft(uuid.uuid4(), index=index)
        for column, values in TAMPERABLE.items():
            current = getattr(honest, column)
            for value in values:
                assert value != current, f"{column} variant {value!r} equals the honest value"

    def test_the_hashed_field_set_is_every_column_except_seq_hash_and_prev_hash(self) -> None:
        """Derived from the table, so a column added by a later migration fails here.

        A field left outside `SEMANTIC_FIELDS` would be editable without breaking any hash — a
        hole no chain test could notice, because the chain would still verify.
        """
        columns = {column.name for column in AuditEvent.__table__.columns}
        assert set(SEMANTIC_FIELDS) == columns - {"seq", "hash", "prev_hash"}

    @pytest.mark.asyncio
    @_SETTINGS
    @given(column=st.sampled_from(tuple(TAMPERABLE)))
    async def test_each_tamperable_column_is_in_the_hashed_set(self, column: str) -> None:
        """The tamper matrix and the hashed set must agree, or a passing tamper test could be
        exercising a column the hash never covered."""
        assert column in SEMANTIC_FIELDS


@pytest.mark.asyncio
class TestTheChainSurvivesRealisticVolume:
    async def test_a_longer_chain_verifies_and_localises(
        self,
        sessions: async_sessionmaker[AsyncSession],
        head_engine: AsyncEngine,
    ) -> None:
        """One deterministic longer case beside the generated short ones.

        Generated examples stay short so the budget buys breadth. This asserts the same two
        claims once at a length where an off-by-one in the start-point arithmetic would show:
        twenty rows, verified whole, then tampered in the middle and localised.
        """
        writer = AuditWriter()
        tenant = uuid.uuid4()
        seqs = await write_chain(sessions, tenant, 20)
        async with sessions() as session:
            assert (await writer.verify_chain(session, tenant_id=tenant, since_seq=0)).ok

        target = seqs[10]
        await tamper(head_engine, seq=target, column="reason", value="edited in the middle")
        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        assert result.divergence is not None
        assert result.divergence.seq == target
        assert result.rows_checked == 10

        # And from a start point AFTER the tamper the chain verifies again, which is what makes
        # `since_seq` usable during an incident: the operator can bound the damage.
        async with sessions() as session:
            after = await writer.verify_chain(session, tenant_id=tenant, since_seq=seqs[11])
        assert after.ok, after.divergence
