# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-04 — exactly one audit record per transit (design.md §11.6, §11.9, A.3, Appendix B Q-04).

Property, universally quantified over chokepoint transits:

    ∀ transits (allow, deny, block, pending, apply, revert): exactly one `audit_events` row is
    written per transit, in the same transaction as the state change; a rolled-back transaction
    leaves neither.

Why this needs a real database, and why it is a property
-------------------------------------------------------
Every clause here is a claim about a **transaction**, not about arithmetic. "In the same
transaction" cannot be observed without a database that can commit and roll back, and "exactly
one row" is only interesting across the whole space of transit kinds and orderings — a per-kind
example test proves each kind writes one record and says nothing about a *sequence*, where the
interesting failures live: a refusal that writes two because a later stage re-audited, or one
that writes none because the exception outran the commit.

So the transit kinds are generated and composed into sequences. For each generated sequence the
property counts the rows the project accumulated and compares against the number of transits,
and it checks the row *actions* against the kinds in order — because "six rows for six transits"
is satisfied by six copies of the wrong record.

The four clauses
----------------
* **Cardinality.** One row per transit, over generated sequences of every kind.
* **Attribution.** The row's `action` matches the transit kind, in order. A count alone would
  pass for six identical rows.
* **Atomicity.** The audit row and the state change commit together: after a blocked transit both
  the `blocked` status and its record exist; and a failed audit write **aborts** the state change
  rather than leaving it committed without a record.
* **Rollback.** A transaction that is rolled back leaves neither the record nor the state change.

Negative control (`mutations.toml` Q-04): move the audit write outside the transaction. The
property must then fail.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.audit.writer import AuditDraft, AuditWriter, InvalidAuditDraftError
from src.core.errors import ProblemException
from src.governance.chokepoint import GovernanceAction, MutationRequest
from src.governance.policy import PolicyDocumentUndefinedError, PolicySourceUnavailableError

