# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for Autonomous Deployment Worker, Fencing Token Claim, and State Machine (Phase 4).

Tests:
1. Initial atomic claim increments fence_token (0 -> 1) and sets active lease.
2. Concurrent claim rejection while lease is active.
3. Claim takeover after lease expiration (fence_token increments 1 -> 2).
4. Terminal and cancelling runs cannot be claimed.
5. Routine heartbeat extends lease without incrementing fence_token.
6. Heartbeat rejection when token is stale or lease expired.
7. Stale worker write fails immediately with WorkerFencingLostError.
8. Cooperative cancellation check returns True when run is cancelling, False when running.
9. Cooperative cancellation raises WorkerFencingLostError on stale token or expired lease.
10. Stage state transitions update stage timestamps, metadata, and emit transactional outbox events.
11. Monotonic progress calculation across all 4 strategies (100% only achievable on G7 success).
"""

from __future__ import annotations

import operator
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import Update
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList, UnaryExpression

from src.deployments.autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentLog,
    AutonomousDeploymentOutbox,
    AutonomousDeploymentStage,
)
from src.deployments.autonomous_schemas import (
    CreateAutonomousRunRequest,
    DeploymentStrategy,
    GitHubConfigRequest,
    VercelConfigRequest,
)
from src.deployments.autonomous_service import (
    STAGE_G1_BLUEPRINT,
    STAGE_G2_ARTIFACT,
    STAGE_G3_CONSISTENCY,
    STAGE_G4_BUILD,
    STAGE_G5_APPLY,
    STAGE_G6_WORKLOAD,
    STAGE_G7_VERIFICATION,
    STAGE_GITHUB_RELEASE,
    STAGE_VERCEL_DEPLOY,
    AutonomousDeploymentService,
    build_stage_graph,
)
from src.deployments.autonomous_worker import (
    AutonomousWorker,
    WorkerFencingLostError,
    calculate_progress,
)


def _eval_clause(clause: Any, run: AutonomousDeployment) -> bool:
    """Evaluates an arbitrary SQLAlchemy whereclause AST against an in-memory AutonomousDeployment."""
    if clause is None:
        return True

    if hasattr(clause, "element") and not isinstance(clause, UnaryExpression):
        return _eval_clause(clause.element, run)

    if isinstance(clause, BooleanClauseList):
        if clause.operator is operator.or_:
            return any(_eval_clause(c, run) for c in clause.clauses)
        return all(_eval_clause(c, run) for c in clause.clauses)

    if isinstance(clause, BinaryExpression):
        left_name = getattr(clause.left, "name", None)
        run_val = getattr(run, left_name, None) if left_name else None
        right = clause.right
        val = getattr(right, "value", right)
        op_name = getattr(clause.operator, "__name__", "")

        if "is_not" in op_name:
            return run_val is not None
        if "is_" in op_name or op_name == "is":
            return run_val is None
        if "not_in" in op_name or "notin" in op_name:
            val_items = getattr(right, "value", right)
            return run_val not in val_items
        if "in_op" in op_name or "in_" in op_name:
            val_items = getattr(right, "value", right)
            return run_val in val_items

        if isinstance(run_val, datetime) and isinstance(val, datetime):
            if run_val.tzinfo is None and val.tzinfo is not None:
                run_val = run_val.replace(tzinfo=timezone.utc)
            elif run_val.tzinfo is not None and val.tzinfo is None:
                val = val.replace(tzinfo=timezone.utc)

        try:
            return bool(clause.operator(run_val, val))
        except Exception:
            return False

    if isinstance(clause, UnaryExpression):
        s = str(clause)
        if "IS NULL" in s:
            col_name = getattr(clause.element, "name", None)
            return getattr(run, col_name, None) is None
        if "IS NOT NULL" in s:
            col_name = getattr(clause.element, "name", None)
            return getattr(run, col_name, None) is not None

    return True


class MockScalars:
    """Mock scalars collection matching SQLAlchemy CursorResult."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def first(self) -> Any | None:
        return self._items[0] if self._items else None

    def all(self) -> list[Any]:
        return list(self._items)


