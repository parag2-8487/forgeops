# SPDX-License-Identifier: FSL-1.1-ALv2
"""`policy_evaluations` — the row that makes double evaluation auditable (§11.7, FR-37).

Design: §6.2, §11.7; task 9.2; deliverable 1.7; criterion 7.

Why the row matters, and why it is asserted separately from the audit record
---------------------------------------------------------------------------
`policy_evaluations.side` exists so that a disagreement between the backend's OPA server and
the agent's embedded evaluator is a row you can query for rather than an invisible bug
(§1.10). Leaf 9.2 writes the `side="backend"` half; leaf 9.4 writes the agent's. If nothing
wrote either, Q-06 would still pass — two evaluators can agree perfectly and leave no
evidence that they were ever asked — so the write needs its own assertions.

The audit record and this row answer different questions and both are checked here in the
same transit: the audit record says a governance transit happened and why, and this row says
what the policy engine answered. A refused transit has both, which is the case that would be
easiest to get wrong: the row is written **before** the refusal, so a deny leaves the
evidence of its own reason behind.

Every query below is scoped by a fresh reason MARKER rather than by the project, and that is
finding 72 rather than a stylistic choice: the table has no `project_id`, and `change_set_id`
is NULL for every stage-1 row because A.3 evaluates policy before `InsertChangeSet`. See
`chokepoint_support.policy_evaluations`.

Real PostgreSQL, because every claim below is a claim about what committed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.core.errors import ProblemException
from src.governance.chokepoint import APPLY_OPERATION, MutationRequest
from src.governance.policy import (
    GovernanceDecision,
    PolicyDocumentUndefinedError,
    PolicySourceUnavailableError,
)

from .chokepoint_support import (
    RecordingSink,
    ScriptedPolicy,
    audit_rows,
    build_chokepoint,
    make_fixture,
    one_create,
    policy_evaluations,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


def marker() -> str:
    """A token unique to one test, carried in the decision reason so its row is findable."""
    return f"marker-{uuid.uuid4().hex}"


class TestOneRowPerDecision:
    async def test_an_allow_is_recorded_with_its_rule_and_reason(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        token = marker()
        policy = ScriptedPolicy(
            decision=GovernanceDecision(result="allow", reason=f"no rule objected {token}", rule_id="governance.allow")
        )
        chokepoint = build_chokepoint(policy=policy, sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create(), reason="add a compose file"),
                principal=fixture.principal,
            )

            rows = await policy_evaluations(session, marker=token)
            assert len(rows) == 1
            row = rows[0]
            assert row["side"] == "backend"
            assert row["result"] == "allow"
            assert row["operation"] == APPLY_OPERATION
            # FR-37 wants the rule id AND the human-readable reason. Both in one column,
            # because `policy_evaluations` has no `rule_id`: the rule is prefixed in brackets
            # so it stays machine-findable without a migration this leaf does not own.
            assert row["reason"] == f"[governance.allow] no rule objected {token}"
            # NULL, and that is correct rather than a gap: A.3 evaluates policy at stage 1,
            # before `InsertChangeSet`, so no change set exists to point at yet.
            assert row["change_set_id"] is None

    async def test_a_require_approval_is_recorded(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        token = marker()
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(
                decision=GovernanceDecision(
                    result="require_approval", reason=f"prod requires approval {token}", rule_id="approval.required"
                )
            ),
            sink=sink,
            redis_client=redis_client,
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create(), reason="prod change"),
                principal=fixture.principal,
            )

            rows = await policy_evaluations(session, marker=token)
            assert [row["result"] for row in rows] == ["require_approval"]
            assert rows[0]["reason"].startswith("[approval.required] ")

    async def test_a_deny_records_its_reason_before_refusing(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """The case most easily got wrong: the refusal must not discard its own evidence.

        `_refuse` raises, so a row written after it would never exist for exactly the transits
        an operator most wants explained.
        """
        token = marker()
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(
                decision=GovernanceDecision(
                    result="deny", reason=f"friday deploy window is closed {token}", rule_id="schedule.blocked_window"
                )
            ),
            sink=sink,
            redis_client=redis_client,
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="friday deploy"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.status == 403

        # A FRESH session, so the assertion is about what committed rather than about the
        # writing session's identity map.
        async with sessions() as verify:
            rows = await policy_evaluations(verify, marker=token)
            assert [row["result"] for row in rows] == ["deny"]
            assert "friday deploy window is closed" in rows[0]["reason"]
            # Both records exist, and they say different things.
            assert [row["action"] for row in await audit_rows(verify, fixture.project_id)] == ["mutation_denied"]

    async def test_an_engine_outage_records_the_deny_it_synthesises(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """§11.6's "an OPA outage denies" leaves a row saying so, not silence.

        Without this, the one situation where an operator needs to know why every mutation is
        being refused would be the one situation with nothing in the table.
        """
        token = marker()
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(raises=PolicySourceUnavailableError(f"connection refused {token}")),
            sink=sink,
            redis_client=redis_client,
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException):
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="any change"),
                    principal=fixture.principal,
                )

        async with sessions() as verify:
            rows = await policy_evaluations(verify, marker=token)
            assert [row["result"] for row in rows] == ["deny"]
            assert "policy engine unavailable" in rows[0]["reason"]

    async def test_an_undefined_document_records_no_row(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """Deliberate, and the reason is the table's own vocabulary.

        `policy_evaluations.result` is constrained to allow/deny/require_approval. An undefined
        document produced no decision, so there is nothing truthful to write; inventing a
        fourth result to record a non-decision would make the constraint a lie and would undo
        D-25's distinction inside the table. The `policy_undefined` audit record is the
        evidence, and it is asserted here so "no row" is a stated property rather than a gap.
        """
        token = marker()
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(raises=PolicyDocumentUndefinedError(f"no such document {token}")),
            sink=sink,
            redis_client=redis_client,
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.submit(
                    session,
                    MutationRequest(project_id=fixture.project_id, items=one_create(), reason="any change"),
                    principal=fixture.principal,
                )
            assert raised.value.problem.status == 503

        async with sessions() as verify:
            assert await policy_evaluations(verify, marker=token) == []
            assert [row["action"] for row in await audit_rows(verify, fixture.project_id)] == ["policy_undefined"]


class TestTheRowIsCommittedWithTheTransit:
    async def test_a_rolled_back_transit_leaves_no_evaluation_row(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """Q-04's argument, applied to a second table.

        The row joins the caller's transaction, so a transit that rolls back leaves neither the
        audit record nor the evaluation. This is what makes writing it in the chokepoint rather
        than in the policy client load-bearing (D-93): a client holding its own connection
        could commit an evaluation for a transit that never happened.
        """
        token = marker()
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(
                decision=GovernanceDecision(result="allow", reason=f"nothing objected {token}", rule_id=None)
            ),
            sink=sink,
            redis_client=redis_client,
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            admitted = await chokepoint._admit(  # noqa: SLF001
                session, project_id=fixture.project_id, principal=fixture.principal
            )
            await chokepoint._evaluate_policy(  # noqa: SLF001 - stage 1 in isolation, on purpose
                session,
                principal=fixture.principal,
                admitted=admitted,
                operation=APPLY_OPERATION,
                items=one_create(),
            )
            # Present in this session before the rollback: the positive control, without which
            # "absent afterwards" would pass for a method that never wrote anything.
            assert len(await policy_evaluations(session, marker=token)) == 1
            await session.rollback()

        async with sessions() as verify:
            assert await policy_evaluations(verify, marker=token) == []


class TestTheEnvironmentReachesThePolicy:
    async def test_the_requested_environment_is_in_the_stage_one_payload(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        """`MutationRequest.environment` is what makes "require approval for production" reachable.

        Without it the bundle's `input.environment` would always be absent and every transit
        would answer `require_approval` — safe, and useless, because A.3's auto-approve path
        would be unreachable for every caller.
        """
        policy = ScriptedPolicy(decision=GovernanceDecision(result="allow", reason="ok", rule_id=None))
        chokepoint = build_chokepoint(policy=policy, sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=fixture.project_id,
                    items=one_create(),
                    reason="ship it",
                    environment="prod",
                ),
                principal=fixture.principal,
            )

            assert len(policy.calls) == 1
            assert policy.calls[0]["environment"] == "prod"
            assert policy.calls[0]["policy_parameters"] == {}

    async def test_an_unstated_environment_arrives_as_none_not_as_a_default(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: Any
    ) -> None:
        policy = ScriptedPolicy(decision=GovernanceDecision(result="allow", reason="ok", rule_id=None))
        chokepoint = build_chokepoint(policy=policy, sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            await chokepoint.submit(
                session,
                MutationRequest(project_id=fixture.project_id, items=one_create(), reason="ship it"),
                principal=fixture.principal,
            )

            assert policy.calls[0]["environment"] is None

    def test_the_request_type_does_not_default_the_environment(self) -> None:
        """Asserted on the type, so a later default cannot be added without this failing."""
        request = MutationRequest(project_id=uuid.uuid4(), items=one_create(), reason="ship it")

        assert request.environment is None
