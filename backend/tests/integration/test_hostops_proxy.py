# SPDX-License-Identifier: FSL-1.1-ALv2
"""§2.4 and §2.9's host operations, against the real chokepoint and the real database.

WHAT IS ASSERTED HERE, and why each is a property somebody would otherwise get wrong:

1. **A mutating dashboard action leaves a change-set row with its own operation and a blast-radius
   verdict.** That row is the evidence it went THROUGH the chokepoint rather than around it. A verdict can
   only come from the analyser, so its presence distinguishes a real transit from a route that wrote a row
   and sent a command.
2. **A read leaves no change-set row and mints no approval.** A row per panel refresh would bury the
   governance log an operator reads after an incident, and a read carrying an approval would be an
   approval nobody granted — which the agent refuses anyway.
3. **An environment that requires approval stops a workload action**, even when the policy would
   auto-approve it, and a policy that requires one stops it even when the environment waives it. The two
   mechanisms may only ADD caution.
4. **An operation outside the closed set raises rather than being minted** — including the two READS,
   which must never travel the mutating transit.
5. **A denied read raises rather than returning an empty inventory.** An empty answer renders as "this host
   has nothing", which is the defect class this phase was warned about, with a human reading it.

The agent's own behaviour against a real daemon and a real cluster is not simulated here: it is exercised
directly in `agent/internal/executor/docker_real_test.go` and `kubernetes_cluster_test.go`, which run
against a real Docker daemon and a real kind cluster in CI.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.errors import ProblemException
from src.governance.chokepoint import (
    DOCKER_CONTAINER_OPERATION,
    DOCKER_INVENTORY_OPERATION,
    KUBERNETES_INVENTORY_OPERATION,
    KUBERNETES_WORKLOAD_OPERATION,
)

from tests.integration.chokepoint_support import (
    ScriptedPolicy,
    allow,
    build_chokepoint,
    deny,
    make_fixture,
    require_approval,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


class _Pending:
    """One delivered command's result. Satisfies `PendingCommand` structurally."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def result(self, timeout: float | None = None) -> dict[str, Any]:
        return self._payload


class AnsweringSink:
    """A `CommandSink` that keeps what it was handed AND can answer a read.

    The TRANSPORT, not the agent. What it must not stand in for is Docker or Kubernetes, and it does not:
    every payload here is supplied by the test, and the real tools are driven in the agent's own suite
    against a real daemon and a real cluster.
    """

    def __init__(self, payload: dict[str, Any] | None = None, *, correlates: bool = True) -> None:
        self.sent: list[tuple[uuid.UUID, Any]] = []
        self._payload = payload or {}
        self._correlates = correlates

    async def send_command(self, *, device_id: uuid.UUID, command: Any) -> Any:
        self.sent.append((device_id, command))
        # `None` models a sink that delivers but cannot correlate a result — which a mutation tolerates
        # and a read cannot. Returning it is how the read's refusal gets tested.
        return _Pending(self._payload) if self._correlates else None


async def _rows(session: AsyncSession, project_id: uuid.UUID) -> list[Any]:
    result = await session.execute(
        text(
            "SELECT operation, status, blast_radius_verdict, blast_radius_score, operation_args "
            "FROM change_sets WHERE project_id = :project ORDER BY created_at"
        ),
        {"project": project_id},
    )
    return list(result)


class TestTheWritePathTravelsTheChokepoint:
    async def test_a_container_action_leaves_a_change_set_carrying_its_operation(
        self, sessions: Any, redis_client: Any
    ) -> None:
        sink = AnsweringSink()
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            submission = await chokepoint.transit_host_action(
                session,
                project_id=fixture.project_id,
                principal=fixture.principal,
                operation=DOCKER_CONTAINER_OPERATION,
                target="api",
                args={"action": "restart", "container": "api"},
                environment_name=None,
                environment_requires_approval=False,
                reason="it stopped serving",
            )
            rows = await _rows(session, fixture.project_id)

        assert submission.outcome == "applying"
        assert len(rows) == 1
        assert rows[0][0] == DOCKER_CONTAINER_OPERATION
        # A VERDICT AND A SCORE, which only the analyser produces. Their presence is the evidence that
        # the blast-radius stage ran rather than being skipped for a "small" action.
        assert rows[0][2] in {"allow", "warn", "block"}
        assert rows[0][3] is not None
        assert rows[0][4]["container"] == "api"
        assert len(sink.sent) == 1
        assert sink.sent[0][1].envelope["operation"] == DOCKER_CONTAINER_OPERATION
        # A mutation always carries an approval.
        assert sink.sent[0][1].envelope["approval_id"] != ""

    async def test_an_action_naming_no_target_is_refused(self, sessions: Any, redis_client: Any) -> None:
        """The catastrophic default: an action with no target must not become one on every target."""
        sink = AnsweringSink()
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            for target in ("", "   "):
                with pytest.raises(ValueError, match="no target"):
                    await chokepoint.transit_host_action(
                        session,
                        project_id=fixture.project_id,
                        principal=fixture.principal,
                        operation=DOCKER_CONTAINER_OPERATION,
                        target=target,
                        args={"action": "remove", "container": target},
                        environment_name=None,
                        environment_requires_approval=False,
                        reason="",
                    )
        assert sink.sent == []

    @pytest.mark.parametrize(
        "operation",
        ["docker.inventory", "kubernetes.inventory", "changeset.apply", "shell.exec", ""],
    )
    async def test_an_operation_outside_the_host_action_set_is_refused(
        self, sessions: Any, redis_client: Any, operation: str
    ) -> None:
        """Including the two READS, which must never travel the mutating transit.

        `changeset.apply` is listed for the same reason: it has its own transit with its own change-item
        machinery, and routing it through this one would write a change set with no items.
        """
        sink = AnsweringSink()
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ValueError, match="host action"):
                await chokepoint.transit_host_action(
                    session,
                    project_id=fixture.project_id,
                    principal=fixture.principal,
                    operation=operation,
                    target="api",
                    args={},
                    environment_name=None,
                    environment_requires_approval=False,
                    reason="",
                )
            rows = await _rows(session, fixture.project_id)
        assert sink.sent == []
        assert rows == []


