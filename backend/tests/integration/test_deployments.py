# SPDX-License-Identifier: FSL-1.1-ALv2
"""§2.2's deployment transit and its stable-state snapshot, against the real chokepoint and database.

THE THREE PROPERTIES THAT MATTER, and why each is here rather than assumed:

1. **An environment that requires approval gets one, whatever the policy says.** Three mechanisms can
   demand a human — the environment row, the approval gate's verdict, and the Rego result — and they may
   only ADD caution. The test that matters is the one where the policy would auto-approve and the
   environment still stops it: the Rego bundle is data that can be replaced at runtime, and an operator
   who marked production as needing a human must not lose that to a bundle change.
2. **`stable` is set on HEALTH, not on apply.** This is the whole basis of rollback. A deployment whose
   manifests reached the cluster and whose workloads never converged is not a state to return to, and a
   rollback offered to one would restore a broken deployment. Asserted against the database, including the
   CHECK constraint, because the service is one writer and a support UPDATE is another.
3. **This is the first caller that supplies `environment` to the policy.** `_evaluate_policy` has taken
   the argument since Phase 1 and every existing caller left it absent — which is why `approval.rego`'s
   "unknown environment" branch was the only one ever taken. The test reads what the policy was actually
   asked, because a parameter that is passed and ignored is this codebase's most repeated defect.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.errors import ProblemException
from src.deployments.service import DeploymentService, manifest_digest
from src.environments.service import EnvironmentService

from tests.integration.chokepoint_support import (
    RecordingSink,
    ScriptedPolicy,
    allow,
    build_chokepoint,
    make_fixture,
    require_approval,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

MANIFESTS = ["k8s/deployment.yaml", "k8s/service.yaml"]


async def _environment(session: AsyncSession, project_id: uuid.UUID, *, kind: str, gate: bool | None) -> Any:
    service = EnvironmentService(pepper="0123456789abcdef0123456789abcdef")
    return await service.create(
        session,
        project_id=project_id,
        tenant_id=None,
        name=f"{kind}-{uuid.uuid4().hex[:6]}",
        kind=kind,
        k8s_context="kind-forgeops",
        requires_approval=gate,
    )


async def _deploy(
    chokepoint: Any,
    session: AsyncSession,
    fixture: Any,
    environment: Any,
    *,
    deployment_id: uuid.UUID | None = None,
) -> Any:
    return await chokepoint.deploy_manifests(
        session,
        project_id=fixture.project_id,
        principal=fixture.principal,
        deployment_id=deployment_id or uuid.uuid4(),
        environment_name=environment.name,
        environment_requires_approval=environment.requires_approval,
        manifests=MANIFESTS,
        cluster_context=environment.k8s_context,
        namespace="default",
        health_timeout_seconds=120,
        reason="a test deployment",
    )


class TestTheEnvironmentsApprovalRequirementIsEnforced:
    async def test_a_gated_environment_stops_a_deployment_the_policy_would_allow(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """THE TEST THAT MATTERS. The policy says allow; the environment says no; the human wins.

        The Rego bundle is runtime data. An operator who marked an environment as needing approval must
        not be able to lose that because somebody replaced a bundle.
        """
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="production", gate=None)

            submission = await _deploy(chokepoint, session, fixture, environment)

        assert submission.status == "pending_approval"
        assert sink.sent == [], "a gated environment must not reach the agent before a human approves"

    async def test_an_ungated_environment_with_an_allowing_policy_is_delivered(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """The other half: the opt-out genuinely works, or the gate above would just be a block."""
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="custom", gate=False)

            submission = await _deploy(chokepoint, session, fixture, environment)

        assert submission.status == "applying"
        assert len(sink.sent) == 1
        _, command = sink.sent[0]
        assert command.envelope["operation"] == "deployment.apply_manifests"
        assert command.envelope["args"]["manifests"] == MANIFESTS
        assert command.envelope["args"]["context"] == "kind-forgeops"

    async def test_a_requiring_policy_stops_an_ungated_environment(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """Caution only ever adds. An environment cannot waive a policy's demand for a human."""
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=sink, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="custom", gate=False)

            submission = await _deploy(chokepoint, session, fixture, environment)

        assert submission.status == "pending_approval"
        assert sink.sent == []

    async def test_the_policy_is_actually_told_which_environment(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """A parameter that is passed and ignored is this codebase's most repeated defect.

        `_evaluate_policy` has accepted `environment` since Phase 1 with no caller ever supplying one, so
        this reads the payload the policy was handed rather than trusting that the argument arrived.
        """
        policy = ScriptedPolicy(decision=allow())
        chokepoint = build_chokepoint(policy=policy, sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="custom", gate=False)

            await _deploy(chokepoint, session, fixture, environment)

        assert policy.calls, "the policy was never consulted"
        payload = policy.calls[-1]
        flattened = str(payload)
        assert environment.name in flattened, payload
        assert "deployment.apply_manifests" in flattened, payload


class TestTheDeploymentRowRecordsWhatHappened:
    async def test_a_blocked_or_pending_deployment_still_has_a_record(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """Written before the decision is asked for, so a refusal is never invisible."""
        service = DeploymentService()
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="production", gate=None)

            record = await service.create(
                session,
                project_id=fixture.project_id,
                environment_id=environment.id,
                tenant_id=None,
                manifests=MANIFESTS,
                cluster_context=environment.k8s_context,
                namespace="default",
                requested_by=None,
            )
            submission = await _deploy(chokepoint, session, fixture, environment, deployment_id=record.id)
            await service.attach_change_set(
                session,
                deployment_id=record.id,
                change_set_id=submission.change_set_id,
                status="pending_approval",
            )
            reread = await service.read(session, deployment_id=record.id)

        assert reread.status == "pending_approval"
        assert reread.change_set_id == submission.change_set_id
        # Nothing has verified anything yet, so `healthy` is NULL rather than False. A client must be able
        # to tell "not checked" from "checked and not ready".
        assert reread.healthy is None
        assert reread.stable is False

    async def test_a_healthy_report_marks_the_row_stable(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="custom", gate=False)
            record = await service.create(
                session,
                project_id=fixture.project_id,
                environment_id=environment.id,
                tenant_id=None,
                manifests=MANIFESTS,
                cluster_context="kind-forgeops",
                namespace="default",
                requested_by=None,
            )

            done = await service.complete(
                session,
                deployment_id=record.id,
                healthy=True,
                report={"workloads": [{"kind": "deployment", "name": "app", "ready": True}]},
            )
            target = await service.rollback_target(session, environment_id=environment.id)

        assert (done.status, done.healthy, done.stable) == ("applied", True, True)
        assert target is not None and target.id == done.id

    async def test_a_degraded_report_is_not_a_rollback_target(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """THE INVARIANT ROLLBACK RESTS ON.

        `degraded` means the manifests are in the cluster and a workload did not converge. It is not a
        failure — nothing needs retrying — and it is not a state to return to. A rollback offered to one
        would restore a broken deployment while reporting success.
        """
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="custom", gate=False)
            first = await service.create(
                session,
                project_id=fixture.project_id,
                environment_id=environment.id,
                tenant_id=None,
                manifests=MANIFESTS,
                cluster_context="kind-forgeops",
                namespace="default",
                requested_by=None,
            )
            await service.complete(session, deployment_id=first.id, healthy=True, report={})
            second = await service.create(
                session,
                project_id=fixture.project_id,
                environment_id=environment.id,
                tenant_id=None,
                manifests=MANIFESTS,
                cluster_context="kind-forgeops",
                namespace="default",
                requested_by=None,
            )
            degraded = await service.complete(
                session,
                deployment_id=second.id,
                healthy=False,
                report={"workloads": [{"kind": "deployment", "name": "app", "ready": False}]},
            )
            target = await service.rollback_target(session, environment_id=environment.id)

        assert degraded.status == "degraded"
        assert degraded.stable is False
        # The rollback target is the earlier HEALTHY one, not the newest.
        assert target is not None and target.id == first.id

    async def test_the_database_refuses_a_stable_row_that_is_not_healthy(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """Enforced below the service, because the service is one writer and a support UPDATE is another."""
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="custom", gate=False)
            record = await service.create(
                session,
                project_id=fixture.project_id,
                environment_id=environment.id,
                tenant_id=None,
                manifests=MANIFESTS,
                cluster_context="kind-forgeops",
                namespace="default",
                requested_by=None,
            )
            await service.complete(session, deployment_id=record.id, healthy=False, report={})

            with pytest.raises(Exception):  # noqa: B017, PT011 - the driver's IntegrityError
                await session.execute(text("UPDATE deployments SET stable = true WHERE id = :id"), {"id": record.id})
                await session.flush()

    async def test_a_second_report_is_refused_rather_than_applied(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """Delivery is at-least-once. A redelivered `degraded` must not un-stable a settled deployment."""
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="custom", gate=False)
            record = await service.create(
                session,
                project_id=fixture.project_id,
                environment_id=environment.id,
                tenant_id=None,
                manifests=MANIFESTS,
                cluster_context="kind-forgeops",
                namespace="default",
                requested_by=None,
            )
            await service.complete(session, deployment_id=record.id, healthy=True, report={})

            with pytest.raises(ProblemException) as caught:
                await service.complete(session, deployment_id=record.id, healthy=False, report={})

            detail = str(caught.value.problem.detail or "")
            reread = await service.read(session, deployment_id=record.id)

        assert "terminal" in detail, detail
        assert (reread.status, reread.stable) == ("applied", True)


class TestTheManifestDigestAnswersTheDiffQuestion:
    def test_order_does_not_change_the_digest(self) -> None:
        """Two deployments of the same set in different orders must compare equal.

        §2.3 diffs two deployments. If order changed the digest, the timeline would report a change where
        nothing changed.
        """
        assert manifest_digest(["b.yaml", "a.yaml"]) == manifest_digest(["a.yaml", "b.yaml"])

    def test_a_different_set_changes_it(self) -> None:
        assert manifest_digest(["a.yaml"]) != manifest_digest(["a.yaml", "b.yaml"])


class TestARequestThatCannotBeDeliveredIsRefusedEarly:
    async def test_a_manifest_set_past_the_agents_bound(self, sessions: Any) -> None:
        """Refused by the backend because the AGENT would refuse it.

        A request the backend accepts and the agent rejects becomes a change set that can never be
        delivered, and the failure surfaces three layers from the request that caused it.
        """
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="custom", gate=False)

            with pytest.raises(ProblemException) as caught:
                await service.create(
                    session,
                    project_id=fixture.project_id,
                    environment_id=environment.id,
                    tenant_id=None,
                    manifests=[f"k8s/m{index}.yaml" for index in range(33)],
                    cluster_context=None,
                    namespace=None,
                    requested_by=None,
                )

        assert "32" in str(caught.value.problem.detail or "")

    @pytest.mark.parametrize("path", ["/etc/passwd", "../../secrets.yaml", "C:\\windows\\hosts"])
    async def test_a_path_outside_the_workspace(self, sessions: Any, path: str) -> None:
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="custom", gate=False)

            with pytest.raises(ProblemException):
                await service.create(
                    session,
                    project_id=fixture.project_id,
                    environment_id=environment.id,
                    tenant_id=None,
                    manifests=[path],
                    cluster_context=None,
                    namespace=None,
                    requested_by=None,
                )

    async def test_an_empty_manifest_set(self, sessions: Any) -> None:
        """A deployment of nothing must not be recorded as a deployment."""
        service = DeploymentService()
        async with sessions() as session:
            fixture = await make_fixture(session)
            environment = await _environment(session, fixture.project_id, kind="custom", gate=False)

            with pytest.raises(ProblemException):
                await service.create(
                    session,
                    project_id=fixture.project_id,
                    environment_id=environment.id,
                    tenant_id=None,
                    manifests=["   "],
                    cluster_context=None,
                    namespace=None,
                    requested_by=None,
                )