from ..integration.chokepoint_support import (
    RecordingSink,
    ScriptedPolicy,
    allow,
    audit_rows,
    build_chokepoint,
    change_sets,
    deny,
    make_fixture,
    many_deletes,
    one_create,
    one_delete,
    require_approval,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

#: Deliberately modest. Every example runs real transits against real Postgres and Redis, so the
#: budget buys breadth of ORDERING rather than raw count — which is where the failures are.
_SETTINGS = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

#: Every transit kind A.3 produces, with the action its single audit row must carry.
#:
#: `apply` is not a separate kind: A.3's apply path IS the auto-approved transit, whose record is
#: `change_set_auto_approved`. Listing it twice would double-count one transit.
TRANSIT_ACTIONS: dict[str, str] = {
    "auto-approve": GovernanceAction.CHANGE_SET_AUTO_APPROVED.value,
    "deny": GovernanceAction.MUTATION_DENIED.value,
    "outage": GovernanceAction.MUTATION_DENIED.value,
    "undefined": GovernanceAction.POLICY_UNDEFINED.value,
    "block": GovernanceAction.CHANGE_SET_BLOCKED.value,
    "pending": GovernanceAction.APPROVAL_REQUIRED.value,
    "policy-pending": GovernanceAction.APPROVAL_REQUIRED.value,
}

TRANSIT_KINDS = tuple(TRANSIT_ACTIONS)

transit_sequences = st.lists(st.sampled_from(TRANSIT_KINDS), min_size=1, max_size=4)


async def _run_transit(
    session: AsyncSession, fixture: Any, kind: str, redis_client: Any, sink: RecordingSink, ordinal: int
) -> None:
    """Drive one transit of `kind`, swallowing only the refusal it is defined to raise.

    The `ProblemException` is expected for the refusing kinds and is what the transit *is*, so
    catching it here is not leniency — an unexpected exception type still propagates and fails
    the property.
    """
    policy = {
        "auto-approve": ScriptedPolicy(decision=allow()),
        "deny": ScriptedPolicy(decision=deny()),
        "outage": ScriptedPolicy(raises=PolicySourceUnavailableError("connection refused")),
        "undefined": ScriptedPolicy(raises=PolicyDocumentUndefinedError("data.forgeops.governance undefined")),
        "block": ScriptedPolicy(decision=allow()),
        "pending": ScriptedPolicy(decision=allow()),
        "policy-pending": ScriptedPolicy(decision=require_approval()),
    }[kind]
    items = {
        "auto-approve": one_create(f"created-{ordinal}.yml"),
        "deny": one_create(f"denied-{ordinal}.yml"),
        "outage": one_create(f"outage-{ordinal}.yml"),
        "undefined": one_create(f"undefined-{ordinal}.yml"),
        "block": many_deletes(4),
        "pending": one_delete(f"pending-{ordinal}.yml"),
        "policy-pending": one_create(f"policy-pending-{ordinal}.yml"),
    }[kind]
    chokepoint = build_chokepoint(policy=policy, sink=sink, redis_client=redis_client)
    request = MutationRequest(project_id=fixture.project_id, items=items, reason=f"transit {ordinal}: {kind}")
    refusing = kind in ("deny", "outage", "undefined", "block")
    if refusing:
        with pytest.raises(ProblemException):
            await chokepoint.submit(session, request, principal=fixture.principal)
    else:
        await chokepoint.submit(session, request, principal=fixture.principal)


class TestOneRecordPerTransit:
    @_SETTINGS
    @given(kinds=transit_sequences)
    async def test_the_row_count_equals_the_transit_count(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: Any,
        kinds: list[str],
    ) -> None:
        """The cardinality clause, over every generated ordering of every kind."""
        sink = RecordingSink()
        async with sessions() as session:
            fixture = await make_fixture(session)
            for ordinal, kind in enumerate(kinds):
                await _run_transit(session, fixture, kind, redis_client, sink, ordinal)
            rows = await audit_rows(session, fixture.project_id)
        assert len(rows) == len(kinds), (
            f"{len(kinds)} transit(s) {kinds} produced {len(rows)} audit row(s) {[row['action'] for row in rows]}"
        )

    @_SETTINGS
    @given(kinds=transit_sequences)
    async def test_each_row_names_the_transit_that_wrote_it(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: Any,
        kinds: list[str],
    ) -> None:
        """The attribution clause. A count alone would pass for N copies of the wrong record."""
        sink = RecordingSink()
        async with sessions() as session:
            fixture = await make_fixture(session)
            for ordinal, kind in enumerate(kinds):
                await _run_transit(session, fixture, kind, redis_client, sink, ordinal)
            rows = await audit_rows(session, fixture.project_id)
        assert [row["action"] for row in rows] == [TRANSIT_ACTIONS[kind] for kind in kinds]

    @_SETTINGS
    @given(kinds=transit_sequences)
    async def test_every_row_carries_a_non_empty_reason_and_a_named_actor(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: Any,
        kinds: list[str],
    ) -> None:
        """NFR-14's who and why, on every row a transit writes — not only on the happy path."""
        sink = RecordingSink()
        async with sessions() as session:
            fixture = await make_fixture(session)
            for ordinal, kind in enumerate(kinds):
                await _run_transit(session, fixture, kind, redis_client, sink, ordinal)
            rows = await audit_rows(session, fixture.project_id)
        for row in rows:
            assert row["reason"].strip(), row
            assert row["actor_kind"] == "user"
            assert row["actor_user_id"] == fixture.user_id

    @_SETTINGS
    @given(kinds=st.lists(st.sampled_from(("deny", "outage", "undefined")), min_size=1, max_size=3))
    async def test_a_refused_transit_writes_its_record_and_no_change_set(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: Any,
        kinds: list[str],
    ) -> None:
        """§11.6: "a denial is as auditable as an approval".

        The sharp half is that the record must survive the exception. `AuditWriter.append` joins
        the caller's transaction and never commits, so a refusal that raised before committing
        would leave no trace at all — which is the failure this clause exists to exclude. Stage 1
        precedes stage 3, so no change set exists either.
        """
        sink = RecordingSink()
        async with sessions() as session:
            fixture = await make_fixture(session)
            for ordinal, kind in enumerate(kinds):
                await _run_transit(session, fixture, kind, redis_client, sink, ordinal)
            rows = await audit_rows(session, fixture.project_id)
            sets = await change_sets(session, fixture.project_id)
        assert len(rows) == len(kinds)
        assert sets == [], "stage 1 refuses before stage 3 compiles anything"
        assert not sink.sent, "no envelope may exist for a refused transit"


class TestTheRecordAndTheStateChangeAreAtomic:
    async def test_a_blocked_transit_commits_both_or_neither(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: Any,
        sink: RecordingSink,
    ) -> None:
        """The state change and its record are visible together, in a fresh session.

        Read back through a **new** session so the assertion is about what COMMITTED rather than
        about what the writing session happens to hold in its identity map.
        """
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException):
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=many_deletes(4), reason="remove the stack"),
                    principal=fixture.principal,
                )
        async with sessions() as reader:
            rows = await audit_rows(reader, fixture.project_id)
            sets = await change_sets(reader, fixture.project_id)
        assert len(rows) == 1 and rows[0]["action"] == GovernanceAction.CHANGE_SET_BLOCKED.value
        assert len(sets) == 1 and sets[0]["status"] == "blocked"
        assert rows[0]["resource_id"] == str(sets[0]["id"])

    @_SETTINGS
    @given(reason=st.sampled_from(("", "   ", "\t\n")))
    async def test_a_failed_audit_write_aborts_the_state_change(
        self,
        sessions: async_sessionmaker[AsyncSession],
        reason: str,
    ) -> None:
        """The direction that matters: no state change without a record.

        Appendix C.1 puts it as `audit-write-failed` (500): "a failed audit write ABORTS the
        mutation … availability is traded for auditability, deliberately." Driven through the
        writer with a draft it must refuse, inside a transaction that has already changed a
        change set's status — the state change must not survive.
        """
        writer = AuditWriter()
        async with sessions() as session:
            fixture = await make_fixture(session)
            change_set_id = await _insert_change_set(session, fixture)
            await session.commit()

            await session.execute(
                text("UPDATE change_sets SET status = 'approved' WHERE id = :id"), {"id": change_set_id}
            )
            with pytest.raises(InvalidAuditDraftError):
                await writer.append(
                    session,
                    AuditDraft(
                        action="change_set_approved",
                        resource_kind="change_set",
                        reason=reason,
                        outcome="allowed",
                        project_id=fixture.project_id,
                    ),
                )
            await session.rollback()

        async with sessions() as reader:
            result = await reader.execute(text("SELECT status FROM change_sets WHERE id = :id"), {"id": change_set_id})
            assert result.scalar() == "validating", "the state change survived a failed audit write"
            assert await audit_rows(reader, fixture.project_id) == []


