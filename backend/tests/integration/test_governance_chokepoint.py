# SPDX-License-Identifier: FSL-1.1-ALv2
"""The six-stage chokepoint against a REAL PostgreSQL and Redis (§2.2, §11.6, A.3; leaf 7.5).

Why these need real services
----------------------------
Every claim worth making about a transit is a claim about a transaction:

* **exactly one** `audit_events` row per transit, committed **with** the state change (Q-04);
* a refused transit leaves a record and **no** change set past `validating`;
* no envelope exists for a denied, blocked or pending change set;
* two concurrent approvals produce one winner and one `409` (Q-22);
* the rollback handle exists **before** the envelope does (A.3's postcondition).

None of those can be observed without a database, and the `seq`/nonce allocator is Redis-
authoritative by §7.6, so a fake Redis would exercise a different allocator from the one that
ships.

What is substituted, and why that is allowed
--------------------------------------------
§0.4.1 permits a **transport** substitution and forbids a **collaborator** substitution. Here the
real `SemanticPlanAnalyzer`, the real `ThresholdApprovalGate`, the real `AuditWriter`, the real
`RedisEnvelopeSequencer`, the real `DeviceService` custody path and the real envelope signing all
run. Two collaborators are substituted, and in both cases the production article **does not exist
yet**:

* `GovernancePolicySource` — leaf 9.2 builds `OpaGovernancePolicy`, and it cannot exist before
  leaf 9.1 authors the bundle it queries. What is composed today is
  `UnavailableGovernancePolicy`, which the chokepoint turns into a deny, and that composed
  default is asserted in `test_wiring_governance.py`. Driving the allow, deny, require-approval
  and undefined paths therefore needs a policy source, and the one below is a hand-written class
  implementing the Protocol — not a `Mock`, which `FO-TD004` forbids under this directory.
* `CommandSink` — leaf 8.4 builds the hub. `RecordingSink` below keeps what it was handed so the
  per-path envelope assertions can read it.

Both doubles are ordinary classes whose methods have the real signatures, which is §0.4.3's
"signature-enforcing double" rather than D-23's reassigned `spec=` child.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.audit.writer import AuditWriter
from src.core.errors import ProblemException
from src.governance.chokepoint import (
    APPLY_OPERATION,
    REVERT_OPERATION,
    ChangeItemRequest,
    GovernanceAction,
    GovernanceChokepoint,
    MutationRequest,
)
from src.governance.envelope import verify_envelope_signature
from src.governance.policy import PolicyDocumentUndefinedError, PolicySourceUnavailableError

# The pure helpers only. `sessions`, `redis_client` and `sink` are pytest FIXTURES and arrive
# through `conftest.py`: importing them by name here would shadow every test method's parameter
# of the same name and produce 88 F811 findings, which is what the first attempt did.
from .chokepoint_support import (
    STALE_DIGEST,
    Fixture,
    RecordingSink,
    ScriptedPolicy,
    allow,
    audit_rows,
    build_chokepoint,
    change_sets,
    deny,
    handles,
    make_fixture,
    many_deletes,
    one_create,
    one_delete,
    require_approval,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


# ─── the seven transits ───────────────────────────────────────────────────────────────────


class TestTheAutoApprovedTransit:
    async def test_all_six_stages_run_and_exactly_one_envelope_is_minted(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        policy = ScriptedPolicy(decision=allow())
        chokepoint = build_chokepoint(policy=policy, sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            result = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                principal=fixture.principal,
            )

            assert result.outcome == "applying"
            assert result.status == "applying"
            assert result.command is not None, "an auto-approved transit must mint an envelope"
            assert len(sink.sent) == 1
            rows = await audit_rows(session, fixture.project_id)
            assert [row["action"] for row in rows] == [str(GovernanceAction.CHANGE_SET_AUTO_APPROVED)]
            assert rows[0]["outcome"] == "allowed"
            assert result.audit_seq == rows[0]["seq"]

    async def test_the_envelope_verifies_under_the_devices_sealed_key(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """End to end through D-62's custody: generate, seal, store, unseal, sign, verify.

        This is the assertion that makes the custody decision more than a docstring — the key
        that signed the envelope came out of `agent_devices.envelope_key_enc`.
        """
        from src.governance.envelope import CommandEnvelope

        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            result = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                principal=fixture.principal,
            )
        assert result.command is not None
        envelope = CommandEnvelope.from_mapping(result.command.envelope)
        assert verify_envelope_signature(envelope, result.command.signature, fixture.key)
        assert not verify_envelope_signature(envelope, result.command.signature, b"a-different-key" * 2)

    async def test_the_envelope_carries_the_operation_the_bundle_and_the_approval(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            result = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                principal=fixture.principal,
            )
        assert result.command is not None
        wire = result.command.envelope
        assert wire["operation"] == APPLY_OPERATION
        assert wire["device_id"] == str(fixture.device_id)
        assert wire["policy_context"] == {"bundle_digest": fixture.digest, "decision": "allow"}
        assert wire["approval_id"] == str(result.approval_id)
        assert wire["seq"] == 1
        assert "signature" not in wire, "the canonical mapping must never carry the signature"
        assert result.command.as_wire()["signature"] == result.command.signature

    async def test_the_rollback_handle_exists_before_the_envelope_does(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """A.3's postcondition, tested by making delivery fail.

        The sink raises **after** recording, so the mint happened and the send did not complete.
        The handle must already be on disk: that is the whole reason stage 6 precedes the mint,
        and the failure it protects against is a crash between mint and apply.
        """
        failing = RecordingSink(raises=RuntimeError("the hub died mid-send"))
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=failing, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(RuntimeError, match="hub died"):
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                    principal=fixture.principal,
                )
            await session.rollback()
            sets = await change_sets(session, fixture.project_id)
            assert len(sets) == 1
            # `approved`, not `applying`: the status advances only after a successful send, so a
            # failed delivery is retryable rather than stuck.
            assert sets[0]["status"] == "approved"
            reserved = await handles(session, sets[0]["id"])
            assert len(reserved) == 1, "the rollback handle must exist even though delivery failed"
            assert reserved[0]["consumed"] is False
            assert reserved[0]["agent_device_id"] == str(fixture.device_id)

    async def test_the_change_items_carry_pre_image_hashes(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """`change_items.old_hash` is what lets the agent refuse a stale apply (§6.3)."""
        import hashlib

        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            result = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=fixture.project_id,
                    items=(
                        ChangeItemRequest(
                            file_path="Dockerfile", action="update", old_content="FROM a", new_content="FROM b"
                        ),
                    ),
                    reason="bump the base image",
                ),
                principal=fixture.principal,
            )
            rows = await session.execute(
                text(
                    "SELECT file_path, action, old_hash, new_hash, ordinal FROM change_items WHERE change_set_id = :cs"
                ),
                {"cs": result.change_set_id},
            )
            item = rows.mappings().one()
        assert item["old_hash"] == hashlib.sha256(b"FROM a").hexdigest()
        assert item["new_hash"] == hashlib.sha256(b"FROM b").hexdigest()
        assert item["ordinal"] == 0


class TestTheDeniedTransit:
    async def test_a_policy_deny_writes_one_record_and_mints_nothing(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=deny()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.status == 403
            assert raised.value.problem.type.endswith("/policy-denied")
            rows = await audit_rows(session, fixture.project_id)
            assert [row["action"] for row in rows] == [str(GovernanceAction.MUTATION_DENIED)]
            assert "friday deploy window" in rows[0]["reason"]
            assert not sink.sent, "no envelope may exist for a denied transit"
            assert await change_sets(session, fixture.project_id) == [], "stage 1 precedes stage 3"

    async def test_an_engine_outage_denies_rather_than_allowing(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """§11.6: "fail closed — an OPA outage denies". The one clause an availability-minded
        change would quietly invert."""
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(raises=PolicySourceUnavailableError("connection refused")),
            sink=sink,
            redis_client=redis_client,
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.type.endswith("/policy-denied")
            rows = await audit_rows(session, fixture.project_id)
            assert len(rows) == 1
            assert "failing closed" in rows[0]["reason"]
            assert not sink.sent

    async def test_an_undefined_document_is_a_503_not_a_deny(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """D-25's lesson: a broken bundle must not be indistinguishable from a working one that
        refuses everyone."""
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(raises=PolicyDocumentUndefinedError("data.forgeops.governance is undefined")),
            sink=sink,
            redis_client=redis_client,
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.status == 503
            assert raised.value.problem.type.endswith("/governance-policy-undefined")
            rows = await audit_rows(session, fixture.project_id)
            assert [row["action"] for row in rows] == [str(GovernanceAction.POLICY_UNDEFINED)]
            assert rows[0]["outcome"] == "failed", "an undefined document is a fault, not a decision"
            assert not sink.sent


class TestAdmissionRefusals:
    """A.3's postcondition says every early return writes exactly one record. Its body is silent
    about three of them; these are those three plus the stale bundle it does cover."""

    async def test_no_active_device_is_refused_and_audited(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session, device_status="pending")
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.type.endswith("/device-not-connected")
            rows = await audit_rows(session, fixture.project_id)
            assert [row["action"] for row in rows] == [str(GovernanceAction.MUTATION_REFUSED)]
            assert "no active agent device" in rows[0]["reason"]

    async def test_a_revoked_device_is_distinguished_from_an_absent_one(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session, device_status="revoked")
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.type.endswith("/device-revoked")
            rows = await audit_rows(session, fixture.project_id)
            assert len(rows) == 1
            assert "revoked" in rows[0]["reason"]

    async def test_a_stale_bundle_is_refused_before_any_policy_call(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        policy = ScriptedPolicy(decision=allow())
        chokepoint = build_chokepoint(policy=policy, sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session, device_digest=STALE_DIGEST)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.type.endswith("/policy-bundle-stale")
            assert policy.calls == [], "stage 0 precedes stage 1"
            rows = await audit_rows(session, fixture.project_id)
            assert len(rows) == 1
            assert "policy bundle stale" in rows[0]["reason"]

    async def test_a_project_with_no_published_bundle_is_refused(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """Fail closed: you cannot govern without a bundle, so a project with none mutates nothing."""
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session, active_digest=None)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.type.endswith("/policy-bundle-stale")

    async def test_an_unknown_project_is_a_non_disclosing_403(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """§4.2 and Q-20: a 404 here would be an enumeration oracle for project ids."""
        from src.core.errors import FORBIDDEN_DETAIL

        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=uuid.uuid4(), items=one_create(), reason="add a compose file"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.status == 403
            assert raised.value.problem.detail == FORBIDDEN_DETAIL


class TestTheBlockedTransit:
    async def test_a_blast_radius_block_persists_the_state_and_mints_nothing(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=many_deletes(4), reason="remove the stack"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.status == 409
            assert raised.value.problem.type.endswith("/blast-radius-blocked")
            sets = await change_sets(session, fixture.project_id)
            assert len(sets) == 1
            assert sets[0]["status"] == "blocked", "the block is persisted, not just reported"
            assert sets[0]["blast_radius_verdict"] == "block"
            assert sets[0]["blast_radius_score"] > 0
            rows = await audit_rows(session, fixture.project_id)
            assert [row["action"] for row in rows] == [str(GovernanceAction.CHANGE_SET_BLOCKED)]
            assert rows[0]["outcome"] == "blocked"
            assert rows[0]["resource_id"] == str(sets[0]["id"])
            assert not sink.sent
            assert await handles(session, sets[0]["id"]) == [], "a blocked set reserves no handle"


class TestThePendingTransit:
    async def test_a_warn_verdict_requires_approval_and_mints_nothing(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            result = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_delete(), reason="drop the compose file"),
                principal=fixture.principal,
            )
            assert result.outcome == "approval-required"
            assert result.status == "pending_approval"
            assert result.command is None, "no envelope may exist for a pending change set"
            assert not sink.sent
            sets = await change_sets(session, fixture.project_id)
            assert sets[0]["status"] == "pending_approval"
            rows = await audit_rows(session, fixture.project_id)
            assert [row["action"] for row in rows] == [str(GovernanceAction.APPROVAL_REQUIRED)]
            assert rows[0]["outcome"] == "pending"
            assert await handles(session, sets[0]["id"]) == [], "the handle is reserved at approval, not at submit"

    async def test_a_policy_require_approval_overrides_an_allowing_gate(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """A.3: `gate = REQUIRES_APPROVAL **OR** decision.result = REQUIRE_APPROVAL`."""
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=sink, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            result = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                principal=fixture.principal,
            )
        assert result.outcome == "approval-required"
        assert result.command is None
        assert not sink.sent


class TestTheApproveTransit:
    async def test_approval_mints_the_envelope_and_records_the_approver(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            pending = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_delete(), reason="drop the compose file"),
                principal=fixture.principal,
            )
            assert pending.change_set_id is not None
            approved = await chokepoint.approve(
                session, change_set_id=pending.change_set_id, principal=fixture.principal, comment="looks right"
            )
            assert approved.outcome == "applying"
            assert approved.command is not None
            assert len(sink.sent) == 1
            approval = await session.execute(
                text("SELECT id, approver_id, status, comment FROM approvals WHERE change_set_id = :cs"),
                {"cs": pending.change_set_id},
            )
            row = approval.mappings().one()
            assert row["approver_id"] == fixture.user_id
            assert row["status"] == "approved"
            assert row["comment"] == "looks right"
            assert approved.approval_id == row["id"]
            assert approved.command.envelope["approval_id"] == str(row["id"])
            rows = await audit_rows(session, fixture.project_id)
            assert [r["action"] for r in rows] == [
                str(GovernanceAction.APPROVAL_REQUIRED),
                str(GovernanceAction.CHANGE_SET_APPROVED),
            ]
            reserved = await handles(session, pending.change_set_id)
            assert len(reserved) == 1

    async def test_the_version_advances_so_a_stale_approval_conflicts(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            pending = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_delete(), reason="drop the compose file"),
                principal=fixture.principal,
            )
            assert pending.change_set_id is not None
            with pytest.raises(ProblemException) as raised:
                await chokepoint.approve(
                    session,
                    change_set_id=pending.change_set_id,
                    principal=fixture.principal,
                    expected_version=99,
                )
            assert raised.value.problem.status == 409
            assert raised.value.problem.type.endswith("/change-set-conflict")

    async def test_two_concurrent_approvals_yield_one_winner_and_one_409(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """Q-22's concurrency clause, over two real transactions on a real database."""
        first_sink, second_sink = RecordingSink(), RecordingSink()
        async with sessions() as setup:
            fixture = await make_fixture(setup)
            pending = await build_chokepoint(
                policy=ScriptedPolicy(decision=allow()), sink=first_sink, redis_client=redis_client
            ).submit(
                setup,
                MutationRequest(project_id=fixture.project_id, items=one_delete(), reason="drop the compose file"),
                principal=fixture.principal,
            )
        assert pending.change_set_id is not None

        async def attempt(sink_for_attempt: RecordingSink) -> str:
            chokepoint = build_chokepoint(
                policy=ScriptedPolicy(decision=allow()), sink=sink_for_attempt, redis_client=redis_client
            )
            async with sessions() as session:
                try:
                    await chokepoint.approve(session, change_set_id=pending.change_set_id, principal=fixture.principal)
                    return "won"
                except ProblemException as exc:
                    return exc.problem.type.rsplit("/", 1)[-1]

        outcomes = await asyncio.gather(attempt(first_sink), attempt(second_sink))
        assert sorted(outcomes) == ["change-set-conflict", "won"], outcomes
        assert len(first_sink.sent) + len(second_sink.sent) == 1, "exactly one envelope for two approvals"
        async with sessions() as session:
            approvals = await session.execute(
                text("SELECT count(*) FROM approvals WHERE change_set_id = :cs"), {"cs": pending.change_set_id}
            )
            assert approvals.scalar() == 1

    async def test_approving_something_that_is_not_pending_conflicts(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            applied = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                principal=fixture.principal,
            )
            assert applied.change_set_id is not None
            with pytest.raises(ProblemException) as raised:
                await chokepoint.approve(session, change_set_id=applied.change_set_id, principal=fixture.principal)
            assert raised.value.problem.type.endswith("/change-set-conflict")

    async def test_a_revoked_device_stops_a_pending_approval(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """Admission runs again at approval, so a change set approved after its device was revoked
        is refused rather than applied."""
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            pending = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_delete(), reason="drop the compose file"),
                principal=fixture.principal,
            )
            await session.execute(
                text("UPDATE agent_devices SET status = 'revoked' WHERE id = :id"), {"id": fixture.device_id}
            )
            await session.commit()
            assert pending.change_set_id is not None
            with pytest.raises(ProblemException) as raised:
                await chokepoint.approve(session, change_set_id=pending.change_set_id, principal=fixture.principal)
            assert raised.value.problem.type.endswith("/device-revoked")
            assert not sink.sent


