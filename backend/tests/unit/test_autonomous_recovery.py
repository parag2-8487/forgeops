# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for Autonomous Deployment Orchestrator Phase 6:
Cancellation Settlement, Compensation Rollback & Recovery Sweeper.

Tests:
1. CancellationSettlement:
   - Tracks active child subprocesses and asyncio tasks for runs.
   - Graceful SIGTERM cancellation when process exits within grace period.
   - Escalation to SIGKILL when process does not terminate within grace period.
   - Task cancellation for registered asyncio tasks.
   - Terminal cancelled state, completed_at, and outbox event emission.
   - Pipeline cancellation settlement integration.
2. CompensationRollback:
   - Strategy-specific rollback cleans up Docker containers labeled with `forgeops.run_id = :run_id`.
   - Successful rollback marks run status = 'rolled_back' and preserves primary_error.
   - Rollback failure preserves both primary_error and compensation_error separately with status = 'failed'.
   - Pipeline failure path triggers compensation rollback when enabled.
3. AutonomousRecoverySweeper:
   - Finds runs with expired leases (lease_expires_at < now() - 60s).
   - Atomically claims expired runs with incremented fence_token and new lease.
   - Reconciles exact operation identities (GitHub commit SHA, Docker container labels, Vercel deployment ID).
   - Ignores active leases (future expiry and within 60s grace period).
   - Ignores terminal runs ('succeeded', 'failed', 'cancelled', 'rolled_back').
   - Handles reconciliation failures by marking run failed.