class MockResult:
    """Mock SQLAlchemy execution result."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> MockScalars:
        return MockScalars(self._items)

    def scalar_one_or_none(self) -> Any | None:
        return self._items[0] if self._items else None

    def scalar(self) -> Any | None:
        return self._items[0] if self._items else None

    @property
    def rowcount(self) -> int:
        return len(self._items)


class MockAsyncSession:
    """In-memory AsyncSession supporting Select, Update, returning, and child relationships."""

    def __init__(self) -> None:
        self.runs: dict[uuid.UUID, AutonomousDeployment] = {}
        self.stages: list[AutonomousDeploymentStage] = []
        self.logs: list[AutonomousDeploymentLog] = []
        self.outbox: list[AutonomousDeploymentOutbox] = []

    def add(self, entity: Any) -> None:
        if isinstance(entity, AutonomousDeployment):
            self.runs[entity.id] = entity
        elif isinstance(entity, AutonomousDeploymentStage):
            if entity not in self.stages:
                self.stages.append(entity)
        elif isinstance(entity, AutonomousDeploymentLog):
            if entity not in self.logs:
                self.logs.append(entity)
        elif isinstance(entity, AutonomousDeploymentOutbox):
            if entity not in self.outbox:
                self.outbox.append(entity)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def execute(self, stmt: Any) -> MockResult:
        # Handle UPDATE statements
        if isinstance(stmt, Update) or (hasattr(stmt, "is_update") and stmt.is_update):
            matching_runs: list[AutonomousDeployment] = []
            for run in self.runs.values():
                if _eval_clause(stmt.whereclause, run):
                    matching_runs.append(run)

            # Apply updates
            for run in matching_runs:
                for k, v in stmt._values.items():
                    col_name = k.name
                    if isinstance(v, BinaryExpression):
                        # Expression like fence_token = fence_token + 1
                        left_name = getattr(v.left, "name", col_name)
                        left_val = getattr(run, left_name)
                        right_val = getattr(v.right, "value", v.right)
                        new_val = v.operator(left_val, right_val)
                    elif hasattr(v, "value"):
                        new_val = v.value
                    else:
                        new_val = v
                    setattr(run, col_name, new_val)

            # Check returning clause
            if getattr(stmt, "_returning", None):
                return_col = stmt._returning[0]
                ret_vals = [getattr(r, return_col.name) for r in matching_runs]
                return MockResult(ret_vals)

            return MockResult([r.id for r in matching_runs])

        # Handle SELECT statements
        entity_cls = stmt.column_descriptions[0]["type"]

        if entity_cls is AutonomousDeployment:
            candidates = list(self.runs.values())
            for r in candidates:
                if not r.stages:
                    r.stages = [s for s in self.stages if s.run_id == r.id]
                    r.stages.sort(key=lambda s: s.position)

            matched = [r for r in candidates if _eval_clause(stmt.whereclause, r)]
            return MockResult(matched)

        elif entity_cls is AutonomousDeploymentStage:
            matched_stages = [
                s
                for s in self.stages
                if (
                    not hasattr(stmt, "whereclause")
                    or stmt.whereclause is None
                    or _eval_clause(stmt.whereclause, s)  # type: ignore[arg-type]
                )
            ]
            matched_stages.sort(key=lambda s: s.position)
            return MockResult(matched_stages)

        elif entity_cls is AutonomousDeploymentLog:
            matched_logs = list(self.logs)
            if hasattr(stmt, "whereclause") and stmt.whereclause is not None:
                matched_logs = [l for l in matched_logs if _eval_clause(stmt.whereclause, l)]  # type: ignore[arg-type]
            matched_logs.sort(key=lambda l: l.log_seq)
            return MockResult(matched_logs)

        return MockResult([])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_run(
    session: MockAsyncSession,
    *,
    status: str = "pending",
    fence_token: int = 0,
    worker_id: str | None = None,
    lease_expires_at: datetime | None = None,
    strategy: str = "docker_github_vercel",
) -> AutonomousDeployment:
    """Creates and registers a run with default stage graph in MockAsyncSession."""
    run_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    user_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=proj_id,
        attempt_number=1,
        status=status,
        strategy=strategy,
        configuration={},
        progress_pct=0,
        payload_hash="test-hash",
        created_by=user_id,
        fence_token=fence_token,
        worker_id=worker_id,
        lease_expires_at=lease_expires_at,
        dispatch_status="pending",
        log_sequence_counter=0,
        outbox_sequence_counter=0,
    )
    session.add(run)

    stage_defs = build_stage_graph(strategy)
    stages = []
    for pos, (st_name, gate_id) in enumerate(stage_defs, start=1):
        stage = AutonomousDeploymentStage(
            id=uuid.uuid4(),
            run_id=run.id,
            stage_name=st_name,
            gate_id=gate_id,
            position=pos,
            status="pending",
            progress_pct=0,
            stage_metadata={},
        )
        stages.append(stage)
        session.add(stage)

    run.stages = stages
    return run


# ---------------------------------------------------------------------------
# 1. Worker Claim Protocol Tests
# ---------------------------------------------------------------------------


class TestWorkerClaimProtocol:
    """Verifies atomic run claiming, fence token increment, and lease management."""

    @pytest.mark.asyncio
    async def test_initial_claim_increments_fence_token(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-node-1")
        run = _create_test_run(session, status="pending", fence_token=0, lease_expires_at=None)

        token = await worker.claim_run(session, run_id=run.id, project_id=run.project_id, worker_id="worker-node-1")

        assert token == 1
        assert run.fence_token == 1
        assert run.worker_id == "worker-node-1"
        assert run.dispatch_status == "acknowledged"
        assert run.lease_expires_at is not None

        # Verify lease duration is approximately 30 seconds
        now = datetime.now(timezone.utc)
        assert run.lease_expires_at > now
        assert run.lease_expires_at <= now + timedelta(seconds=31)

    @pytest.mark.asyncio
    async def test_concurrent_claim_rejected_while_lease_is_active(self) -> None:
        session = MockAsyncSession()
        worker1 = AutonomousWorker(worker_id="worker-1")
        worker2 = AutonomousWorker(worker_id="worker-2")
        run = _create_test_run(session, status="pending", fence_token=0)

        # Worker 1 claims first
        token1 = await worker1.claim_run(session, run_id=run.id, worker_id="worker-1")
        assert token1 == 1

        # Worker 2 attempts concurrent claim while lease is active
        token2 = await worker2.claim_run(session, run_id=run.id, worker_id="worker-2")
        assert token2 is None
        assert run.fence_token == 1
        assert run.worker_id == "worker-1"

    @pytest.mark.asyncio
    async def test_claim_takeover_after_lease_expiration(self) -> None:
        session = MockAsyncSession()
        worker1 = AutonomousWorker(worker_id="worker-1")
        worker2 = AutonomousWorker(worker_id="worker-2")
        now = datetime.now(timezone.utc)

        # Run was claimed by worker 1, but its lease expired 5 seconds ago
        run = _create_test_run(
            session,
            status="running",
            fence_token=1,
            worker_id="worker-1",
            lease_expires_at=now - timedelta(seconds=5),
        )

        # Worker 2 takes over the expired lease
        token2 = await worker2.claim_run(session, run_id=run.id, worker_id="worker-2")
        assert token2 == 2
        assert run.fence_token == 2
        assert run.worker_id == "worker-2"
        assert run.lease_expires_at is not None
        assert run.lease_expires_at > now

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "terminal_status",
        ["succeeded", "failed", "cancelled", "rolled_back", "cancelling"],
    )
    async def test_terminal_and_cancelling_runs_cannot_be_claimed(self, terminal_status: str) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        # Even with expired lease or no lease, terminal runs cannot be claimed
        run = _create_test_run(
            session,
            status=terminal_status,
            fence_token=1,
            worker_id="worker-old",
            lease_expires_at=now - timedelta(seconds=10),
        )

        token = await worker.claim_run(session, run_id=run.id, worker_id="worker-1")
        assert token is None
        assert run.fence_token == 1
        assert run.worker_id == "worker-old"

    @pytest.mark.asyncio
    async def test_claim_respects_project_id_boundary(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        run = _create_test_run(session, status="pending")

        foreign_project_id = uuid.uuid4()
        token = await worker.claim_run(
            session, run_id=run.id, project_id=foreign_project_id, worker_id="worker-1"
        )
        assert token is None
        assert run.fence_token == 0


# ---------------------------------------------------------------------------
# 2. Worker Heartbeat Tests
# ---------------------------------------------------------------------------


class TestWorkerHeartbeat:
    """Verifies heartbeat renewals without fence_token increment."""

    @pytest.mark.asyncio
    async def test_routine_heartbeat_extends_lease_without_incrementing_fence_token(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        run = _create_test_run(
            session,
            status="running",
            fence_token=1,
            worker_id="worker-1",
            lease_expires_at=now + timedelta(seconds=10),
        )
        original_lease = run.lease_expires_at

        # Heartbeat routine
        renewed = await worker.heartbeat(session, run_id=run.id, fence_token=1)
        assert renewed is True
        assert run.fence_token == 1  # Crucial: fence_token must NOT increment
        assert run.lease_expires_at is not None
        assert run.lease_expires_at > original_lease

    @pytest.mark.asyncio
    async def test_heartbeat_fails_with_stale_fence_token(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        # Run fence token has moved to 2 (Worker 2 superseded)
        run = _create_test_run(
            session,
            status="running",
            fence_token=2,
            worker_id="worker-2",
            lease_expires_at=now + timedelta(seconds=20),
        )

        # Worker 1 with stale token 1 attempts heartbeat
        renewed = await worker.heartbeat(session, run_id=run.id, fence_token=1)
        assert renewed is False
        assert run.fence_token == 2

    @pytest.mark.asyncio
    async def test_heartbeat_fails_when_lease_already_expired(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        run = _create_test_run(
            session,
            status="running",
            fence_token=1,
            worker_id="worker-1",
            lease_expires_at=now - timedelta(seconds=5),
        )

        renewed = await worker.heartbeat(session, run_id=run.id, fence_token=1)
        assert renewed is False


# ---------------------------------------------------------------------------
# 3. Guarded Update & WorkerFencingLostError Tests
# ---------------------------------------------------------------------------


class TestGuardedUpdateAndFencingLost:
    """Verifies write guard and immediate termination via WorkerFencingLostError."""

    @pytest.mark.asyncio
    async def test_guarded_update_succeeds_with_valid_token_and_lease(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        run = _create_test_run(
            session,
            status="running",
            fence_token=1,
            worker_id="worker-1",
            lease_expires_at=now + timedelta(seconds=25),
        )

        count = await worker.guarded_update(
            session,
            run_id=run.id,
            fence_token=1,
            values={"current_stage": STAGE_G1_BLUEPRINT},
        )
        assert count == 1
        assert run.current_stage == STAGE_G1_BLUEPRINT

    @pytest.mark.asyncio
    async def test_guarded_update_raises_fencing_lost_error_on_stale_token(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        run = _create_test_run(
            session,
            status="running",
            fence_token=2,  # Current token is 2
            worker_id="worker-2",
            lease_expires_at=now + timedelta(seconds=25),
        )

        # Worker 1 with stale token 1 attempts write
        with pytest.raises(WorkerFencingLostError) as exc_info:
            await worker.guarded_update(
                session,
                run_id=run.id,
                fence_token=1,  # Stale token
                values={"current_stage": STAGE_G1_BLUEPRINT},
            )
        assert "fence_token=1" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_guarded_update_raises_fencing_lost_error_on_expired_lease(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        run = _create_test_run(
            session,
            status="running",
            fence_token=1,
            worker_id="worker-1",
            lease_expires_at=now - timedelta(seconds=1),  # Lease expired
        )

        with pytest.raises(WorkerFencingLostError):
            await worker.guarded_update(
                session,
                run_id=run.id,
                fence_token=1,
                values={"current_stage": STAGE_G1_BLUEPRINT},
            )

    @pytest.mark.asyncio
    async def test_check_cancellation_lifecycle(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        run = _create_test_run(
            session,
            status="running",
            fence_token=1,
            worker_id="worker-1",
            lease_expires_at=now + timedelta(seconds=20),
        )

        # Status running -> returns False
        assert await worker.check_cancellation(session, run_id=run.id, fence_token=1) is False

        # Status transitioning to cancelling -> returns True
        run.status = "cancelling"
        assert await worker.check_cancellation(session, run_id=run.id, fence_token=1) is True

        # Status cancelled -> returns True
        run.status = "cancelled"
        assert await worker.check_cancellation(session, run_id=run.id, fence_token=1) is True

        # Stale fence token -> raises WorkerFencingLostError
        with pytest.raises(WorkerFencingLostError):
            await worker.check_cancellation(session, run_id=run.id, fence_token=99)

        # Expired lease -> raises WorkerFencingLostError
        run.lease_expires_at = now - timedelta(seconds=5)
        with pytest.raises(WorkerFencingLostError):
            await worker.check_cancellation(session, run_id=run.id, fence_token=1)


# ---------------------------------------------------------------------------
# 4. Stage Transitions and Outbox Emission Tests
# ---------------------------------------------------------------------------


class TestStageTransitionsAndOutbox:
    """Verifies stage updates, timestamps, outbox emission, and monotonic run progress."""

    @pytest.mark.asyncio
    async def test_transition_stage_running_sets_timestamps_and_outbox(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        run = _create_test_run(
            session,
            status="pending",
            fence_token=1,
            worker_id="worker-1",
            lease_expires_at=now + timedelta(seconds=30),
        )

        stage = await worker.transition_stage(
            session,
            run_id=run.id,
            stage_name=STAGE_G1_BLUEPRINT,
            fence_token=1,
            status="running",
            progress_pct=50,
            metadata={"blueprint": "nodejs-react"},
        )

        assert stage.status == "running"
        assert stage.progress_pct == 50
        assert stage.started_at is not None
        assert stage.completed_at is None
        assert stage.stage_metadata["blueprint"] == "nodejs-react"

        # Run level status and current stage
        assert run.status == "running"
        assert run.current_stage == STAGE_G1_BLUEPRINT
        assert run.started_at is not None
        assert run.outbox_sequence_counter == 1

        # Outbox event registration
        assert len(session.outbox) == 1
        outbox = session.outbox[0]
        assert outbox.run_id == run.id
        assert outbox.event_seq == 1
        assert outbox.event_type == "stage_running"
        assert outbox.payload["stage_name"] == STAGE_G1_BLUEPRINT
        assert outbox.payload["status"] == "running"
        assert outbox.payload["metadata"]["blueprint"] == "nodejs-react"

    @pytest.mark.asyncio
    async def test_transition_stage_succeeded_completes_stage(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        run = _create_test_run(
            session,
            status="running",
            fence_token=1,
            worker_id="worker-1",
            lease_expires_at=now + timedelta(seconds=30),
        )

        stage = await worker.transition_stage(
            session,
            run_id=run.id,
            stage_name=STAGE_G1_BLUEPRINT,
            fence_token=1,
            status="succeeded",
            progress_pct=100,
        )

        assert stage.status == "succeeded"
        assert stage.progress_pct == 100
        assert stage.completed_at is not None
        assert run.outbox_sequence_counter == 1
        assert session.outbox[0].event_type == "stage_succeeded"

    @pytest.mark.asyncio
    async def test_transition_stage_fails_on_stale_token(self) -> None:
        session = MockAsyncSession()
        worker = AutonomousWorker(worker_id="worker-1")
        now = datetime.now(timezone.utc)

        run = _create_test_run(
            session,
            status="running",
            fence_token=2,
            worker_id="worker-2",
            lease_expires_at=now + timedelta(seconds=30),
        )

        with pytest.raises(WorkerFencingLostError):
            await worker.transition_stage(
                session,
                run_id=run.id,
                stage_name=STAGE_G1_BLUEPRINT,
                fence_token=1,  # Stale token
                status="succeeded",
            )


# ---------------------------------------------------------------------------
# 5. Monotonic Progress Calculation Tests
# ---------------------------------------------------------------------------


class TestMonotonicProgressCalculation:
    """Verifies monotonic progress calculation across all 4 strategies and G7 gating."""

    def test_docker_github_vercel_strategy_progress(self) -> None:
        stages = [
            {"stage_name": STAGE_G1_BLUEPRINT, "status": "pending", "progress_pct": 0},
            {"stage_name": STAGE_G2_ARTIFACT, "status": "pending", "progress_pct": 0},
            {"stage_name": STAGE_G3_CONSISTENCY, "status": "pending", "progress_pct": 0},
            {"stage_name": STAGE_G4_BUILD, "status": "pending", "progress_pct": 0},
            {"stage_name": STAGE_G5_APPLY, "status": "pending", "progress_pct": 0},
            {"stage_name": STAGE_G6_WORKLOAD, "status": "pending", "progress_pct": 0},
            {"stage_name": STAGE_GITHUB_RELEASE, "status": "pending", "progress_pct": 0},
            {"stage_name": STAGE_VERCEL_DEPLOY, "status": "pending", "progress_pct": 0},
            {"stage_name": STAGE_G7_VERIFICATION, "gate_id": "G7", "status": "pending", "progress_pct": 0},
        ]
        strat = DeploymentStrategy.DOCKER_GITHUB_VERCEL

        # 0. Initial pending: 0%
        assert calculate_progress(stages, strat) == 0

        # 1. G1 succeeds: +10% -> 10%
        stages[0]["status"] = "succeeded"
        p1 = calculate_progress(stages, strat)
        assert p1 == 10

        # 2. G2 succeeds: +10% -> 20%
        stages[1]["status"] = "succeeded"
        p2 = calculate_progress(stages, strat)
        assert p2 == 20

        # 3. G3 succeeds: +10% -> 30%
        stages[2]["status"] = "succeeded"
        p3 = calculate_progress(stages, strat)
        assert p3 == 30

        # 4. G4 succeeds: +15% -> 45%
        stages[3]["status"] = "succeeded"
        p4 = calculate_progress(stages, strat)
        assert p4 == 45

        # 5. G5 succeeds: +15% -> 60%
        stages[4]["status"] = "succeeded"
        p5 = calculate_progress(stages, strat)
        assert p5 == 60

        # 6. G6 succeeds: +15% -> 75%
        stages[5]["status"] = "succeeded"
        p6 = calculate_progress(stages, strat)
        assert p6 == 75

        # 7. GitHub release succeeds: +10% -> 85%
        stages[6]["status"] = "succeeded"
        p7 = calculate_progress(stages, strat)
        assert p7 == 85

        # 8. Vercel deploy succeeds: +10% -> 95%
        stages[7]["status"] = "succeeded"
        p8 = calculate_progress(stages, strat)
        assert p8 == 95

        # 9. G7 running at 80%: 95 + 5*0.8 = 99%
        stages[8]["status"] = "running"
        stages[8]["progress_pct"] = 80
        p9_running = calculate_progress(stages, strat)
        assert p9_running == 99

        # Crucial: Without G7 succeeded, progress can never reach 100%
        stages[8]["progress_pct"] = 100  # Running at 100%, but not settled to succeeded
        assert calculate_progress(stages, strat) == 99

        # 10. G7 verification succeeds -> reaches 100%
        stages[8]["status"] = "succeeded"
        p_final = calculate_progress(stages, strat)
        assert p_final == 100

        # Strictly monotonic
        assert 0 < p1 < p2 < p3 < p4 < p5 < p6 < p7 < p8 <= p9_running < p_final

    def test_docker_github_strategy_progress(self) -> None:
        stages = [
            {"stage_name": STAGE_G1_BLUEPRINT, "status": "pending"},
            {"stage_name": STAGE_G2_ARTIFACT, "status": "pending"},
            {"stage_name": STAGE_G3_CONSISTENCY, "status": "pending"},
            {"stage_name": STAGE_G4_BUILD, "status": "pending"},
            {"stage_name": STAGE_G5_APPLY, "status": "pending"},
            {"stage_name": STAGE_G6_WORKLOAD, "status": "pending"},
            {"stage_name": STAGE_GITHUB_RELEASE, "status": "pending"},
            {"stage_name": STAGE_G7_VERIFICATION, "gate_id": "G7", "status": "pending"},
        ]
        strat = DeploymentStrategy.DOCKER_GITHUB
        prog_seq = [calculate_progress(stages, strat)]

        for s in stages[:-1]:
            s["status"] = "succeeded"
            prog_seq.append(calculate_progress(stages, strat))

        # Before G7: strictly < 100
        assert prog_seq[-1] == 90

        # G7 succeeds -> 100%
        stages[-1]["status"] = "succeeded"
        prog_seq.append(calculate_progress(stages, strat))
        assert prog_seq[-1] == 100

        # Check monotonic increase
        assert all(prog_seq[i] <= prog_seq[i + 1] for i in range(len(prog_seq) - 1))

    def test_github_only_strategy_progress(self) -> None:
        stages = [
            {"stage_name": STAGE_G1_BLUEPRINT, "status": "pending"},
            {"stage_name": STAGE_G2_ARTIFACT, "status": "pending"},
            {"stage_name": STAGE_G3_CONSISTENCY, "status": "pending"},
            {"stage_name": STAGE_GITHUB_RELEASE, "status": "pending"},
            {"stage_name": STAGE_G7_VERIFICATION, "gate_id": "G7", "status": "pending"},
        ]
        strat = DeploymentStrategy.GITHUB_ONLY
        prog_seq = [calculate_progress(stages, strat)]

        for s in stages[:-1]:
            s["status"] = "succeeded"
            prog_seq.append(calculate_progress(stages, strat))

        assert prog_seq[-1] == 90

        stages[-1]["status"] = "succeeded"
        prog_seq.append(calculate_progress(stages, strat))
        assert prog_seq[-1] == 100
        assert all(prog_seq[i] <= prog_seq[i + 1] for i in range(len(prog_seq) - 1))

    def test_vercel_only_strategy_progress(self) -> None:
        stages = [
            {"stage_name": STAGE_G1_BLUEPRINT, "status": "pending"},
            {"stage_name": STAGE_G2_ARTIFACT, "status": "pending"},
            {"stage_name": STAGE_G3_CONSISTENCY, "status": "pending"},
            {"stage_name": STAGE_VERCEL_DEPLOY, "status": "pending"},
            {"stage_name": STAGE_G7_VERIFICATION, "gate_id": "G7", "status": "pending"},
        ]
        strat = DeploymentStrategy.VERCEL_ONLY
        prog_seq = [calculate_progress(stages, strat)]

        for s in stages[:-1]:
            s["status"] = "succeeded"
            prog_seq.append(calculate_progress(stages, strat))

        assert prog_seq[-1] == 90

        stages[-1]["status"] = "succeeded"
        prog_seq.append(calculate_progress(stages, strat))
        assert prog_seq[-1] == 100
        assert all(prog_seq[i] <= prog_seq[i + 1] for i in range(len(prog_seq) - 1))
