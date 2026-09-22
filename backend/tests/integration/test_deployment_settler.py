# SPDX-License-Identifier: FSL-1.1-ALv2
"""The deployment settler: the runtime path that was missing. §2.2, §2.6.

Before this existed, `DeploymentService.complete` had no production caller — only tests — so a real
deployment stayed `applying` for ever, `healthy` and `stable` were never written, and `rollback_target` could
never offer anything. These tests drive `record_command_result`, which is what the hub calls when the agent
reports, and READ THE DATABASE afterwards rather than trusting a return value.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.deployments.service import DeploymentService
from src.deployments.settler import DeploymentSettler
from src.environments.service import EnvironmentService
from src.notifications.service import NotificationService

from tests.integration.chokepoint_support import (
    ScriptedPolicy,
    allow,
    build_chokepoint,
    make_fixture,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


class Sink:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_command(self, *, device_id: uuid.UUID, command: Any) -> Any:
        self.sent.append(command)
        return None


async def _deploy(session: AsyncSession, chokepoint: Any, fixture: Any, service: DeploymentService) -> Any:
    """A real deployment through the transit, so the change set is `applying` and linked to the row."""
    environments = EnvironmentService(pepper="0123456789abcdef0123456789abcdef")
    environment = await environments.create(
        session,
        project_id=fixture.project_id,
        tenant_id=None,
        name=f"staging-{uuid.uuid4().hex[:6]}",
        kind="staging",
        k8s_context="kind-forgeops",
        requires_approval=False,
    )
    record = await service.create(
        session,
        project_id=fixture.project_id,
        environment_id=environment.id,
        tenant_id=None,
        manifests=["k8s/deployment.yaml", "k8s/service.yaml"],
        cluster_context="kind-forgeops",
        namespace="default",
        requested_by=None,
    )
    submission = await chokepoint.deploy_manifests(
        session,
        project_id=fixture.project_id,
        principal=fixture.principal,
        deployment_id=record.id,
        environment_name=environment.name,
        environment_requires_approval=False,
        manifests=list(record.manifests),
        cluster_context="kind-forgeops",
        namespace="default",
        health_timeout_seconds=120,
        reason="a settler test",
    )
    await service.attach_change_set(
        session, deployment_id=record.id, change_set_id=submission.change_set_id, status="applying"
    )
    return record, submission


async def _row(session: AsyncSession, deployment_id: uuid.UUID) -> Any:
    return (
        (
            await session.execute(
                text("SELECT status, healthy, stable, report FROM deployments WHERE id = :id"),
                {"id": deployment_id},
            )
        )
        .mappings()
        .first()
    )


def _chokepoint_with_settler(redis_client: Any, sink: Sink) -> tuple[Any, DeploymentService, Any]:
    deployments = DeploymentService()
    notifications = NotificationService(channels={})
    chokepoint = build_chokepoint(
        policy=ScriptedPolicy(decision=allow()),
        sink=sink,
        redis_client=redis_client,
        change_set_settler=DeploymentSettler(deployments=deployments, notifications=notifications),
    )
    return chokepoint, deployments, notifications


async def test_a_converged_report_makes_the_deployment_healthy_and_stable(sessions: Any, redis_client: Any) -> None:
    sink = Sink()
    chokepoint, deployments, notifications = _chokepoint_with_settler(redis_client, sink)
    async with sessions() as session:
        fixture = await make_fixture(session)
        record, submission = await _deploy(session, chokepoint, fixture, deployments)
        await chokepoint.record_command_result(
            session,
            change_set_id=submission.change_set_id,
            status="succeeded",
            backup_manifest={
                "applied": ["deployment.apps/api created", "service/api created"],
                "workloads": [{"kind": "deployment", "name": "api", "ready": True}],
                "healthy": True,
            },
        )
        row = await _row(session, record.id)
        raised = await notifications.list_for_project(session, project_id=fixture.project_id)

    # READ FROM THE DATABASE. This is the assertion that would have failed before the settler existed.
    assert row["status"] == "applied"
    assert row["healthy"] is True
    assert row["stable"] is True
    assert row["report"]["applied"] == ["deployment.apps/api created", "service/api created"]
    # And somebody was told.
    assert [item.kind for item in raised] == ["deploy_completed"]
    assert "converged" in raised[0].subject


async def test_an_unready_workload_makes_it_degraded_and_not_stable(sessions: Any, redis_client: Any) -> None:
    """The distinction the whole design rests on: applied is not running."""
    sink = Sink()
    chokepoint, deployments, notifications = _chokepoint_with_settler(redis_client, sink)
    async with sessions() as session:
        fixture = await make_fixture(session)
        record, submission = await _deploy(session, chokepoint, fixture, deployments)
        await chokepoint.record_command_result(
            session,
            change_set_id=submission.change_set_id,
            status="succeeded",
            backup_manifest={
                "applied": ["deployment.apps/api created"],
                "workloads": [{"kind": "deployment", "name": "api", "ready": False, "detail": "timed out"}],
            },
        )
        row = await _row(session, record.id)
        raised = await notifications.list_for_project(session, project_id=fixture.project_id)

    assert row["healthy"] is False
    # NOT STABLE, so a rollback will never be offered this deployment.
    assert row["stable"] is False
    assert [item.kind for item in raised] == ["deploy_failed"]
    # The message names the workload, which is what an operator acts on.
    assert "api" in raised[0].body


async def test_a_report_with_no_workloads_leaves_health_unknown(sessions: Any, redis_client: Any) -> None:
    """Absence of health is not health. Inferring `true` from a successful apply is the conflation
    `degraded` exists to prevent, so a report that says nothing about workloads leaves `healthy` null."""
    sink = Sink()
    chokepoint, deployments, _ = _chokepoint_with_settler(redis_client, sink)
    async with sessions() as session:
        fixture = await make_fixture(session)
        record, submission = await _deploy(session, chokepoint, fixture, deployments)
        await chokepoint.record_command_result(
            session,
            change_set_id=submission.change_set_id,
            status="succeeded",
            backup_manifest={"applied": ["configmap/app created"]},
        )
        row = await _row(session, record.id)

    assert row["healthy"] is None
    assert row["stable"] is False


async def test_a_failed_command_fails_the_deployment_and_notifies(sessions: Any, redis_client: Any) -> None:
    sink = Sink()
    chokepoint, deployments, notifications = _chokepoint_with_settler(redis_client, sink)
    async with sessions() as session:
        fixture = await make_fixture(session)
        record, submission = await _deploy(session, chokepoint, fixture, deployments)
        await chokepoint.record_command_result(session, change_set_id=submission.change_set_id, status="failed")
        row = await _row(session, record.id)
        raised = await notifications.list_for_project(session, project_id=fixture.project_id)

    assert row["status"] == "failed"
    assert row["stable"] is False
    assert [item.kind for item in raised] == ["deploy_failed"]


async def test_a_change_set_that_is_not_a_deployment_settles_without_complaint(
    sessions: Any, redis_client: Any
) -> None:
    """An apply, a clone and a dashboard action all reach the settler. Nothing to settle is ordinary."""
    sink = Sink()
    chokepoint, _, notifications = _chokepoint_with_settler(redis_client, sink)
    async with sessions() as session:
        fixture = await make_fixture(session)
        submission = await chokepoint.transit_host_action(
            session,
            project_id=fixture.project_id,
            principal=fixture.principal,
            operation="docker.container_action",
            target="api",
            args={"action": "restart", "container": "api"},
            environment_name=None,
            environment_requires_approval=False,
            reason="a restart",
        )
        outcome = await chokepoint.record_command_result(
            session, change_set_id=submission.change_set_id, status="succeeded"
        )
        raised = await notifications.list_for_project(session, project_id=fixture.project_id)

    assert outcome == "applied"
    # No deployment notification for something that was not a deployment.
    assert raised == []