class TestTheApprovalRequirementsCanOnlyAddCaution:
    async def test_a_gated_environment_stops_an_action_the_policy_would_allow(
        self, sessions: Any, redis_client: Any
    ) -> None:
        sink = AnsweringSink()
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            submission = await chokepoint.transit_host_action(
                session,
                project_id=fixture.project_id,
                principal=fixture.principal,
                operation=KUBERNETES_WORKLOAD_OPERATION,
                target="deployment/api",
                args={
                    "action": "scale",
                    "kind": "deployment",
                    "name": "api",
                    "namespace": "default",
                    "replicas": 3,
                },
                environment_name="production",
                environment_requires_approval=True,
                reason="traffic doubled",
            )
            rows = await _rows(session, fixture.project_id)

        assert submission.status == "pending_approval"
        assert submission.outcome == "approval-required"
        # NOTHING WAS SENT. A pending action already delivered would make the approval decorative.
        assert sink.sent == []
        assert rows[0][1] == "pending_approval"

    async def test_a_policy_requiring_approval_stops_an_ungated_environment(
        self, sessions: Any, redis_client: Any
    ) -> None:
        sink = AnsweringSink()
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval("this cluster is production-adjacent")),
            sink=sink,
            redis_client=redis_client,
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            submission = await chokepoint.transit_host_action(
                session,
                project_id=fixture.project_id,
                principal=fixture.principal,
                operation=KUBERNETES_WORKLOAD_OPERATION,
                target="deployment/api",
                args={"action": "restart", "kind": "deployment", "name": "api", "namespace": "default"},
                environment_name="staging",
                environment_requires_approval=False,
                reason="a rolling restart",
            )
        assert submission.status == "pending_approval"
        assert sink.sent == []


class TestTheReadPath:
    async def test_a_read_leaves_no_change_set_and_carries_no_approval(self, sessions: Any, redis_client: Any) -> None:
        payload = {
            "containers": [{"name": "api", "state": "running", "cpu_percent": None}],
            "docker_version": "27.1.1",
            "observed_at": "2026-09-21T10:00:00Z",
            "stats_sampled": False,
        }
        sink = AnsweringSink(payload)
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            answer = await chokepoint.read_inventory(
                session,
                project_id=fixture.project_id,
                principal=fixture.principal,
                operation=DOCKER_INVENTORY_OPERATION,
                args={"stats": False},
            )
            rows = await _rows(session, fixture.project_id)

        # UNALTERED. The null is the contract that lets a panel distinguish "not measured" from "idle",
        # and the timestamp is the agent's because only the agent knows when it measured.
        assert answer["containers"][0]["cpu_percent"] is None
        assert answer["observed_at"] == "2026-09-21T10:00:00Z"
        assert answer["stats_sampled"] is False
        assert rows == []
        assert len(sink.sent) == 1
        # EMPTY, not fabricated. The agent's dispatch row for a read requires no approval and refuses one.
        assert sink.sent[0][1].envelope["approval_id"] == ""
        assert sink.sent[0][1].envelope["operation"] == DOCKER_INVENTORY_OPERATION

    async def test_a_denied_read_raises_rather_than_returning_an_empty_inventory(
        self, sessions: Any, redis_client: Any
    ) -> None:
        """The failure mode: "you may not look" rendered as "this host has nothing"."""
        sink = AnsweringSink({"containers": []})
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=deny("the bundle forbids reading this host")),
            sink=sink,
            redis_client=redis_client,
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.read_inventory(
                    session,
                    project_id=fixture.project_id,
                    principal=fixture.principal,
                    operation=KUBERNETES_INVENTORY_OPERATION,
                    args={},
                )
        assert "forbids" in (raised.value.problem.detail or "")
        assert sink.sent == []

    async def test_a_read_refuses_when_the_sink_cannot_correlate_a_result(
        self, sessions: Any, redis_client: Any
    ) -> None:
        """A read needs the agent's answer; a mutation does not. The difference is stated, not assumed."""
        sink = AnsweringSink(correlates=False)
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ProblemException) as raised:
                await chokepoint.read_inventory(
                    session,
                    project_id=fixture.project_id,
                    principal=fixture.principal,
                    operation=DOCKER_INVENTORY_OPERATION,
                    args={},
                )
        assert "read" in (raised.value.problem.detail or "").lower()
        # It was still SENT: the refusal is about the answer, and saying otherwise would be wrong.
        assert len(sink.sent) == 1

    async def test_a_read_cannot_be_minted_with_an_approval(self, sessions: Any, redis_client: Any) -> None:
        """The guard inside the single signer.

        Worth its own test because it is the one place where "this envelope claims an approval" and "this
        operation needs none" could disagree, and the agent would then refuse a command the backend
        believed it had authorised.
        """
        sink = AnsweringSink({})
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            admitted = await chokepoint._admit(  # noqa: SLF001 - the guard under test is private
                session, project_id=fixture.project_id, principal=fixture.principal
            )
            with pytest.raises(ValueError, match="must not carry an approval"):
                await chokepoint._mint_and_sign(  # noqa: SLF001
                    session,
                    change_set_id=None,
                    admitted=admitted,
                    approval_id=uuid.uuid4(),
                    audit_seq=1,
                    decision=allow(),
                    report=None,
                    operation=DOCKER_INVENTORY_OPERATION,
                    args={},
                )
