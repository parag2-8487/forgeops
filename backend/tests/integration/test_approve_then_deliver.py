# SPDX-License-Identifier: FSL-1.1-ALv2
"""Approving with no agent connected must not strand a change set for ever.

THE DEFECT, in three layers.

`_deliver` runs AFTER the approval commits, and `sink.send_command` raises `device-not-connected`
rather than queueing. So approving before starting the agent produced a durable, correct, committed
human decision and a change set no later action could apply:

  * `CHANGE_SET_TRANSITIONS` declares `("approved", "applying")`.
  * `_deliver`'s docstring says a failed send "leaves the set `approved` and retryable".
  * The only traversal of that edge lived inside `approve`, in the very transaction whose send had
    just failed. Nothing retried. The edge was declared, documented as retryable, and unreachable.

On top of that, `device-not-connected` is registered at **409**, and the approvals screen branched on
the status code — so a successful approval whose send failed was reported as "This change set moved
since it was displayed", which was false twice: nothing had moved, and the decision had been recorded.

These tests pin the mechanism. The screen's copy is covered in the frontend suite.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.errors import ProblemException
from src.governance.chokepoint import MutationRequest

from .chokepoint_support import (
    RecordingSink,
    ScriptedPolicy,
    allow,
    build_chokepoint,
    make_fixture,
    one_create,
    require_approval,
)

pytestmark = pytest.mark.asyncio


async def _status(session: AsyncSession, change_set_id: uuid.UUID) -> str:
    return str(
        (
            await session.execute(
                text("SELECT status FROM change_sets WHERE id = :i"), {"i": change_set_id}
            )
        ).scalar_one()
    )


class TestApprovingWithNoAgentConnected:
    """The approval is durable; the delivery is retryable; the two are separate facts."""

    async def test_the_approval_survives_a_failed_send(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: object
    ) -> None:
        """The half that already worked, pinned so a fix to the other half cannot break it.

        A human decision must not be discarded because a process happened to be offline. The change set
        must be left `approved` — not `pending_approval`, which would ask for the decision again, and
        not `applying`, which would claim something is in flight when nothing is.
        """
        offline = RecordingSink(
            raises=ProblemException(
                status=409, type_suffix="device-not-connected", title="No agent connected"
            )
        )
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=offline, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            pending = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=fixture.project_id,
                    items=one_create("deploy/app.yaml"),
                    reason="approve before the agent is running",
                ),
                principal=fixture.principal,
            )
            assert pending.status == "pending_approval"

            with pytest.raises(ProblemException):
                await chokepoint.approve(
                    session, change_set_id=pending.change_set_id, principal=fixture.principal
                )

            assert await _status(session, pending.change_set_id) == "approved"
            # The decision is recorded exactly once, which is what makes re-approving both unnecessary
            # and wrong.
            count = (
                await session.execute(
                    text("SELECT count(*) FROM approvals WHERE change_set_id = :cs"),
                    {"cs": pending.change_set_id},
                )
            ).scalar_one()
            assert int(count) == 1

    async def test_delivery_can_be_retried_once_an_agent_is_there(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: object
    ) -> None:
        """The half that did not exist. This is the edge the model declared and nothing traversed."""
        offline = RecordingSink(
            raises=ProblemException(
                status=409, type_suffix="device-not-connected", title="No agent connected"
            )
        )
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=offline, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            pending = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=fixture.project_id,
                    items=one_create("deploy/app.yaml"),
                    reason="approve before the agent is running",
                ),
                principal=fixture.principal,
            )
            with pytest.raises(ProblemException):
                await chokepoint.approve(
                    session, change_set_id=pending.change_set_id, principal=fixture.principal
                )
            assert await _status(session, pending.change_set_id) == "approved"

        # A NEW CHOKEPOINT with a working sink, which is the point: the agent arrived in a later
        # process, exactly as it does when a user starts it after approving.
        online = RecordingSink()
        chokepoint2 = build_chokepoint(
            policy=ScriptedPolicy(decision=allow()), sink=online, redis_client=redis_client
        )
        async with sessions() as session:
            result = await chokepoint2.deliver_approved(
                session, change_set_id=pending.change_set_id, principal=fixture.principal
            )
            assert result.status == "applying"
            assert result.command is not None
            assert len(online.sent) == 1
            assert await _status(session, pending.change_set_id) == "applying"

    async def test_redelivery_mints_no_second_approval(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: object
    ) -> None:
        """One human decision must leave one record.

        A redelivery that wrote another `approvals` row would make a single approval look like two and
        break Q-04's one-row-per-transit. The original approval is the authority; delivery is that
        transit's outcome leaving the building, which is why `_deliver` writes no audit row either.
        """
        offline = RecordingSink(
            raises=ProblemException(
                status=409, type_suffix="device-not-connected", title="No agent connected"
            )
        )
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=offline, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            pending = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=fixture.project_id,
                    items=one_create("deploy/app.yaml"),
                    reason="one decision only",
                ),
                principal=fixture.principal,
            )
            with pytest.raises(ProblemException):
                await chokepoint.approve(
                    session, change_set_id=pending.change_set_id, principal=fixture.principal
                )

        online = RecordingSink()
        chokepoint2 = build_chokepoint(
            policy=ScriptedPolicy(decision=allow()), sink=online, redis_client=redis_client
        )
        async with sessions() as session:
            await chokepoint2.deliver_approved(
                session, change_set_id=pending.change_set_id, principal=fixture.principal
            )
            approvals = (
                await session.execute(
                    text("SELECT count(*) FROM approvals WHERE change_set_id = :cs"),
                    {"cs": pending.change_set_id},
                )
            ).scalar_one()
            assert int(approvals) == 1, "a redelivery must not record a second approval"
            approved_events = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_events WHERE resource_id = :r "
                        "AND action = 'change_set_approved'"
                    ),
                    {"r": str(pending.change_set_id)},
                )
            ).scalar_one()
            assert int(approved_events) == 1, "a redelivery must not record a second approval transit"

    async def test_delivery_is_refused_from_any_other_status(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: object
    ) -> None:
        """Only an approved change set that never reached an agent may be delivered.

        Named distinctly from the send failure so a caller can tell "this is not waiting to be sent"
        from "it is, and the agent is not there" — the exact conflation that made the screen lie.
        """
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=sink, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            pending = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=fixture.project_id,
                    items=one_create("deploy/app.yaml"),
                    reason="still pending",
                ),
                principal=fixture.principal,
            )
            assert await _status(session, pending.change_set_id) == "pending_approval"
            with pytest.raises(ProblemException) as caught:
                await chokepoint.deliver_approved(
                    session, change_set_id=pending.change_set_id, principal=fixture.principal
                )
            assert "not approved" in str(caught.value.problem.detail)


class TestTheStalenessProtectionIsIntact:
    """Fixing the false conflict must not remove the true one."""

    async def test_an_approval_immediately_after_render_succeeds(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: object
    ) -> None:
        """The honest case: send the version the screen displayed, and it works.

        This is what a user does. It has to succeed, or the protection is not protection but a lock.
        """
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=sink, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            pending = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=fixture.project_id,
                    items=one_create("deploy/app.yaml"),
                    reason="approve straight away",
                ),
                principal=fixture.principal,
            )
            displayed = int(
                (
                    await session.execute(
                        text("SELECT version FROM change_sets WHERE id = :i"),
                        {"i": pending.change_set_id},
                    )
                ).scalar_one()
            )
            result = await chokepoint.approve(
                session,
                change_set_id=pending.change_set_id,
                principal=fixture.principal,
                expected_version=displayed,
            )
            assert result.status == "applying"

    async def test_a_genuinely_stale_version_is_still_refused(
        self, sessions: async_sessionmaker[AsyncSession], sink: RecordingSink, redis_client: object
    ) -> None:
        """A tab that displayed an older version must not decide on state it did not review."""
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=sink, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            pending = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=fixture.project_id,
                    items=one_create("deploy/app.yaml"),
                    reason="stale tab",
                ),
                principal=fixture.principal,
            )
            current = int(
                (
                    await session.execute(
                        text("SELECT version FROM change_sets WHERE id = :i"),
                        {"i": pending.change_set_id},
                    )
                ).scalar_one()
            )
            with pytest.raises(ProblemException) as caught:
                await chokepoint.approve(
                    session,
                    change_set_id=pending.change_set_id,
                    principal=fixture.principal,
                    # One BEHIND what the row holds, which is what a stale tab sends.
                    expected_version=current - 1,
                )
            assert "modified concurrently" in str(caught.value.problem.detail)
            # And the change set did not move, so the honest retry after a reload can still succeed.
            assert await _status(session, pending.change_set_id) == "pending_approval"