"""

from __future__ import annotations

import asyncio
import operator
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import Update
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList, UnaryExpression

from src.deployments.autonomous_gates import GateResult
from src.deployments.autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentLog,
    AutonomousDeploymentOutbox,
    AutonomousDeploymentStage,
)
from src.deployments.autonomous_recovery import (
    AutonomousRecoverySweeper,
    CancellationSettlement,
    CompensationRollback,
    ReconciliationResult,
    cleanup_docker_containers,
)
from src.deployments.autonomous_schemas import (
    CreateAutonomousRunRequest,
    DeploymentStrategy,
    DockerConfigRequest,
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
    run_pipeline,
)


# ===========================================================================
# In-Memory SQLAlchemy Test Harness
# ===========================================================================


def _eval_clause(clause: Any, run: Any) -> bool:
    """Evaluates an arbitrary SQLAlchemy whereclause AST against an entity."""
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
        if isinstance(stmt, Update) or (hasattr(stmt, "is_update") and stmt.is_update):
            matching_runs: list[AutonomousDeployment] = []
            for run in self.runs.values():
                if _eval_clause(stmt.whereclause, run):
                    matching_runs.append(run)

            for run in matching_runs:
                for k, v in stmt._values.items():
                    col_name = k.name
                    if isinstance(v, BinaryExpression):
                        left_name = getattr(v.left, "name", col_name)
                        left_val = getattr(run, left_name)
                        right_val = getattr(v.right, "value", v.right)
                        new_val = v.operator(left_val, right_val)
                    elif hasattr(v, "value"):
                        new_val = v.value
                    else:
                        new_val = v
                    setattr(run, col_name, new_val)

            if getattr(stmt, "_returning", None):
                return_col = stmt._returning[0]
                ret_vals = [getattr(r, return_col.name) for r in matching_runs]
                return MockResult(ret_vals)

            return MockResult([r.id for r in matching_runs])

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
                    or _eval_clause(stmt.whereclause, s)
                )
            ]
            matched_stages.sort(key=lambda s: s.position)
            return MockResult(matched_stages)

        elif entity_cls is AutonomousDeploymentLog:
            matched_logs = list(self.logs)
            if hasattr(stmt, "whereclause") and stmt.whereclause is not None:
                matched_logs = [l for l in matched_logs if _eval_clause(stmt.whereclause, l)]
            matched_logs.sort(key=lambda l: l.log_seq)
            return MockResult(matched_logs)

        elif entity_cls is AutonomousDeploymentOutbox:
            matched_outbox = [
                o
                for o in self.outbox
                if (
                    not hasattr(stmt, "whereclause")
                    or stmt.whereclause is None
                    or _eval_clause(stmt.whereclause, o)
                )
            ]
            return MockResult(matched_outbox)

        return MockResult([])


async def _create_test_run(
    session: MockAsyncSession,
    strategy: DeploymentStrategy = DeploymentStrategy.DOCKER_GITHUB_VERCEL,
    status: str = "pending",
    fence_token: int = 0,
    lease_expires_at: datetime | None = None,
    worker_id: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> AutonomousDeployment:
    """Helper to initialize an autonomous deployment run with strategy-appropriate stages."""
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()

    default_config: dict[str, Any] = {"strategy": strategy.value}
    if strategy in (DeploymentStrategy.DOCKER_GITHUB_VERCEL, DeploymentStrategy.DOCKER_GITHUB, DeploymentStrategy.GITHUB_ONLY):
        default_config["github_config"] = {
            "repository_name": "forgeops/test-repo",
            "target_branch": "main",
            "commit_message": "Automated deployment by ForgeOps",
        }
    if strategy in (DeploymentStrategy.DOCKER_GITHUB_VERCEL, DeploymentStrategy.VERCEL_ONLY):
        default_config["vercel_config"] = {
            "project_name": "forgeops-app",
            "production_deploy": True,
        }
    if strategy in (DeploymentStrategy.DOCKER_GITHUB_VERCEL, DeploymentStrategy.DOCKER_GITHUB):
        default_config["docker_config"] = {
            "port_bindings": {"8080": 8080},
            "container_name": "forgeops-test-cnt",
        }

    if config_overrides:
        default_config.update(config_overrides)

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status=status,
        strategy=strategy.value,
        configuration=default_config,
        progress_pct=0,
        payload_hash="dummy_hash_for_testing",
        created_by=uuid.uuid4(),
        dispatch_status="pending",
        fence_token=fence_token,
        lease_expires_at=lease_expires_at,
        worker_id=worker_id,
        log_sequence_counter=0,
        outbox_sequence_counter=1,
    )
    session.add(run)

    stage_defs = build_stage_graph(strategy)
    stages: list[AutonomousDeploymentStage] = []
    for pos, (s_name, g_id) in enumerate(stage_defs, start=1):
        st = AutonomousDeploymentStage(
            id=uuid.uuid4(),
            run_id=run.id,
            stage_name=s_name,
            gate_id=g_id,
            position=pos,
            status="pending",
            progress_pct=0,
            stage_metadata={},
        )
        session.add(st)
        stages.append(st)

    run.stages = stages
    await session.flush()
    return run


# ===========================================================================
# Mock Subprocess Helper
# ===========================================================================


class MockProcess:
    """Mock subprocess tracking terminate, kill, and exit state."""

    def __init__(self, exits_on_terminate: bool = True) -> None:
        self.pid = 9999
        self.exits_on_terminate = exits_on_terminate
        self.terminated = False
        self.killed = False
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.terminated = True
        if self.exits_on_terminate:
            self.returncode = -15  # SIGTERM

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9  # SIGKILL

    def poll(self) -> int | None:
        return self.returncode

    async def wait(self) -> int:
        if self.exits_on_terminate and self.terminated:
            return -15
        if self.killed:
            return -9
        # Stubborn process: simulate hanging
        await asyncio.sleep(10.0)
        return 0


# ===========================================================================
# 1. Subprocess Cancellation Settlement Tests
# ===========================================================================


class TestCancellationSettlement:
    """Verifies cooperative child process tracking, graceful SIGTERM, SIGKILL escalation,
    and terminal cancelled status settlement.
    """

    @pytest.mark.asyncio
    async def test_graceful_sigterm_cancellation(self) -> None:
        """When child process exits cleanly upon SIGTERM, kill() is never invoked."""
        session = MockAsyncSession()
        run = await _create_test_run(session, status="running")
        settler = CancellationSettlement()

        proc = MockProcess(exits_on_terminate=True)
        settler.register_process(run.id, proc)

        assert proc in settler.get_active_processes(run.id)

        # Settle cancellation
        cancelled_run = await settler.settle_cancellation(
            session,
            run=run,
            grace_period_seconds=1.0,
            reason="User cancelled pipeline",
        )

        # Assert process signals
        assert proc.terminated is True
        assert proc.killed is False
        assert proc.returncode == -15

        # Assert run state
        assert cancelled_run.status == "cancelled"
        assert cancelled_run.completed_at is not None
        assert cancelled_run.error_summary == "User cancelled pipeline"

        # Assert outbox event
        outbox_events = [o for o in session.outbox if o.run_id == run.id and o.event_type == "run_cancelled"]
        assert len(outbox_events) == 1
        assert outbox_events[0].payload["status"] == "cancelled"

        # Process list should be cleared
        assert settler.get_active_processes(run.id) == []

    @pytest.mark.asyncio
    async def test_escalation_to_sigkill_when_process_hangs(self) -> None:
        """When child process ignores SIGTERM, settlement escalates to SIGKILL after grace period."""
        session = MockAsyncSession()
        run = await _create_test_run(session, status="running")
        settler = CancellationSettlement()

        # Stubborn process does not exit on terminate
        stubborn_proc = MockProcess(exits_on_terminate=False)
        settler.register_process(run.id, stubborn_proc)

        # Settle with tiny grace period for fast test execution
        cancelled_run = await settler.settle_cancellation(
            session,
            run=run,
            grace_period_seconds=0.05,
            reason="Timeout exceeded",
        )

        assert stubborn_proc.terminated is True
        assert stubborn_proc.killed is True
        assert stubborn_proc.returncode == -9

        assert cancelled_run.status == "cancelled"
        assert cancelled_run.completed_at is not None

    @pytest.mark.asyncio
    async def test_cancels_active_asyncio_tasks(self) -> None:
        """Registered asyncio tasks are cleanly cancelled during settlement."""
        session = MockAsyncSession()
        run = await _create_test_run(session, status="running")
        settler = CancellationSettlement()

        async def _long_running_task() -> None:
            await asyncio.sleep(60.0)

        task = asyncio.create_task(_long_running_task())
        settler.register_task(run.id, task)

        await settler.settle_cancellation(session, run=run)
        await asyncio.sleep(0)

        # Task was cancelled
        cancelling = task.cancelling() if hasattr(task, "cancelling") else False
        assert task.cancelled() or cancelling or task.done()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_settles_running_stages_to_cancelled(self) -> None:
        """Any stages currently running are marked cancelled with completed_at set."""
        session = MockAsyncSession()
        run = await _create_test_run(session, status="running")
        settler = CancellationSettlement()

        g1_stage = next(s for s in run.stages if s.stage_name == STAGE_G1_BLUEPRINT)
        g1_stage.status = "running"
        g1_stage.started_at = datetime.now(timezone.utc)

        await settler.settle_cancellation(
            session,
            run=run,
            current_stage=g1_stage,
        )

        assert g1_stage.status == "cancelled"
        assert g1_stage.completed_at is not None
        assert run.status == "cancelled"

    @pytest.mark.asyncio
    async def test_pipeline_integration_with_cancellation_settlement(self) -> None:
        """Worker pipeline cooperatively terminates active processes and settles on cancellation."""
        session = MockAsyncSession()
        run = await _create_test_run(session, status="cancelling")

        worker = AutonomousWorker(worker_id="test-cancel-worker")
        proc = MockProcess(exits_on_terminate=True)
        worker.cancellation_settlement.register_process(run.id, proc)

        cancelled_run = await worker.run_pipeline(session, run_id=run.id)

        assert proc.terminated is True
        assert cancelled_run.status == "cancelled"
        assert cancelled_run.completed_at is not None


# ===========================================================================
# 2. Compensation Rollback Tests
# ===========================================================================


class TestCompensationRollback:
    """Verifies strategy-specific rollback, Docker cleanup, and dual error preservation."""

    @pytest.mark.asyncio
    async def test_docker_cleanup_success_marks_rolled_back(self) -> None:
        """When compensation rollback succeeds: cleans up Docker containers, marks status='rolled_back',
        and preserves primary_error with compensation_error=None.
        """
        session = MockAsyncSession()
        run = await _create_test_run(session, strategy=DeploymentStrategy.DOCKER_GITHUB, status="failed")

        cleaned_ids: list[str] = []

        async def _mock_docker_cleanup(run_id: str) -> list[str]:
            cleaned_ids.append(f"container-for-{run_id}")
            return [f"container-for-{run_id}"]

        rollback = CompensationRollback(docker_cleanup_fn=_mock_docker_cleanup)

        primary_err = {
            "stage_name": STAGE_G5_APPLY,
            "gate_id": "G5",
            "message": "Container startup timeout",
        }

        rolled_back_run = await rollback.execute_rollback(
            session,
            run=run,
            primary_error=primary_err,
        )

        assert rolled_back_run.status == "rolled_back"
        assert rolled_back_run.primary_error == primary_err
        assert rolled_back_run.compensation_error is None
        assert rolled_back_run.completed_at is not None
        assert len(cleaned_ids) == 1

        # Assert outbox event emitted
        outbox_events = [o for o in session.outbox if o.run_id == run.id and o.event_type == "run_rolled_back"]
        assert len(outbox_events) == 1
        assert outbox_events[0].payload["status"] == "rolled_back"
        assert outbox_events[0].payload["primary_error"] == primary_err

    @pytest.mark.asyncio
    async def test_docker_cleanup_failure_preserves_both_errors_with_failed_status(self) -> None:
        """When compensation rollback fails: marks status='failed', preserves primary_error,
        and records compensation_error.
        """
        session = MockAsyncSession()
        run = await _create_test_run(session, strategy=DeploymentStrategy.DOCKER_GITHUB, status="failed")

        async def _failing_docker_cleanup(run_id: str, context: Any = None) -> list[str]:
            raise ConnectionError("Docker daemon unreachable on socket /var/run/docker.sock")

        rollback = CompensationRollback(docker_cleanup_fn=_failing_docker_cleanup)

        primary_err = {
            "stage_name": STAGE_G6_WORKLOAD,
            "gate_id": "G6",
            "message": "Workload readiness probe failed: 503 Service Unavailable",
        }

        failed_run = await rollback.execute_rollback(
            session,
            run=run,
            primary_error=primary_err,
        )

        # Status must remain 'failed' because rollback failed
        assert failed_run.status == "failed"

        # Primary error preserved
        assert failed_run.primary_error == primary_err

        # Compensation error recorded
        assert failed_run.compensation_error is not None
        assert "unreachable" in failed_run.compensation_error["message"]
        assert failed_run.compensation_error["stage"] == "compensation_rollback"

        # Both errors exist in database
        assert failed_run.primary_error is not None and failed_run.compensation_error is not None

        # Outbox event emitted for failure with dual errors
        outbox_events = [o for o in session.outbox if o.run_id == run.id and o.event_type == "run_failed"]
        assert len(outbox_events) == 1
        assert outbox_events[0].payload["status"] == "failed"
        assert outbox_events[0].payload["compensation_error"] is not None

    @pytest.mark.asyncio
    async def test_non_docker_strategy_rollback_marks_rolled_back(self) -> None:
        """For github_only strategy, rollback cleanly transitions to rolled_back without docker calls."""
        session = MockAsyncSession()
        run = await _create_test_run(session, strategy=DeploymentStrategy.GITHUB_ONLY, status="failed")

        docker_called = False

        async def _mock_docker_cleanup(run_id: str, context: Any = None) -> list[str]:
            nonlocal docker_called
            docker_called = True
            return []

        rollback = CompensationRollback(docker_cleanup_fn=_mock_docker_cleanup)

        primary_err = {"stage_name": STAGE_GITHUB_RELEASE, "message": "GitHub API error"}
        rolled_back_run = await rollback.execute_rollback(session, run=run, primary_error=primary_err)

        assert rolled_back_run.status == "rolled_back"
        assert docker_called is False
        assert rolled_back_run.primary_error == primary_err

    @pytest.mark.asyncio
    async def test_pipeline_failure_triggers_rollback_when_enabled(self) -> None:
        """Worker pipeline automatically triggers compensation rollback when rollback_on_failure=True."""
        session = MockAsyncSession()
        run = await _create_test_run(session, strategy=DeploymentStrategy.DOCKER_GITHUB)

        cleaned_containers: list[str] = []

        async def _mock_cleanup(run_id: str) -> list[str]:
            cleaned_containers.append(f"c-{run_id}")
            return [f"c-{run_id}"]

        # Context triggers G4 build failure and specifies rollback
        failed_run = await run_pipeline(
            session,
            run_id=run.id,
            worker_id="test-worker-fail-rollback",
            context={
                "fail_g4": True,
                "docker_cleanup": _mock_cleanup,
                "rollback_on_failure": True,
            },
        )

        assert failed_run.status == "rolled_back"
        assert failed_run.primary_error is not None
        assert failed_run.primary_error["stage_name"] == STAGE_G4_BUILD
        assert len(cleaned_containers) == 1


# ===========================================================================
# 3. Autonomous Recovery Sweeper Tests
# ===========================================================================


class TestAutonomousRecoverySweeper:
    """Verifies crash recovery: sweeping expired leases (> 60s), fence_token increments,
    operation identity reconciliation, and ignoring active/terminal runs.
    """

    @pytest.mark.asyncio
    async def test_sweeper_finds_expired_runs_and_increments_fence_token(self) -> None:
        """Sweeper finds runs in running/pending whose lease expired > 60s ago,
        claims them with incremented fence_token, and reconciles state.
        """
        session = MockAsyncSession()
        now = datetime.now(timezone.utc)

        # Run with lease expired 90 seconds ago (> 60s threshold)
        expired_run = await _create_test_run(
            session,
            strategy=DeploymentStrategy.DOCKER_GITHUB,
            status="running",
            fence_token=1,
            lease_expires_at=now - timedelta(seconds=90),
            worker_id="crashed-worker",
        )

        sweeper = AutonomousRecoverySweeper(sweeper_id="recovery-sweeper-1")

        swept = await sweeper.sweep(session)

        assert len(swept) == 1
        assert swept[0].id == expired_run.id

        # Fence token must have incremented (1 -> 2)
        assert swept[0].fence_token == 2
        assert swept[0].worker_id == "recovery-sweeper-1"
        assert swept[0].dispatch_status == "recovered"
        # Lease extended
        assert swept[0].lease_expires_at > now

    @pytest.mark.asyncio
    async def test_sweeper_ignores_active_leases(self) -> None:
        """Runs with active leases or leases expired less than 60s ago are ignored."""
        session = MockAsyncSession()
        now = datetime.now(timezone.utc)

        # 1. Run with active lease in the future
        active_run = await _create_test_run(
            session,
            status="running",
            fence_token=1,
            lease_expires_at=now + timedelta(seconds=20),
            worker_id="live-worker",
        )

        # 2. Run with lease expired only 15 seconds ago (within 60s grace period)
        grace_run = await _create_test_run(
            session,
            status="running",
            fence_token=5,
            lease_expires_at=now - timedelta(seconds=15),
            worker_id="recently-timed-out-worker",
        )

        sweeper = AutonomousRecoverySweeper(sweeper_id="recovery-sweeper-1")
        swept = await sweeper.sweep(session)

        assert swept == []
        assert active_run.fence_token == 1
        assert grace_run.fence_token == 5

    @pytest.mark.asyncio
    async def test_sweeper_ignores_terminal_runs(self) -> None:
        """Terminal runs ('succeeded', 'failed', 'cancelled', 'rolled_back') with expired leases are ignored."""
        session = MockAsyncSession()
        now = datetime.now(timezone.utc)

        terminal_statuses = ["succeeded", "failed", "cancelled", "rolled_back"]
        for st in terminal_statuses:
            await _create_test_run(
                session,
                status=st,
                fence_token=1,
                lease_expires_at=now - timedelta(seconds=120),
            )

        sweeper = AutonomousRecoverySweeper(sweeper_id="recovery-sweeper-1")
        swept = await sweeper.sweep(session)

        assert swept == []

    @pytest.mark.asyncio
    async def test_sweeper_reconciles_exact_operation_identities(self) -> None:
        """Reconciliation verifies Docker container labels, GitHub commit SHA, and Vercel deployment ID."""
        session = MockAsyncSession()
        now = datetime.now(timezone.utc)

        run = await _create_test_run(
            session,
            strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
            status="running",
            fence_token=1,
            lease_expires_at=now - timedelta(seconds=100),
        )

        sweeper = AutonomousRecoverySweeper(sweeper_id="recovery-sweeper-1")

        reconciled_docker = False
        reconciled_github = False
        reconciled_vercel = False

        async def _docker_rec(run_id: Any, ctx: Any) -> dict[str, Any]:
            nonlocal reconciled_docker
            reconciled_docker = True
            return {"container_id": f"cont-{run_id}", "status": "running"}

        async def _gh_rec(r: Any, ctx: Any) -> dict[str, Any]:
            nonlocal reconciled_github
            reconciled_github = True
            return {"commit_sha": "sha_recovered_123", "branch": "main"}

        async def _v_rec(r: Any, ctx: Any) -> dict[str, Any]:
            nonlocal reconciled_vercel
            reconciled_vercel = True
            return {"deployment_id": "dpl_recovered_456", "status": "ready"}

        recon_result = await sweeper.reconcile_run(
            session,
            run,
            context={
                "docker_reconciler": _docker_rec,
                "github_reconciler": _gh_rec,
                "vercel_reconciler": _v_rec,
            },
        )

        assert recon_result.reconciled is True
        assert recon_result.docker_status == "verified"
        assert recon_result.github_status == "verified"
        assert recon_result.vercel_status == "verified"
        assert reconciled_docker is True
        assert reconciled_github is True
        assert reconciled_vercel is True

    @pytest.mark.asyncio
    async def test_sweeper_reconciliation_failure_marks_run_failed(self) -> None:
        """When external reconciliation reveals missing or failed resources, run is marked failed."""
        session = MockAsyncSession()
        now = datetime.now(timezone.utc)

        run = await _create_test_run(
            session,
            strategy=DeploymentStrategy.DOCKER_GITHUB,
            status="running",
            fence_token=1,
            lease_expires_at=now - timedelta(seconds=100),
        )

        sweeper = AutonomousRecoverySweeper(sweeper_id="recovery-sweeper-1")

        # Context forces Docker reconciliation failure (container disappeared)
        swept = await sweeper.sweep(
            session,
            context={"fail_docker_reconciliation": True},
        )

        assert len(swept) == 1
        failed_run = swept[0]
        assert failed_run.status == "failed"
        assert failed_run.error_summary is not None
        assert "reconciliation failed" in failed_run.error_summary.lower()
        assert failed_run.primary_error is not None
        assert failed_run.completed_at is not None

    @pytest.mark.asyncio
    async def test_sweeper_recovers_crashed_run_before_task_enqueueing(self) -> None:
        """Regression test for crash window between start commit and task enqueue.

        Simulates:
        1. Run transitioned to 'running' with dispatch_status='enqueued'.
        2. Backend crashes before task enqueueing completes (lease_expires_at IS NULL, fence_token=0).
        3. Sweeper discovers orphaned run (dispatch_requested_at > 60s ago).
        4. Sweeper claims custody, increments fence_token (0 -> 1), reconciles state,
           re-enqueues execution to task dispatcher, and emits run_recovered event.
        """
        session = MockAsyncSession()
        now = datetime.now(timezone.utc)

        # 1. Run committed to DB but process crashed before worker claim/enqueue
        run = await _create_test_run(
            session,
            strategy=DeploymentStrategy.DOCKER_GITHUB,
            status="running",
            fence_token=0,
            lease_expires_at=None,
            worker_id=None,
        )
        run.dispatch_status = "enqueued"
        run.dispatch_requested_at = now - timedelta(seconds=90)
        run.started_at = now - timedelta(seconds=90)

        # 2. Mock task dispatcher to capture re-enqueue
        dispatched_tasks: list[tuple[str, dict[str, Any]]] = []

        class MockDispatcher:
            async def enqueue(self, task_name: str, payload: dict[str, Any]) -> None:
                dispatched_tasks.append((task_name, payload))

        mock_dispatcher = MockDispatcher()
        sweeper = AutonomousRecoverySweeper(sweeper_id="recovery-sweeper-crash-window")

        # 3. Find expired/orphaned runs
        expired_runs = await sweeper.find_expired_runs(session)
        assert len(expired_runs) == 1
        assert expired_runs[0].id == run.id

        # 4. Execute sweep
        swept = await sweeper.sweep(
            session,
            context={"task_dispatcher": mock_dispatcher},
        )

        assert len(swept) == 1
        recovered_run = swept[0]
        assert recovered_run.id == run.id
        assert recovered_run.fence_token == 1
        assert recovered_run.worker_id == "recovery-sweeper-crash-window"
        assert recovered_run.dispatch_status == "recovered"
        assert recovered_run.lease_expires_at is not None
        assert recovered_run.lease_expires_at > now

        # Verify task was re-enqueued to dispatcher
        assert len(dispatched_tasks) == 1
        task_name, payload = dispatched_tasks[0]
        assert task_name == "autonomous_deploy_run"
        assert payload["run_id"] == str(run.id)
        assert payload["project_id"] == str(run.project_id)

        # Verify run_recovered outbox event emitted
        outbox_events = [o for o in session.outbox if o.run_id == run.id and o.event_type == "run_recovered"]
        assert len(outbox_events) == 1
        assert outbox_events[0].payload["fence_token"] == 1
        assert outbox_events[0].payload["dispatch_status"] == "recovered"