class TestTheRevertTransit:
    async def _apply(self, session: AsyncSession, chokepoint: GovernanceChokepoint, fixture: Fixture) -> uuid.UUID:
        """Apply an **update**, so the reverse set is also an update.

        Chosen deliberately: the inverse of a `create` is a `delete`, which the analyser scores as
        destructive, so reverting a create requires human approval. That is correct behaviour and
        is asserted separately below — but it would make every revert test here a two-step
        approval flow and hide what those tests are actually about.
        """
        result = await chokepoint.submit(
            session,
            MutationRequest(
                project_id=fixture.project_id,
                items=(
                    ChangeItemRequest(
                        file_path="Dockerfile", action="update", old_content="FROM a\n", new_content="FROM b\n"
                    ),
                ),
                reason="bump the base image",
            ),
            principal=fixture.principal,
        )
        assert result.change_set_id is not None
        # The agent's `command.result` is leaf 8.4's; mark the set applied here so the revert
        # transit has a legal §3.6 predecessor to work from.
        await session.execute(
            text("UPDATE change_sets SET status = 'applied', applied_at = now() WHERE id = :id"),
            {"id": result.change_set_id},
        )
        await session.commit()
        return result.change_set_id

    async def test_reverting_a_create_requires_approval_because_deleting_is_destructive(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """The reverse of a create is a delete, and stage 4 does not auto-approve destruction."""
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            created = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                principal=fixture.principal,
            )
            assert created.change_set_id is not None
            await session.execute(
                text("UPDATE change_sets SET status = 'applied', applied_at = now() WHERE id = :id"),
                {"id": created.change_set_id},
            )
            await session.commit()
            sent_before = len(sink.sent)
            result = await chokepoint.revert(session, change_set_id=created.change_set_id, principal=fixture.principal)
            assert result.outcome == "approval-required"
            assert result.command is None
            assert len(sink.sent) == sent_before, "no envelope for a revert that still needs a human"
            handle = await handles(session, created.change_set_id)
            assert handle[0]["consumed"] is False, "a revert awaiting approval must not consume the handle"

    async def test_a_revert_compiles_the_reverse_set_and_mints_its_own_authority(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            original = await self._apply(session, chokepoint, fixture)
            forward_command = sink.sent[-1][1]

            result = await chokepoint.revert(session, change_set_id=original, principal=fixture.principal)

            assert result.outcome == "reverting"
            assert result.reverse_change_set_id is not None
            assert result.reverse_change_set_id != original
            assert result.command is not None
            assert result.command.envelope["operation"] == REVERT_OPERATION
            assert result.command.envelope["args"]["reverts_change_set_id"] == str(original)
            # A fresh authority, not the original's: different approval id, different digest.
            assert result.command.digest != forward_command.digest
            assert result.command.envelope["approval_id"] != forward_command.envelope["approval_id"]
            assert result.command.envelope["seq"] > forward_command.envelope["seq"]

    async def test_the_reverse_items_invert_the_original(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            original = await self._apply(session, chokepoint, fixture)
            result = await chokepoint.revert(session, change_set_id=original, principal=fixture.principal)
            rows = await session.execute(
                text(
                    "SELECT file_path, action, old_content, new_content FROM change_items "
                    "WHERE change_set_id = :cs ORDER BY ordinal"
                ),
                {"cs": result.reverse_change_set_id},
            )
            reversed_items = list(rows.mappings().all())
        assert len(reversed_items) == 1
        assert reversed_items[0]["action"] == "update", "the inverse of an update swaps its contents"
        assert reversed_items[0]["old_content"] == "FROM b\n"
        assert reversed_items[0]["new_content"] == "FROM a\n"

    async def test_the_original_handle_is_consumed_and_a_second_revert_refuses(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """Single-use, enforced backend-side as well as in the agent (Q-02's clause)."""
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            original = await self._apply(session, chokepoint, fixture)
            await chokepoint.revert(session, change_set_id=original, principal=fixture.principal)
            consumed = await handles(session, original)
            assert consumed[0]["consumed"] is True

            with pytest.raises(ProblemException) as raised:
                await chokepoint.revert(session, change_set_id=original, principal=fixture.principal)
            assert raised.value.problem.status == 409
            assert raised.value.problem.type.endswith("/revert-unavailable")

    async def test_reverting_a_set_that_was_never_applied_refuses(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            pending = await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_delete(), reason="drop the compose file"),
                principal=fixture.principal,
            )
            assert pending.change_set_id is not None
            with pytest.raises(ProblemException) as raised:
                await chokepoint.revert(session, change_set_id=pending.change_set_id, principal=fixture.principal)
            assert raised.value.problem.type.endswith("/revert-unavailable")
            assert "only an applied set" in (raised.value.problem.detail or "")

    async def test_a_denied_revert_writes_one_record_and_leaves_the_handle_alone(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """A revert is a mutation, so a policy deny stops it — and must not consume the handle,
        or a denied revert would make the change set permanently unrevertable."""
        allowing, denying = RecordingSink(), RecordingSink()
        async with sessions() as session:
            fixture = await make_fixture(session)
            original = await self._apply(
                session,
                build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=allowing, redis_client=redis_client),
                fixture,
            )
            before = len(await audit_rows(session, fixture.project_id))
            denier = build_chokepoint(
                policy=ScriptedPolicy(decision=deny("reverts need a change window")),
                sink=denying,
                redis_client=redis_client,
            )
            with pytest.raises(ProblemException) as raised:
                await denier.revert(session, change_set_id=original, principal=fixture.principal)
            assert raised.value.problem.type.endswith("/policy-denied")
            rows = await audit_rows(session, fixture.project_id)
            assert len(rows) == before + 1
            assert rows[-1]["action"] == str(GovernanceAction.MUTATION_DENIED)
            assert not denying.sent
            handle = await handles(session, original)
            assert handle[0]["consumed"] is False


class TestTheAuditChainCoversEveryTransit:
    async def test_the_chain_verifies_after_a_mixed_sequence(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """Six transits of five different kinds, then `verify_chain` over the whole tenant chain.

        The chain is the shared artifact: if any transit wrote its record outside the transaction,
        or wrote two, this is where the arithmetic stops agreeing.
        """
        writer = AuditWriter()
        async with sessions() as session:
            fixture = await make_fixture(session)
            allow_point = build_chokepoint(
                policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client
            )
            deny_point = build_chokepoint(policy=ScriptedPolicy(decision=deny()), sink=sink, redis_client=redis_client)

            await allow_point.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create("a.yml"), reason="one"),
                principal=fixture.principal,
            )
            with pytest.raises(ProblemException):
                await deny_point.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create("b.yml"), reason="two"),
                    principal=fixture.principal,
                )
            with pytest.raises(ProblemException):
                await allow_point.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=many_deletes(4), reason="three"),
                    principal=fixture.principal,
                )
            pending = await allow_point.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_delete("c.yml"), reason="four"),
                principal=fixture.principal,
            )
            assert pending.change_set_id is not None
            await allow_point.approve(session, change_set_id=pending.change_set_id, principal=fixture.principal)

            rows = await audit_rows(session, fixture.project_id)
            assert [row["action"] for row in rows] == [
                str(GovernanceAction.CHANGE_SET_AUTO_APPROVED),
                str(GovernanceAction.MUTATION_DENIED),
                str(GovernanceAction.CHANGE_SET_BLOCKED),
                str(GovernanceAction.APPROVAL_REQUIRED),
                str(GovernanceAction.CHANGE_SET_APPROVED),
            ]
            verification = await writer.verify_chain(session, tenant_id=None, since_seq=0)
        assert verification.ok, verification.divergence
        assert verification.rows_checked >= 5

    async def test_every_transit_names_the_acting_user(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """NFR-14's "who". A record that cannot say who acted is the one that is useless later."""
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                principal=fixture.principal,
            )
            rows = await audit_rows(session, fixture.project_id)
        assert rows[0]["actor_kind"] == "user"
        assert rows[0]["actor_user_id"] == fixture.user_id


class TestTheSequenceAndNonceAreAllocatedPerEnvelope:
    async def test_each_mint_advances_the_sequence_and_mirrors_it_to_the_row(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            for index in range(3):
                await chokepoint.submit(
                    session,
                    MutationRequest(
                        project_id=fixture.project_id, items=one_create(f"f{index}.yml"), reason=f"add {index}"
                    ),
                    principal=fixture.principal,
                )
            mirrored = await session.execute(
                text("SELECT last_seq FROM agent_devices WHERE id = :id"), {"id": fixture.device_id}
            )
            assert mirrored.scalar() == 3
        assert [command.envelope["seq"] for _, command in sink.sent] == [1, 2, 3]
        nonces = {command.envelope["nonce"] for _, command in sink.sent}
        assert len(nonces) == 3, "a repeated nonce would be a replay the agent must reject"
