# SPDX-License-Identifier: FSL-1.1-ALv2
"""Promotion, rollback and the timeline, against the real chokepoint and database. §2.1, §2.3.

The properties that matter: a promotion carries the SOURCE's stable manifests to the TARGET and the
target's approval requirement governs; nothing unstable can be a promotion source or a rollback target,
because `stable` is set on health and restoring an unverified state under the word "rollback" is the
failure this prevents; and the diff compares manifest sets by digest, which is order-independent.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.deployments.service import DeploymentService
from src.environments.service import EnvironmentService

from tests.integration.chokepoint_support import (
    ScriptedPolicy,
    allow,
    build_chokepoint,
    make_fixture,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

MANIFESTS = ["k8s/deployment.yaml", "k8s/service.yaml"]


class AnsweringSink:
    def __init__(self) -> None:
        self.sent: list[tuple[uuid.UUID, Any]] = []

    async def send_command(self, *, device_id: uuid.UUID, command: Any) -> Any:
        self.sent.append((device_id, command))
        return None


async def _environment(session: AsyncSession, project_id: uuid.UUID, *, name: str, kind: str, gate: bool | None) -> Any:
    service = EnvironmentService(pepper="0123456789abcdef0123456789abcdef")
    return await service.create(
        session,
        project_id=project_id,
        tenant_id=None,
        name=name,
        kind=kind,
        k8s_context="kind-forgeops",
        requires_approval=gate,
    )


async def _healthy_deployment(
    session: AsyncSession, service: DeploymentService, project_id: uuid.UUID, environment: Any
) -> Any:
    """A deployment that converged, so it is stable and can be promoted or rolled back to."""
    record = await service.create(
        session,
        project_id=project_id,
        environment_id=environment.id,
        tenant_id=None,
        manifests=MANIFESTS,
        cluster_context=environment.k8s_context,
        namespace="default",
        requested_by=None,
    )
    await service.complete(
        session,
        deployment_id=record.id,
        healthy=True,
        report={"applied": MANIFESTS, "workloads": [{"kind": "deployment", "name": "api", "ready": True}]},
    )
    return await service.read(session, deployment_id=record.id)


class TestPromotion:
    async def test_the_targets_approval_requirement_governs_not_the_sources(
        self, sessions: Any, redis_client: Any
    ) -> None:
        """Staging waives approval; production demands it; a promotion into production asks."""
        sink = AnsweringSink()
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            staging = await _environment(session, fixture.project_id, name="staging", kind="staging", gate=False)
            production = await _environment(
                session, fixture.project_id, name="production", kind="production", gate=None
            )
            stable = await _healthy_deployment(session, service, fixture.project_id, staging)
            assert stable.stable is True

            # The promotion, performed the way the route performs it: the TARGET environment is what
            # reaches the transit.
            promoted = await service.create(
                session,
                project_id=fixture.project_id,
                environment_id=production.id,
                tenant_id=None,
                manifests=list(stable.manifests),
                cluster_context=production.k8s_context,
                namespace=None,
                requested_by=None,
            )
            submission = await chokepoint.deploy_manifests(
                session,
                project_id=fixture.project_id,
                principal=fixture.principal,
                deployment_id=promoted.id,
                environment_name=production.name,
                environment_requires_approval=production.requires_approval,
                manifests=list(stable.manifests),
                cluster_context=production.k8s_context,
                namespace=None,
                health_timeout_seconds=120,
                reason="promotion from staging to production",
            )

        assert submission.status == "pending_approval"
        # NOTHING REACHED THE AGENT. A promotion already delivered would make the approval decorative.
        assert sink.sent == []

    async def test_the_last_environment_refuses_with_a_reason(self, sessions: Any, redis_client: Any) -> None:
        service = EnvironmentService(pepper="0123456789abcdef0123456789abcdef")
        async with sessions() as session:
            fixture = await make_fixture(session)
            await _environment(session, fixture.project_id, name="staging", kind="staging", gate=False)
            production = await _environment(
                session, fixture.project_id, name="production", kind="production", gate=None
            )
            decision = await service.promote_from(session, environment_id=production.id)

        assert decision.allowed is False
        assert decision.target is None
        # A refusal with a reason rather than an empty success: an operator clicking promote on production
        # needs to be told why nothing happened.
        assert "last environment" in decision.reason

    async def test_an_environment_with_nothing_stable_cannot_be_promoted(
        self, sessions: Any, redis_client: Any
    ) -> None:
        """A degraded deployment is not a state to carry forward under the word "promote"."""
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            staging = await _environment(session, fixture.project_id, name="staging", kind="staging", gate=False)
            record = await service.create(
                session,
                project_id=fixture.project_id,
                environment_id=staging.id,
                tenant_id=None,
                manifests=MANIFESTS,
                cluster_context="kind-forgeops",
                namespace="default",
                requested_by=None,
            )
            # Applied, and the workloads never converged.
            await service.complete(session, deployment_id=record.id, healthy=False, report={"applied": MANIFESTS})
            target = await service.rollback_target(session, environment_id=staging.id)

        # No stable deployment, so the route refuses. Asserted at the service, which is what the route
        # consults.
        assert target is None


class TestRollback:
    async def test_the_target_is_the_newest_stable_not_the_newest(self, sessions: Any, redis_client: Any) -> None:
        """The invariant the whole of §2.3 rests on."""
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            production = await _environment(session, fixture.project_id, name="production", kind="custom", gate=False)
            healthy = await _healthy_deployment(session, service, fixture.project_id, production)

            degraded = await service.create(
                session,
                project_id=fixture.project_id,
                environment_id=production.id,
                tenant_id=None,
                manifests=["k8s/deployment.yaml"],
                cluster_context="kind-forgeops",
                namespace="default",
                requested_by=None,
            )
            await service.complete(session, deployment_id=degraded.id, healthy=False, report={})

            target = await service.rollback_target(session, environment_id=production.id)

        assert target is not None
        # The EARLIER healthy one, not the newer degraded one. Offering the degraded deployment would
        # restore a broken state while reporting success.
        assert target.id == healthy.id
        assert target.stable is True

    async def test_a_deployment_from_another_environment_is_not_a_rollback_target(
        self, sessions: Any, redis_client: Any
    ) -> None:
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            staging = await _environment(session, fixture.project_id, name="staging", kind="staging", gate=False)
            production = await _environment(session, fixture.project_id, name="production", kind="custom", gate=False)
            staging_stable = await _healthy_deployment(session, service, fixture.project_id, staging)
            production_target = await service.rollback_target(session, environment_id=production.id)

        # Production has nothing of its own, and staging's stable deployment is NOT offered as its
        # rollback target — the route checks `environment_id` for exactly this.
        assert production_target is None
        assert staging_stable.environment_id == staging.id


class TestTheTimelineAndDiff:
    async def test_the_timeline_keeps_health_nullable(self, sessions: Any, redis_client: Any) -> None:
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, name="dev", kind="development", gate=False)
            # One in flight: nothing has verified it, so `healthy` must be null rather than false.
            in_flight = await service.create(
                session,
                project_id=fixture.project_id,
                environment_id=environment.id,
                tenant_id=None,
                manifests=MANIFESTS,
                cluster_context="kind-forgeops",
                namespace="default",
                requested_by=None,
            )
            history = await service.history(session, project_id=fixture.project_id)

        assert len(history) == 1
        assert history[0].id == in_flight.id
        # NULL, NOT FALSE. A timeline that collapsed these would mark an in-flight rollout as failed.
        assert history[0].healthy is None
        assert history[0].stable is False

    async def test_the_digest_is_order_independent(self, sessions: Any, redis_client: Any) -> None:
        """So two deployments of the same set in a different order are not reported as a change."""
        from src.deployments.service import manifest_digest

        assert manifest_digest(["b.yaml", "a.yaml"]) == manifest_digest(["a.yaml", "b.yaml"])
        assert manifest_digest(["a.yaml"]) != manifest_digest(["a.yaml", "b.yaml"])