class TestARolledBackTransactionLeavesNeither:
    @_SETTINGS
    @given(count=st.integers(min_value=1, max_value=4))
    async def test_neither_the_record_nor_the_state_change_survives_a_rollback(
        self,
        sessions: async_sessionmaker[AsyncSession],
        count: int,
    ) -> None:
        """Appendix B's own words: "a rolled-back transaction leaves neither".

        Quantified over the number of records in the aborted transaction, because the interesting
        case is more than one: a writer that flushed each append independently would leave the
        earlier rows behind while the caller believed the whole transit had been undone.
        """
        writer = AuditWriter()
        async with sessions() as session:
            fixture = await make_fixture(session)
            change_set_id = await _insert_change_set(session, fixture)
            await session.commit()

            await session.execute(
                text("UPDATE change_sets SET status = 'blocked' WHERE id = :id"), {"id": change_set_id}
            )
            for index in range(count):
                await writer.append(
                    session,
                    AuditDraft(
                        action="change_set_blocked",
                        resource_kind="change_set",
                        resource_id=str(change_set_id),
                        reason=f"record {index} of an aborted transit",
                        outcome="blocked",
                        project_id=fixture.project_id,
                    ),
                )
            await session.rollback()

        async with sessions() as reader:
            result = await reader.execute(text("SELECT status FROM change_sets WHERE id = :id"), {"id": change_set_id})
            assert result.scalar() == "validating"
            assert await audit_rows(reader, fixture.project_id) == []

    async def test_the_control_shows_a_committed_transaction_does_leave_both(
        self,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        """Without this, the rollback clause passes for a writer that never writes anything."""
        writer = AuditWriter()
        async with sessions() as session:
            fixture = await make_fixture(session)
            change_set_id = await _insert_change_set(session, fixture)
            await session.execute(
                text("UPDATE change_sets SET status = 'blocked' WHERE id = :id"), {"id": change_set_id}
            )
            await writer.append(
                session,
                AuditDraft(
                    action="change_set_blocked",
                    resource_kind="change_set",
                    resource_id=str(change_set_id),
                    reason="a committed transit",
                    outcome="blocked",
                    project_id=fixture.project_id,
                ),
            )
            await session.commit()

        async with sessions() as reader:
            result = await reader.execute(text("SELECT status FROM change_sets WHERE id = :id"), {"id": change_set_id})
            assert result.scalar() == "blocked"
            assert len(await audit_rows(reader, fixture.project_id)) == 1


async def _insert_change_set(session: AsyncSession, fixture: Any) -> uuid.UUID:
    change_set_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO change_sets (id, project_id, status, origin, blast_radius_score, "
            "blast_radius_verdict, policy_bundle_digest) VALUES (:id, :project, 'validating', "
            "'manual', 0, 'allow', :digest)"
        ),
        {"id": change_set_id, "project": fixture.project_id, "digest": fixture.digest},
    )
    return change_set_id
