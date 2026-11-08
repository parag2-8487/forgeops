# SPDX-License-Identifier: FSL-1.1-ALv2
"""§2.2's live deployment log stream.

Three properties. The channel name must equal the hub's, or the stream subscribes to nobody and shows an
empty log for a working deployment. An undelivered deployment is refused rather than streamed, because an
empty stream reads as "nothing is happening" instead of "a human has not approved this". And the `log`
event type exists in the closed vocabulary, so a client listening for it is listening for something a
producer can actually emit.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from src.core.sse import SSEEventType, format_event
from src.deployments.logs import PROGRESS_CHANNEL, _deployment_row
from src.deployments.service import DeploymentService
from src.environments.service import EnvironmentService
from src.websocket.hub import _PROGRESS_CHANNEL

from tests.integration.chokepoint_support import make_fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


def test_the_channel_name_matches_the_hub() -> None:
    """Held together here because the route may not import the transport.

    If these drift, the stream subscribes to a channel nobody publishes on and renders an empty log for a
    deployment that is producing output — the quietest possible failure.
    """
    assert PROGRESS_CHANNEL == _PROGRESS_CHANNEL


def test_log_is_in_the_closed_event_vocabulary() -> None:
    """And is distinct from `progress`, which carries a percentage rather than text."""
    assert SSEEventType.LOG.value == "log"
    assert SSEEventType.LOG != SSEEventType.PROGRESS
    frame = format_event(SSEEventType.LOG, {"message": "applying 2 manifest(s)"})
    assert frame.startswith("event: log")
    assert "applying 2 manifest(s)" in frame


async def test_an_undelivered_deployment_has_no_command_to_stream(sessions: Any, redis_client: Any) -> None:
    """The row carries `command_id = NULL` until delivery, which is what the route refuses on."""
    service = DeploymentService()
    environments = EnvironmentService(pepper="0123456789abcdef0123456789abcdef")
    async with sessions() as session:
        fixture = await make_fixture(session)
        environment = await environments.create(
            session,
            project_id=fixture.project_id,
            tenant_id=None,
            name="staging",
            kind="staging",
            k8s_context="kind-forgeops",
            requires_approval=True,
        )
        record = await service.create(
            session,
            project_id=fixture.project_id,
            environment_id=environment.id,
            tenant_id=None,
            manifests=["k8s/deployment.yaml"],
            cluster_context="kind-forgeops",
            namespace="default",
            requested_by=None,
        )
        row = await _deployment_row(session, fixture.project_id, record.id)

    assert row is not None
    # NULL, not an empty string: "not delivered" and "delivered as nothing" are different claims.
    assert row["command_id"] is None


async def test_a_deployment_of_another_project_is_not_found(sessions: Any, redis_client: Any) -> None:
    """The lookup is scoped by project, so one project's stream cannot be opened from another's route."""
    service = DeploymentService()
    environments = EnvironmentService(pepper="0123456789abcdef0123456789abcdef")
    async with sessions() as session:
        fixture = await make_fixture(session)
        environment = await environments.create(
            session,
            project_id=fixture.project_id,
            tenant_id=None,
            name="staging",
            kind="staging",
            k8s_context="kind-forgeops",
            requires_approval=False,
        )
        record = await service.create(
            session,
            project_id=fixture.project_id,
            environment_id=environment.id,
            tenant_id=None,
            manifests=["k8s/deployment.yaml"],
            cluster_context="kind-forgeops",
            namespace="default",
            requested_by=None,
        )
        foreign = await _deployment_row(session, uuid.uuid4(), record.id)

    assert foreign is None


async def test_the_command_id_is_recorded_on_delivery(sessions: Any, redis_client: Any) -> None:
    """Read back from the row rather than trusting the writer.

    This is the correlation the whole stream depends on, and it is written after a successful send so a
    failed delivery cannot claim a command that never left.
    """
    from tests.integration.chokepoint_support import ScriptedPolicy, allow, build_chokepoint

    class Sink:
        def __init__(self) -> None:
            self.sent: list[Any] = []

        async def send_command(self, *, device_id: uuid.UUID, command: Any) -> Any:
            self.sent.append(command)
            return None

    sink = Sink()
    chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
    async with sessions() as session:
        fixture = await make_fixture(session)
        submission = await chokepoint.deploy_manifests(
            session,
            project_id=fixture.project_id,
            principal=fixture.principal,
            deployment_id=uuid.uuid4(),
            environment_name="staging",
            environment_requires_approval=False,
            manifests=["k8s/deployment.yaml"],
            cluster_context="kind-forgeops",
            namespace="default",
            health_timeout_seconds=120,
            reason="a test deployment",
        )
        stored = (
            await session.execute(
                text("SELECT command_id FROM change_sets WHERE id = :id"),
                {"id": submission.change_set_id},
            )
        ).scalar_one()

    assert submission.outcome == "applying"
    assert len(sink.sent) == 1
    # The recorded id is the one that actually went out.
    assert stored == sink.sent[0].envelope["command_id"]
    assert stored
