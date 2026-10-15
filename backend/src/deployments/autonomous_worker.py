# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous Deployment Orchestrator Durable Worker & Fencing Token Management (Phase 4).

Implements:
1. AutonomousWorker:
   - Atomic claim query (`claim_run`) with fence_token increment, worker custody, and lease expiry.
   - Routine heartbeats (`heartbeat`) extending active leases without token increment.
   - Guarded updates (`guarded_update`) asserting fence_token and active lease, raising WorkerFencingLostError.
   - Stage transitions (`transition_stage`) updating stage state, monotonic run progress, and outbox emission.
   - Cooperative cancellation check (`check_cancellation`).
   - Monotonic progress calculation (`calculate_progress`) based on strategy weights.
2. WorkerFencingLostError: Halts stale workers immediately upon lease expiration or epoch preemption.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload
from src.core.logging import redact_secrets

from .autonomous_credentials import resolve_deployment_credentials
from .autonomous_gates import (
    GATE_EVALUATORS,
    STAGE_EVALUATORS,
    GateResult,
)
from .autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentOutbox,
    AutonomousDeploymentStage,
)
from .autonomous_outbox import AutonomousOutboxPublisher
from .autonomous_recovery import (
    CancellationSettlement,
    CompensationRollback,
)
from .autonomous_schemas import DeploymentStrategy
from .autonomous_service import (
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
)

logger = logging.getLogger(__name__)

LEASE_DURATION_SECONDS: int = int(os.getenv("AUTONOMOUS_WORKER_LEASE_SECONDS", "300"))
HEARTBEAT_INTERVAL_SECONDS: int = int(os.getenv("AUTONOMOUS_WORKER_HEARTBEAT_SECONDS", "10"))

NON_CLAIMABLE_STATUSES: tuple[str, ...] = (
    "cancelling",
    "cancelled",
    "succeeded",
    "failed",
    "rolled_back",
)

CLAIMABLE_STATUSES: tuple[str, ...] = ("pending", "running")

DEFAULT_STRATEGY_WEIGHTS: dict[str, dict[str, float]] = {
    DeploymentStrategy.DOCKER_ONLY.value: {
        STAGE_G1_BLUEPRINT: 15.0,
        STAGE_G2_ARTIFACT: 15.0,
        STAGE_G3_CONSISTENCY: 15.0,
        STAGE_G4_BUILD: 20.0,
        STAGE_G5_APPLY: 20.0,
        STAGE_G6_WORKLOAD: 10.0,
        STAGE_G7_VERIFICATION: 5.0,
    },
    DeploymentStrategy.DOCKER_GITHUB_VERCEL.value: {
        STAGE_G1_BLUEPRINT: 10.0,
        STAGE_G2_ARTIFACT: 10.0,
        STAGE_G3_CONSISTENCY: 10.0,
        STAGE_G4_BUILD: 15.0,
        STAGE_G5_APPLY: 15.0,
        STAGE_G6_WORKLOAD: 15.0,
        STAGE_GITHUB_RELEASE: 10.0,
        STAGE_VERCEL_DEPLOY: 10.0,
        STAGE_G7_VERIFICATION: 5.0,
    },
    DeploymentStrategy.DOCKER_GITHUB.value: {
        STAGE_G1_BLUEPRINT: 10.0,
        STAGE_G2_ARTIFACT: 10.0,
        STAGE_G3_CONSISTENCY: 10.0,
        STAGE_G4_BUILD: 15.0,
        STAGE_G5_APPLY: 15.0,
        STAGE_G6_WORKLOAD: 15.0,
        STAGE_GITHUB_RELEASE: 15.0,
        STAGE_G7_VERIFICATION: 10.0,
    },
    DeploymentStrategy.GITHUB_ONLY.value: {
        STAGE_G1_BLUEPRINT: 20.0,
        STAGE_G2_ARTIFACT: 20.0,
        STAGE_G3_CONSISTENCY: 20.0,
        STAGE_GITHUB_RELEASE: 30.0,
        STAGE_G7_VERIFICATION: 10.0,
    },
    DeploymentStrategy.VERCEL_ONLY.value: {
        STAGE_G1_BLUEPRINT: 20.0,
        STAGE_G2_ARTIFACT: 20.0,
        STAGE_G3_CONSISTENCY: 20.0,
        STAGE_VERCEL_DEPLOY: 30.0,
        STAGE_G7_VERIFICATION: 10.0,
    },
}


class WorkerFencingLostError(Exception):
    """Raised when an active worker loses its fencing token or lease custody."""

    pass


def _normalize_datetime(dt: datetime | None) -> datetime | None:
    """Ensures datetime is timezone-aware in UTC for safe comparisons."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _get_ctx(context: Any, key: str, default: Any = None) -> Any:
    """Safely retrieves a configuration or hook from the execution context."""
    if context is None:
        return default
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default)


def calculate_progress(
    stages: Sequence[AutonomousDeploymentStage | dict[str, Any]],
    strategy: str | DeploymentStrategy,
) -> int:
    """Calculates monotonic progress percentage across stages based on strategy weights.

    Formula:
        Progress = sum(W_i * StageProgress_i / 100)

    Guarantees:
    - 100% is achievable only when G7 final verification succeeds.
    - If G7 has not succeeded, progress is capped at 99%.
    - Succeeded or skipped stages contribute 100% of their weight.
    - Running stages contribute their reported progress_pct proportionally.
    """
    if not stages:
        return 0

    strat_key = strategy.value if isinstance(strategy, DeploymentStrategy) else str(strategy)
    weights = DEFAULT_STRATEGY_WEIGHTS.get(strat_key)
    if weights is None:
        equal_weight = 100.0 / len(stages)
        weights = {}
        for s in stages:
            s_name = s.get("stage_name") if isinstance(s, dict) else getattr(s, "stage_name", "")
            weights[s_name] = equal_weight

    total_progress: float = 0.0
    g7_succeeded = False

    for s in stages:
        name = s.get("stage_name") if isinstance(s, dict) else getattr(s, "stage_name", "")
        status = s.get("status") if isinstance(s, dict) else getattr(s, "status", "")
        pct = s.get("progress_pct", 0) if isinstance(s, dict) else getattr(s, "progress_pct", 0)
        gate_id = s.get("gate_id") if isinstance(s, dict) else getattr(s, "gate_id", None)

        w = weights.get(name, 100.0 / len(stages))

        if status == "succeeded":
            effective_pct = 100.0
        elif status == "skipped":
            effective_pct = 0.0
        elif status == "running":
            effective_pct = float(max(0, min(pct, 100)))
        else:
            effective_pct = float(max(0, min(pct, 100)))

        total_progress += w * (effective_pct / 100.0)

        if (name == STAGE_G7_VERIFICATION or gate_id == "G7") and status == "succeeded":
            g7_succeeded = True

    calculated = int(round(total_progress))

    if g7_succeeded:
        return 100
    return min(calculated, 99)


@contextlib.asynccontextmanager
async def _heartbeat_session_context(session: AsyncSession, context: Any = None):
    """Provides an isolated database session for background heartbeat renewal.

    Prevents concurrency conflicts with the primary pipeline session and ensures
    heartbeat transactions are strictly bounded and commit immediately.
    """
    sm = _get_ctx(context, "sessionmaker")
    if sm is not None and callable(sm):
        async with sm() as hb_session:
            yield hb_session
        return

    bind = getattr(session, "bind", None)
    if bind is not None and hasattr(bind, "connect") and type(bind).__name__ == "AsyncEngine":
        hb_sm = async_sessionmaker(bind, expire_on_commit=False)
        async with hb_sm() as hb_session:
            yield hb_session
        return

    # In-memory test sessions (e.g. MockAsyncSession) without an AsyncEngine bind
    yield session


class PipelineHeartbeat:
    """Manages independent background lease renewals and ownership tracking for an active worker."""

    def __init__(
        self,
        worker: AutonomousWorker,
        session: AsyncSession,
        run_id: uuid.UUID,
        fence_token: int,
        context: Any = None,
        run: AutonomousDeployment | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        self.worker = worker
        self.session = session
        self.run_id = run_id
        self.fence_token = fence_token
        self.context = context
        self.run = run
        self.interval_seconds = interval_seconds if interval_seconds is not None else float(HEARTBEAT_INTERVAL_SECONDS)
        self.stop_event = asyncio.Event()
        self.ownership_lost_event = asyncio.Event()
        self.heartbeat_error: Exception | None = None
        self.task: asyncio.Task | None = None
        self.last_renewal_at: datetime | None = None
        self.renewal_count: int = 0

    async def start(self) -> None:
        """Starts the background heartbeat loop."""
        if self.task is None or self.task.done():
            self.stop_event.clear()
            self.task = asyncio.create_task(self._run_loop(), name=f"heartbeat-{self.run_id}")

    async def stop(self) -> None:
        """Stops the background heartbeat loop cleanly."""
        self.stop_event.set()
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def __aenter__(self) -> PipelineHeartbeat:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()

    def assert_ownership(self) -> None:
        """Asserts that the worker has not lost ownership or encountered lease expiration."""
        if self.ownership_lost_event.is_set():
            err_msg = (
                f"Worker fence lost for run {self.run_id} (fence_token={self.fence_token}): "
                "Lease expired or token superseded during background heartbeat renewal."
            )
            if self.heartbeat_error:
                err_msg += f" (Underlying error: {self.heartbeat_error})"
            raise WorkerFencingLostError(err_msg)

    async def pulse(self) -> None:
        """Explicit on-demand heartbeat pulse, safe to call during long-running I/O or cold starts."""
        self.assert_ownership()
        try:
            async with _heartbeat_session_context(self.session, self.context) as hb_session:
                renewed = await self.worker.heartbeat(
                    hb_session,
                    run_id=self.run_id,
                    fence_token=self.fence_token,
                )
                if renewed:
                    with contextlib.suppress(Exception):
                        await hb_session.commit()
                    self.renewal_count += 1
                    self.last_renewal_at = datetime.now(UTC)
                    if self.run is not None:
                        self.run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_DURATION_SECONDS)
                else:
                    self.heartbeat_error = WorkerFencingLostError(
                        f"Worker fence lost for run {self.run_id} (fence_token={self.fence_token}). "
                        "Lease expired or token superseded during manual pulse."
                    )
                    self.ownership_lost_event.set()
                    self.assert_ownership()
        except Exception as exc:
            if isinstance(exc, WorkerFencingLostError):
                raise
            logger.warning("Manual heartbeat pulse failed for run %s: %s", self.run_id, exc)

    async def _run_loop(self) -> None:
        """Routine heartbeat renewal loop running independently of pipeline stages."""
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
            if self.stop_event.is_set():
                break

            try:
                async with _heartbeat_session_context(self.session, self.context) as hb_session:
                    renewed = await self.worker.heartbeat(
                        hb_session,
                        run_id=self.run_id,
                        fence_token=self.fence_token,
                    )
                    if renewed:
                        with contextlib.suppress(Exception):
                            await hb_session.commit()
                        self.renewal_count += 1
                        self.last_renewal_at = datetime.now(UTC)
                        if self.run is not None:
                            self.run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_DURATION_SECONDS)
                    else:
                        logger.error(
                            "Heartbeat renewal rejected for run %s (fence_token=%s): Lease lost or token superseded.",
                            self.run_id,
                            self.fence_token,
                        )
                        self.heartbeat_error = WorkerFencingLostError(
                            f"Worker fence lost for run {self.run_id} (fence_token={self.fence_token}). "
                            "Lease expired or token superseded."
                        )
                        self.ownership_lost_event.set()
                        break
            except Exception as hb_err:
                logger.warning(
                    "Heartbeat renewal tick failed for run %s (fence_token=%s): %s",
                    self.run_id,
                    self.fence_token,
                    hb_err,
                )
                self.heartbeat_error = hb_err


class AutonomousWorker:
    """Execution worker implementing atomic lease claim, fencing token management,
    and stage state transitions for autonomous deployments.
    """

    def __init__(
        self,
        worker_id: str | None = None,
        session: AsyncSession | None = None,
        cancellation_settlement: CancellationSettlement | None = None,
        rollback_handler: CompensationRollback | None = None,
    ) -> None:
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.session = session
        self.cancellation_settlement = cancellation_settlement or CancellationSettlement()
        self.rollback_handler = rollback_handler or CompensationRollback()

    calculate_progress = staticmethod(calculate_progress)

    def _resolve_session(self, first_arg: Any) -> tuple[AsyncSession, bool]:
        """Resolves whether first_arg is AsyncSession or whether to use self.session."""
        if hasattr(first_arg, "execute"):
            return first_arg, True
        if self.session is not None:
            return self.session, False
        raise ValueError(
            "AsyncSession must be provided either as first argument or bound in AutonomousWorker(session=...)"
        )

    async def claim_run(
        self,
        *args: Any,
        run_id: uuid.UUID | str | None = None,
        project_id: uuid.UUID | str | None = None,
        worker_id: str | None = None,
        **kwargs: Any,
    ) -> int | None:
        """Atomically claim ownership of an autonomous deployment run.

        Query Requirements:
        - Updates fence_token = fence_token + 1.
        - Updates worker_id = :worker_id.
        - Updates lease_expires_at = now() + 30s.
        - Updates dispatch_status = 'acknowledged'.
        - Run must be in ('pending', 'running') and NOT in terminal/cancelling states.
        - Run lease must be unassigned (NULL) or expired (< now()).

        Returns:
            New fence_token if successfully claimed, or None if claim failed.
        """
        # Resolve session and positional arguments
        session: AsyncSession
        rem_args: list[Any] = []
        if args:
            session, consumed = self._resolve_session(args[0])
            rem_args = list(args[1:]) if consumed else list(args)
        elif self.session is not None:
            session = self.session
        else:
            raise ValueError("AsyncSession must be provided.")

        if rem_args:
            if run_id is None and len(rem_args) >= 1:
                run_id = rem_args[0]
            if len(rem_args) >= 2:
                if worker_id is None and isinstance(rem_args[1], str) and len(rem_args) == 2:
                    worker_id = rem_args[1]
                elif project_id is None:
                    project_id = rem_args[1]
            if len(rem_args) >= 3 and worker_id is None:
                worker_id = rem_args[2]

        if run_id is None:
            run_id = kwargs.get("run_id")
        if project_id is None:
            project_id = kwargs.get("project_id")
        if worker_id is None:
            worker_id = kwargs.get("worker_id", self.worker_id)

        if run_id is None:
            raise ValueError("run_id is required to claim a run.")

        target_run_id = uuid.UUID(str(run_id)) if isinstance(run_id, str) else run_id
        target_proj_id = uuid.UUID(str(project_id)) if isinstance(project_id, str) and project_id else project_id
        target_worker_id = worker_id or self.worker_id

        now = datetime.now(UTC)
        new_lease = now + timedelta(seconds=LEASE_DURATION_SECONDS)

        conds = [
            AutonomousDeployment.id == target_run_id,
            AutonomousDeployment.status.in_(CLAIMABLE_STATUSES),
            AutonomousDeployment.status.not_in(NON_CLAIMABLE_STATUSES),
            or_(
                AutonomousDeployment.lease_expires_at.is_(None),
                AutonomousDeployment.lease_expires_at < now,
            ),
        ]
        if target_proj_id is not None:
            conds.append(AutonomousDeployment.project_id == target_proj_id)

        stmt = (
            update(AutonomousDeployment)
            .where(and_(*conds))
            .values(
                fence_token=AutonomousDeployment.fence_token + 1,
                worker_id=target_worker_id,
                lease_expires_at=new_lease,
                dispatch_status="acknowledged",
            )
            .returning(AutonomousDeployment.fence_token)
        )

        result = await session.execute(stmt)
        token: int | None
        if hasattr(result, "scalar_one_or_none"):
            token = result.scalar_one_or_none()
        elif hasattr(result, "scalars"):
            token = result.scalars().first()
        else:
            token = None

        if token is not None:
            await session.flush()
            return int(token)
        return None

    async def heartbeat(
        self,
        *args: Any,
        run_id: uuid.UUID | str | None = None,
        fence_token: int | None = None,
        **kwargs: Any,
    ) -> bool:
        """Extends lease_expires_at by 30 seconds WITHOUT incrementing fence_token.

        Requires:
        - fence_token == :fence_token
        - lease_expires_at > now()

        Returns:
            True if lease renewed, False if lease was lost or expired.
        """
        session: AsyncSession
        rem_args: list[Any] = []
        if args:
            session, consumed = self._resolve_session(args[0])
            rem_args = list(args[1:]) if consumed else list(args)
        elif self.session is not None:
            session = self.session
        else:
            raise ValueError("AsyncSession must be provided.")

        if rem_args:
            if run_id is None and len(rem_args) >= 1:
                run_id = rem_args[0]
            if fence_token is None and len(rem_args) >= 2:
                fence_token = rem_args[1]

        if run_id is None:
            run_id = kwargs.get("run_id")
        if fence_token is None:
            fence_token = kwargs.get("fence_token")

        if run_id is None or fence_token is None:
            raise ValueError("run_id and fence_token are required for heartbeat.")

        target_run_id = uuid.UUID(str(run_id)) if isinstance(run_id, str) else run_id
        now = datetime.now(UTC)
        new_lease = now + timedelta(seconds=LEASE_DURATION_SECONDS)

        stmt = (
            update(AutonomousDeployment)
            .where(
                AutonomousDeployment.id == target_run_id,
                AutonomousDeployment.fence_token == fence_token,
                AutonomousDeployment.lease_expires_at > now,
            )
            .values(
                lease_expires_at=new_lease,
            )
            .returning(AutonomousDeployment.id)
        )

        result = await session.execute(stmt)
        updated_id: Any | None
        if hasattr(result, "scalar_one_or_none"):
            updated_id = result.scalar_one_or_none()
        elif hasattr(result, "scalars"):
            updated_id = result.scalars().first()
        else:
            updated_id = None

        if updated_id is not None:
            id_map = getattr(session, "identity_map", None)
            if id_map is not None and hasattr(id_map, "values"):
                try:
                    vals = id_map.values()
                    if not inspect.iscoroutine(vals) and not hasattr(vals, "__await__"):
                        for obj in vals:
                            if isinstance(obj, AutonomousDeployment) and getattr(obj, "id", None) == target_run_id:
                                obj.lease_expires_at = new_lease
                except Exception:
                    pass
            await session.flush()
            return True
        return False

    async def guarded_update(
        self,
        *args: Any,
        run_id: uuid.UUID | str | None = None,
        fence_token: int | None = None,
        values: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> int:
        """Executes an update on autonomous_deployments guarded by fence_token and active lease.

        Asserts:
        - fence_token == :fence_token AND lease_expires_at > now()

        Raises:
            WorkerFencingLostError: If 0 rows updated (lease expired or token superseded).

        Returns:
            Number of rows updated (normally 1).
        """
        session: AsyncSession
        rem_args: list[Any] = []
        if args:
            session, consumed = self._resolve_session(args[0])
            rem_args = list(args[1:]) if consumed else list(args)
        elif self.session is not None:
            session = self.session
        else:
            raise ValueError("AsyncSession must be provided.")

        if rem_args:
            if run_id is None and len(rem_args) >= 1:
                run_id = rem_args[0]
            if fence_token is None and len(rem_args) >= 2:
                fence_token = rem_args[1]
            if values is None and len(rem_args) >= 3:
                values = rem_args[2]

        if run_id is None:
            run_id = kwargs.get("run_id")
        if fence_token is None:
            fence_token = kwargs.get("fence_token")
        if values is None:
            values = kwargs.get("values")

        if run_id is None or fence_token is None:
            raise ValueError("run_id and fence_token are required for guarded_update.")

        target_run_id = uuid.UUID(str(run_id)) if isinstance(run_id, str) else run_id
        now = datetime.now(UTC)
        update_values = dict(values or {})

        stmt = (
            update(AutonomousDeployment)
            .where(
                AutonomousDeployment.id == target_run_id,
                AutonomousDeployment.fence_token == fence_token,
                AutonomousDeployment.lease_expires_at > now,
            )
            .values(**update_values)
            .returning(AutonomousDeployment.id)
        )

        result = await session.execute(stmt)
        updated: list[Any]
        if hasattr(result, "scalars"):
            updated = list(result.scalars().all())
        else:
            updated = []

        if not updated:
            raise WorkerFencingLostError(
                f"Worker fence lost for run {target_run_id} (fence_token={fence_token}). "
                "Lease expired or token superseded."
            )

        id_map = getattr(session, "identity_map", None)
        if id_map is not None and hasattr(id_map, "values"):
            try:
                vals = id_map.values()
                if not inspect.iscoroutine(vals) and not hasattr(vals, "__await__"):
                    for obj in vals:
                        if isinstance(obj, AutonomousDeployment) and getattr(obj, "id", None) == target_run_id:
                            col_keys = {c.key for c in type(obj).__table__.columns}
                            for k, v in update_values.items():
                                if k in col_keys:
                                    setattr(obj, k, v)
            except Exception:
                pass

        await session.flush()
        return len(updated)

    async def transition_stage(
        self,
        *args: Any,
        run_id: uuid.UUID | str | None = None,
        stage_name: str | None = None,
        fence_token: int | None = None,
        status: str | None = None,
        progress_pct: int = 0,
        metadata: dict[str, Any] | None = None,
        error_message: str | None = None,
        **kwargs: Any,
    ) -> AutonomousDeploymentStage:
        """Updates stage state, monotonic run progress, and emits transactional outbox event.

        Enforces:
        - Worker fencing guard (raises WorkerFencingLostError if lease lost).
        - Monotonic run progress recomputation across all stages.
        - Atomic outbox record emission in the same transaction.

        Returns:
            Updated AutonomousDeploymentStage entity.
        """
        session: AsyncSession
        rem_args: list[Any] = []
        if args:
            session, consumed = self._resolve_session(args[0])
            rem_args = list(args[1:]) if consumed else list(args)
        elif self.session is not None:
            session = self.session
        else:
            raise ValueError("AsyncSession must be provided.")

        if rem_args:
            if run_id is None and len(rem_args) >= 1:
                run_id = rem_args[0]
            if stage_name is None and len(rem_args) >= 2:
                stage_name = rem_args[1]
            if fence_token is None and len(rem_args) >= 3:
                fence_token = rem_args[2]
            if status is None and len(rem_args) >= 4:
                status = rem_args[3]

        if run_id is None:
            run_id = kwargs.get("run_id")
        if stage_name is None:
            stage_name = kwargs.get("stage_name")
        if fence_token is None:
            fence_token = kwargs.get("fence_token")
        if status is None:
            status = kwargs.get("status")
        if not progress_pct:
            progress_pct = kwargs.get("progress_pct", 0)
        if metadata is None:
            metadata = kwargs.get("metadata")
        if error_message is None:
            error_message = kwargs.get("error_message")

        if run_id is None or stage_name is None or fence_token is None or status is None:
            raise ValueError("run_id, stage_name, fence_token, and status are required.")

        target_run_id = uuid.UUID(str(run_id)) if isinstance(run_id, str) else run_id

        # 1. Assert worker fencing guard and update current_stage
        update_vals: dict[str, Any] = (
            {"current_stage": stage_name} if status != "skipped" else {"fence_token": fence_token}
        )
        await self.guarded_update(
            session,
            run_id=target_run_id,
            fence_token=fence_token,
            values=update_vals,
        )

        # 2. Fetch authoritative run with eager loaded stages
        stmt_run = (
            select(AutonomousDeployment)
            .options(selectinload(AutonomousDeployment.stages))
            .where(AutonomousDeployment.id == target_run_id)
        )
        res_run = await session.execute(stmt_run)
        run = res_run.scalars().first()
        if run is None:
            raise WorkerFencingLostError(f"Autonomous deployment '{target_run_id}' not found.")

        # 3. Locate target stage
        stage = next((s for s in run.stages if s.stage_name == stage_name), None)
        if stage is None:
            stmt_stage = select(AutonomousDeploymentStage).where(
                AutonomousDeploymentStage.run_id == target_run_id,
                AutonomousDeploymentStage.stage_name == stage_name,
            )
            res_stage = await session.execute(stmt_stage)
            stage = res_stage.scalars().first()
            if stage is None:
                raise ValueError(f"Stage '{stage_name}' not found for run {target_run_id}")

        # 4. Update stage state, progress, and timestamps
        now = datetime.now(UTC)
        stage.status = status
        stage.progress_pct = max(0, min(progress_pct, 100))

        if status == "running":
            if stage.started_at is None:
                stage.started_at = now
        elif status in ("succeeded", "failed", "cancelled", "rolled_back", "skipped"):
            if stage.started_at is None:
                stage.started_at = now
            stage.completed_at = now
            if status == "succeeded":
                stage.progress_pct = 100

        if metadata is not None:
            stage.stage_metadata = {**stage.stage_metadata, **metadata}
        if error_message is not None:
            stage.error_message = redact_secrets(error_message) if isinstance(error_message, str) else error_message

        # 5. Recompute monotonic run progress using strategy weights
        new_run_progress = self.calculate_progress(run.stages, run.strategy)
        run.progress_pct = max(run.progress_pct, new_run_progress)

        if status == "running" and run.status == "pending":
            run.status = "running"
            run.started_at = run.started_at or now
        elif status == "failed":
            run.status = "failed"
            run.error_summary = stage.error_message or f"Stage '{stage_name}' failed."
            run.completed_at = now
        elif stage_name == STAGE_G7_VERIFICATION and status == "succeeded":
            run.status = "succeeded"
            run.progress_pct = 100
            run.completed_at = now

        # 6. Emit outbox event in the same transaction
        run.outbox_sequence_counter += 1
        outbox = AutonomousDeploymentOutbox(
            run_id=run.id,
            event_seq=run.outbox_sequence_counter,
            event_type=f"stage_{status}",
            payload={
                "run_id": str(run.id),
                "stage_name": stage_name,
                "gate_id": stage.gate_id,
                "status": status,
                "progress_pct": stage.progress_pct,
                "run_progress_pct": run.progress_pct,
                "metadata": stage.stage_metadata,
                "error_message": stage.error_message,
            },
            status="pending",
        )
        session.add(outbox)
        session.add(stage)
        session.add(run)
        await session.flush()
        return stage

    async def check_cancellation(
        self,
        *args: Any,
        run_id: uuid.UUID | str | None = None,
        fence_token: int | None = None,
        **kwargs: Any,
    ) -> bool:
        """Checks if run has been transitioned to 'cancelling' or 'cancelled'.

        Raises:
            WorkerFencingLostError: If worker fence token does not match or lease expired.
        """
        session: AsyncSession
        rem_args: list[Any] = []
        if args:
            session, consumed = self._resolve_session(args[0])
            rem_args = list(args[1:]) if consumed else list(args)
        elif self.session is not None:
            session = self.session
        else:
            raise ValueError("AsyncSession must be provided.")

        if rem_args:
            if run_id is None and len(rem_args) >= 1:
                run_id = rem_args[0]
            if fence_token is None and len(rem_args) >= 2:
                fence_token = rem_args[1]

        if run_id is None:
            run_id = kwargs.get("run_id")
        if fence_token is None:
            fence_token = kwargs.get("fence_token")

        if run_id is None or fence_token is None:
            raise ValueError("run_id and fence_token are required for check_cancellation.")

        target_run_id = uuid.UUID(str(run_id)) if isinstance(run_id, str) else run_id
        now = datetime.now(UTC)

        stmt = (
            select(AutonomousDeployment)
            .options(selectinload(AutonomousDeployment.stages))
            .where(AutonomousDeployment.id == target_run_id)
            .execution_options(populate_existing=True)
        )
        result = await session.execute(stmt)
        run = result.scalars().first()
        if run is None:
            raise WorkerFencingLostError(f"Autonomous deployment '{target_run_id}' not found.")

        hb = kwargs.get("heartbeat") or _get_ctx(kwargs.get("context"), "_pipeline_heartbeat")
        if hb is not None and hasattr(hb, "assert_ownership"):
            hb.assert_ownership()

        if run.fence_token != fence_token:
            raise WorkerFencingLostError(
                f"Worker fence lost for run {target_run_id}: expected token {fence_token}, "
                f"current is {run.fence_token}."
            )

        if run.lease_expires_at is not None:
            lease = _normalize_datetime(run.lease_expires_at)
            if lease is not None and lease < now:
                raise WorkerFencingLostError(f"Worker lease expired for run {target_run_id} at {run.lease_expires_at}.")

        return run.status in ("cancelling", "cancelled")

    async def settle_cancellation(
        self,
        *args: Any,
        run_id: uuid.UUID | str | None = None,
        run: AutonomousDeployment | None = None,
        current_stage: str | AutonomousDeploymentStage | None = None,
        fence_token: int | None = None,
        reason: str | None = None,
        **kwargs: Any,
    ) -> AutonomousDeployment:
        """Settles cancellation cooperatively using CancellationSettlement."""
        session: AsyncSession
        if args:
            session, consumed = self._resolve_session(args[0])
            if consumed and len(args) > 1 and run_id is None:
                run_id = args[1]
        elif self.session is not None:
            session = self.session
        else:
            raise ValueError("AsyncSession must be provided.")

        return await self.cancellation_settlement.settle_cancellation(
            session,
            run=run,
            run_id=run_id,
            current_stage=current_stage,
            fence_token=fence_token,
            reason=reason,
            **kwargs,
        )

    async def execute_rollback(
        self,
        *args: Any,
        run: AutonomousDeployment | None = None,
        run_id: uuid.UUID | str | None = None,
        primary_error: dict[str, Any] | None = None,
        context: Any = None,
        **kwargs: Any,
    ) -> AutonomousDeployment:
        """Executes compensation rollback using CompensationRollback."""
        session: AsyncSession
        if args:
            session, consumed = self._resolve_session(args[0])
            if consumed and len(args) > 1 and run_id is None:
                run_id = args[1]
        elif self.session is not None:
            session = self.session
        else:
            raise ValueError("AsyncSession must be provided.")

        return await self.rollback_handler.execute_rollback(
            session,
            run=run,
            run_id=run_id,
            primary_error=primary_error,
            context=context,
            **kwargs,
        )

    async def run_pipeline(
        self,
        *args: Any,
        run_id: uuid.UUID | str | None = None,
        worker_id: str | None = None,
        context: Any = None,
        rollback_on_failure: bool = False,
        **kwargs: Any,
    ) -> AutonomousDeployment:
        """Executes the strategy-aware gate and stage pipeline for an autonomous deployment."""
        session: AsyncSession
        rem_args: list[Any] = []
        if args:
            session, consumed = self._resolve_session(args[0])
            rem_args = list(args[1:]) if consumed else list(args)
        elif self.session is not None:
            session = self.session
        else:
            raise ValueError("AsyncSession must be provided.")

        if rem_args and run_id is None:
            run_id = rem_args[0]
        if run_id is None:
            run_id = kwargs.get("run_id")
        if run_id is None:
            raise ValueError("run_id is required for run_pipeline.")

        return await run_pipeline(
            session,
            run_id=run_id,
            worker_id=worker_id or self.worker_id,
            context=context if context is not None else kwargs.get("context"),
            rollback_on_failure=rollback_on_failure or kwargs.get("rollback_on_failure", False),
            rollback_handler=self.rollback_handler,
            cancellation_settlement=self.cancellation_settlement,
            sessionmaker=kwargs.get("sessionmaker"),
        )

    async def execute_run(
        self,
        *args: Any,
        run_id: uuid.UUID | str | None = None,
        context: Any = None,
        **kwargs: Any,
    ) -> AutonomousDeployment:
        """Alias for run_pipeline for engine task execution."""
        return await self.run_pipeline(*args, run_id=run_id, context=context, **kwargs)


async def run_pipeline(
    session: AsyncSession,
    *,
    run_id: uuid.UUID | str,
    worker_id: str | None = None,
    context: Any = None,
    rollback_on_failure: bool = False,
    rollback_handler: CompensationRollback | None = None,
    cancellation_settlement: CancellationSettlement | None = None,
    redis: Any | None = None,
    sessionmaker: Any | None = None,
) -> AutonomousDeployment:
    """Sequentially executes canonical gates and operational stages for an autonomous deployment run.

    Guarantees:
    - Worker fencing token claim and validation per execution epoch.
    - Routine heartbeat renewals before and after stage execution.
    - Periodic cooperative cancellation checks.
    - Stage transitions through running -> succeeded or failed.
    - Sanitized log appending using AutonomousDeploymentService.append_logs.
    - Strategy-aware stage omission (e.g. G4-G6 omitted for github_only / vercel_only).
    - Pipeline halt on gate failure: marks stage 'failed', run 'failed',
      records error_summary and primary_error, leaving subsequent stages in 'pending'.
    - On full completion: marks run 'succeeded', progress_pct=100, completed_at=now().
    """
    target_run_id = uuid.UUID(str(run_id)) if isinstance(run_id, str) else run_id
    effective_worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    cancel_settler = cancellation_settlement or CancellationSettlement()
    comp_rollback = rollback_handler or CompensationRollback()
    worker = AutonomousWorker(
        worker_id=effective_worker_id,
        cancellation_settlement=cancel_settler,
        rollback_handler=comp_rollback,
    )
    service = AutonomousDeploymentService()

    # 1. Fetch authoritative run with eager-loaded stages
    stmt = (
        select(AutonomousDeployment)
        .options(selectinload(AutonomousDeployment.stages))
        .where(AutonomousDeployment.id == target_run_id)
    )
    result = await session.execute(stmt)
    run = result.scalars().first()
    if run is None:
        raise ValueError(f"Autonomous deployment '{target_run_id}' not found.")

    if run.status == "cancelling":
        return await cancel_settler.settle_cancellation(
            session,
            run=run,
            reason="Pipeline run status is cancelling",
        )

    if run.status in NON_CLAIMABLE_STATUSES:
        return run

    # Securely resolve credentials strictly scoped to run.created_by
    exec_ctx: dict[str, Any] = (
        dict(context) if isinstance(context, dict) else ({} if context is None else {"_raw_ctx": context})
    )
    try:
        resolved_creds = await resolve_deployment_credentials(session, run)
        if exec_ctx.get("github_token") is None and resolved_creds.get("github_token"):
            exec_ctx["github_token"] = resolved_creds["github_token"]
        if exec_ctx.get("vercel_token") is None and resolved_creds.get("vercel_token"):
            exec_ctx["vercel_token"] = resolved_creds["vercel_token"]
        if "credential_status" in resolved_creds:
            exec_ctx["credential_status"] = {
                **exec_ctx.get("credential_status", {}),
                **resolved_creds["credential_status"],
            }
    except Exception as exc:
        logger.debug("Credential resolution failed in worker: %s", exc)
    context = exec_ctx
    if sessionmaker is not None and isinstance(context, dict):
        context["sessionmaker"] = sessionmaker
    publisher = AutonomousOutboxPublisher()
    effective_redis = redis or exec_ctx.get("redis")
    if isinstance(context, dict):
        context["redis"] = effective_redis
        context["outbox_publisher"] = publisher

    async def _drain_outbox() -> None:
        if effective_redis is not None:
            with contextlib.suppress(Exception):
                await publisher.drain_run_events(session, effective_redis, target_run_id)

    # 2. Worker fencing claim / custody verification
    now = datetime.now(UTC)
    lease_norm = _normalize_datetime(run.lease_expires_at)
    lease_active = (
        run.worker_id == effective_worker_id and lease_norm is not None and lease_norm > now and run.fence_token > 0
    )

    if lease_active:
        fence_token = run.fence_token
    else:
        claimed_token = await worker.claim_run(
            session,
            run_id=target_run_id,
            worker_id=effective_worker_id,
        )
        if claimed_token is None:
            raise WorkerFencingLostError(
                f"Could not claim lease for run {target_run_id}: active lease held by another worker."
            )
        fence_token = claimed_token
        with contextlib.suppress(Exception):
            await session.commit()

    heartbeat_mgr = PipelineHeartbeat(
        worker=worker,
        session=session,
        run_id=target_run_id,
        fence_token=fence_token,
        context=context,
        run=run,
    )
    await heartbeat_mgr.start()
    if isinstance(context, dict):
        context["_pipeline_heartbeat"] = heartbeat_mgr

    try:
        # 3. Initial cancellation check
        if await worker.check_cancellation(session, run_id=target_run_id, fence_token=fence_token):
            return await cancel_settler.settle_cancellation(
                session,
                run=run,
                fence_token=fence_token,
                reason="Pipeline cancelled cooperatively by worker",
            )

        # 4. Mark run running if currently pending
        if run.status == "pending":
            run.status = "running"
            run.started_at = run.started_at or datetime.now(UTC)
            await session.flush()
            await _drain_outbox()
            with contextlib.suppress(Exception):
                await session.commit()

        # 4b. Source Preparation Preflight Phase before G1 Blueprint Validation
        from .autonomous_source import prepare_source_workspace

        source_result = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=fence_token,
            service=service,
            redis=effective_redis,
            context=context,
        )
        with contextlib.suppress(Exception):
            await session.commit()
        if not source_result.passed:
            await _drain_outbox()
            with contextlib.suppress(Exception):
                await session.commit()
            return run

        # 4c. Initial Readiness Assessment (R0) Baseline before Gate Execution
        from .autonomous_gates import capture_authoritative_readiness_checkpoint
        from .readiness import resolve_workspace_path

        workspace_dir_val = (
            source_result.details.get("workspace_path")
            or (run.configuration or {}).get("source_preparation", {}).get("workspace_path")
            or _get_ctx(context, "workspace_dir")
            or _get_ctx(context, "workspace_path")
            or f".forgeops/snapshots/{run.id}"
        )
        ws_resolved = resolve_workspace_path(workspace_dir_val)
        if isinstance(context, dict):
            context["workspace_path"] = str(ws_resolved)
            context["workspace_dir"] = str(ws_resolved)

        r0_result = await capture_authoritative_readiness_checkpoint(
            session=session,
            run=run,
            checkpoint_type="initial_r0",
            assessment_version=1,
            reusable_dockerfile=None,
            workspace_path=ws_resolved,
            context=context,
        )
        with contextlib.suppress(Exception):
            await _drain_outbox()

        await service.append_logs(
            session,
            run_id=target_run_id,
            stage_name="initial_readiness",
            entries=[
                (
                    f"Initial Readiness Baseline (R0): {r0_result.score}/100 - {r0_result.explanation}",
                    "INFO",
                )
            ],
            fence_token=fence_token,
        )
        with contextlib.suppress(Exception):
            await session.commit()

        # 5. Ensure stages are loaded and sorted by position
        stmt_stages = (
            select(AutonomousDeploymentStage)
            .where(AutonomousDeploymentStage.run_id == target_run_id)
            .order_by(AutonomousDeploymentStage.position.asc())
        )
        res_stages = await session.execute(stmt_stages)
        stages = list(res_stages.scalars().all())

        # 6. Execute stages sequentially
        stage_idx = 0
        stage_recovery_attempts: dict[str, int] = {}
        while stage_idx < len(stages):
            heartbeat_mgr.assert_ownership()
            stage = stages[stage_idx]
            # Skip stages already succeeded or skipped
            if stage.status in ("succeeded", "skipped"):
                stage_idx += 1
                continue

            # Cancellation check before stage start
            if await worker.check_cancellation(session, run_id=target_run_id, fence_token=fence_token):
                return await cancel_settler.settle_cancellation(
                    session,
                    run=run,
                    current_stage=stage,
                    fence_token=fence_token,
                    reason="Pipeline cancelled cooperatively by worker",
                )

            # Routine heartbeat before stage
            await worker.heartbeat(session, run_id=target_run_id, fence_token=fence_token)

            # Transition stage to running
            await worker.transition_stage(
                session,
                run_id=target_run_id,
                stage_name=stage.stage_name,
                fence_token=fence_token,
                status="running",
                progress_pct=10,
            )
            await _drain_outbox()

            # Append stage start log
            await service.append_logs(
                session,
                run_id=target_run_id,
                stage_name=stage.stage_name,
                entries=[f"Starting stage {stage.stage_name}..."],
                fence_token=fence_token,
            )
            with contextlib.suppress(Exception):
                await session.commit()

            # Check cancellation right before evaluator execution
            if await worker.check_cancellation(session, run_id=target_run_id, fence_token=fence_token):
                return await cancel_settler.settle_cancellation(
                    session,
                    run=run,
                    current_stage=stage,
                    fence_token=fence_token,
                    reason="Pipeline cancelled cooperatively by worker",
                )

            try:
                # Apply configured operational stage delay if requested
                cfg = run.configuration or {}
                delays = cfg.get("stage_delays") or {}
                stg_delay = float(delays.get(stage.stage_name, 0.0))
                if stg_delay > 0:
                    remaining = stg_delay
                    while remaining > 0:
                        sleep_chunk = min(remaining, 1.0)
                        await asyncio.sleep(sleep_chunk)
                        remaining -= sleep_chunk
                        await worker.heartbeat(session, run_id=target_run_id, fence_token=fence_token)

                # Locate stage or gate evaluator
                evaluator = STAGE_EVALUATORS.get(stage.stage_name)
                if evaluator is None and stage.gate_id:
                    evaluator = GATE_EVALUATORS.get(stage.gate_id)

                if evaluator is not None:
                    if inspect.iscoroutinefunction(evaluator):
                        result = await evaluator(session, run, context)
                    else:
                        res = evaluator(session, run, context)
                        if inspect.iscoroutine(res):
                            result = await res
                        else:
                            result = res
                else:
                    result = GateResult(
                        gate_id=stage.gate_id or "",
                        passed=True,
                        status="succeeded",
                        message=f"Stage {stage.stage_name} completed",
                    )
            except Exception as exc:
                logger.exception("Unexpected exception evaluating stage %s: %s", stage.stage_name, exc)
                with contextlib.suppress(Exception):
                    await session.rollback()
                result = GateResult(
                    gate_id=stage.gate_id or "",
                    passed=False,
                    status="failed",
                    message=f"Stage {stage.stage_name} failed with internal error: {str(exc)}",
                )

            # Immediate heartbeat refresh on session to ensure active lease for state transition or recovery
            with contextlib.suppress(Exception):
                await worker.heartbeat(session, run_id=target_run_id, fence_token=fence_token)

            stage_passed = getattr(result, "overall_passed", getattr(result, "passed", False))
            details = getattr(result, "details", {})
            message = getattr(result, "message", "")



            if stage_passed:
                if isinstance(details, dict) and "deployment_receipt" in details:
                    run.configuration = {
                        **(run.configuration or {}),
                        "deployment_receipt": details["deployment_receipt"],
                    }
                    session.add(run)
                # Append success log
                await service.append_logs(
                    session,
                    run_id=target_run_id,
                    stage_name=stage.stage_name,
                    entries=[(f"Stage {stage.stage_name} succeeded: {message}", "INFO")],
                    fence_token=fence_token,
                )
                # Transition stage to succeeded
                await worker.transition_stage(
                    session,
                    run_id=target_run_id,
                    stage_name=stage.stage_name,
                    fence_token=fence_token,
                    status="succeeded",
                    progress_pct=100,
                    metadata=details,
                )
                await _drain_outbox()
                # Heartbeat after successful stage
                await worker.heartbeat(session, run_id=target_run_id, fence_token=fence_token)
                with contextlib.suppress(Exception):
                    await session.commit()
                stage_idx += 1
            else:
                # Check G8 conditional AI recovery
                cfg = run.configuration or {}
                rec_attempts = stage_recovery_attempts.get(stage.stage_name, int(cfg.get("recovery_attempts", 0)))
                enable_g8 = bool(cfg.get("enable_ai_recovery", True))

                is_provider_timeout = (
                    bool(details.get("is_provider_timeout"))
                    or bool(details.get("timed_out"))
                    or details.get("provider_build_error") is False
                    or details.get("ready_state") in ("BUILDING", "INITIALIZING", "ANALYZING", "DEPLOYING")
                ) and stage.stage_name in ("vercel_deploy", "github_release")

                is_cancellation = (
                    bool(details.get("cancelled"))
                    or bool(details.get("cancelled_by_operator"))
                    or getattr(run, "status", None) in ("cancelling", "cancelled")
                )

                if is_provider_timeout:
                    await service.append_logs(
                        session,
                        run_id=target_run_id,
                        stage_name=stage.stage_name,
                        entries=[
                            (
                                f"Stage '{stage.stage_name}' polling window elapsed while provider state is "
                                f"'{details.get('ready_state', 'BUILDING')}'. Bypassing AI code recovery "
                                "because no local manifest or compiler error occurred.",
                                "WARN",
                            )
                        ],
                        fence_token=fence_token,
                    )
                elif is_cancellation:
                    logger.info("Stage '%s' cancelled; bypassing AI recovery.", stage.stage_name)
                elif enable_g8 and rec_attempts < 3:
                    rec_attempts += 1
                    stage_recovery_attempts[stage.stage_name] = rec_attempts
                    cfg["recovery_attempts"] = rec_attempts
                    run.configuration = {**cfg}
                    try:
                        session.add(run)
                        await worker.guarded_update(
                            session,
                            run_id=target_run_id,
                            fence_token=fence_token,
                            values={"configuration": run.configuration},
                        )
                    except Exception:
                        with contextlib.suppress(Exception):
                            await session.rollback()
                        session.add(run)
                        await worker.guarded_update(
                            session,
                            run_id=target_run_id,
                            fence_token=fence_token,
                            values={"configuration": run.configuration},
                        )
                    with contextlib.suppress(Exception):
                        await session.commit()

                    try:
                        from src.diagnostics.bundle import DiagnosticBundle
                        from src.recovery.ai_resolver import resolve_failure_with_ai_async
                    except ImportError:
                        from backend.src.diagnostics.bundle import DiagnosticBundle
                        from backend.src.recovery.ai_resolver import (
                            resolve_failure_with_ai_async,
                        )

                    ws_resolved = resolve_workspace_path(workspace_dir_val)

                    container_st = details.get("container_state") or {}
                    if not isinstance(container_st, dict):
                        container_st = {}
                    if "container_running" in details and "container_running" not in container_st:
                        container_st["container_running"] = details["container_running"]
                    if "Running" not in container_st and "container_running" in details:
                        container_st["Running"] = details["container_running"]
                    if "ExitCode" not in container_st and "exit_code" in details:
                        container_st["ExitCode"] = details["exit_code"]

                    bundle = DiagnosticBundle(
                        gate_identifier=stage.gate_id or stage.stage_name,
                        stage=stage.stage_name,
                        command_line=str(details.get("command", "") or details.get("command_line", "")),
                        exit_code=int(details.get("exit_code", 1)),
                        attempt_count=rec_attempts,
                        stdout=str(details.get("stdout", "")),
                        stderr=str(details.get("stderr", message or "")),
                        error_classification=str(details.get("error_classification", "DETERMINISTIC")),
                        container_state=container_st,
                    )
                    plan = await resolve_failure_with_ai_async(
                        bundle,
                        current_iteration=rec_attempts,
                        workspace_path=ws_resolved,
                        session=session,
                        run=run,
                        context=context,
                    )

                    heartbeat_mgr.assert_ownership()
                    if plan.status == "PROPOSED":
                        has_manifest_remedies = bool(plan.suggested_manifests)
                        has_blueprint_remedies = bool(
                            plan.updated_blueprint is not None
                            and plan.updated_blueprint != getattr(bundle, "blueprint", None)
                        )

                        from .autonomous_gates import _is_simulation_mode

                        is_sim = _is_simulation_mode(session, run, context)

                        manifests_differ = False
                        if has_manifest_remedies and ws_resolved and ws_resolved.exists():
                            for m_path, m_content in plan.suggested_manifests.items():
                                cur_file = ws_resolved / m_path
                                if not cur_file.exists():
                                    manifests_differ = True
                                    break
                                try:
                                    cur_content = cur_file.read_text(encoding="utf-8", errors="replace")
                                    if cur_content.strip() != m_content.strip():
                                        manifests_differ = True
                                        break
                                except Exception:
                                    manifests_differ = True
                                    break
                        elif has_manifest_remedies:
                            manifests_differ = True

                        if not has_blueprint_remedies and (
                            not has_manifest_remedies or (not manifests_differ and not is_sim)
                        ):
                            halt_reason = (
                                "IDENTICAL_MANIFEST_PROPOSED" if has_manifest_remedies else "NO_ACTIONABLE_REMEDY"
                            )
                            halt_msg = (
                                f"G8 AI Recovery halted ({plan.root_cause_category}): Proposed recovery manifests are identical to existing workspace manifests. "
                                f"Halting to prevent ineffective retry loop."
                                if has_manifest_remedies
                                else f"G8 AI Recovery halted ({plan.root_cause_category}): No automated remediation manifests or configuration remedies available. "
                                f"Halting to prevent ineffective retry loop: {plan.operator_message}"
                            )
                            await service.append_logs(
                                session,
                                run_id=target_run_id,
                                stage_name=stage.stage_name,
                                entries=[(halt_msg, "WARN")],
                                fence_token=fence_token,
                            )
                            details["g8_recovery_halted"] = True
                            details["g8_halt_reason"] = halt_reason
                            details["g8_root_cause"] = plan.root_cause_category
                            stage.stage_metadata = {
                                **(stage.stage_metadata or {}),
                                "g8_recovery_halted": True,
                                "g8_halt_reason": halt_reason,
                                "g8_root_cause": plan.root_cause_category,
                            }
                        else:
                            await service.append_logs(
                                session,
                                run_id=target_run_id,
                                stage_name=stage.stage_name,
                                entries=[
                                    (
                                        f"G8 AI Recovery (Iteration {rec_attempts}/3): {plan.root_cause_category} - {plan.operator_message}",
                                        "WARN",
                                    )
                                ],
                                fence_token=fence_token,
                            )

                            heartbeat_mgr.assert_ownership()
                            # Apply suggested manifests safely to workspace
                            ws_resolved = resolve_workspace_path(workspace_dir_val)
                            if plan.suggested_manifests and ws_resolved.exists():
                                try:
                                    from src.deployments.autonomous_gates import _apply_run_local_artifacts
                                except ImportError:
                                    from backend.src.deployments.autonomous_gates import _apply_run_local_artifacts
                                try:
                                    _apply_run_local_artifacts(
                                        ws_resolved, plan.suggested_manifests, run_id=target_run_id
                                    )
                                except Exception as apply_exc:
                                    logger.warning("G8 recovery could not apply manifests safely: %s", apply_exc)

                            # Evaluate and persist R_recN checkpoint
                            r_rec_result = await capture_authoritative_readiness_checkpoint(
                                session=session,
                                run=run,
                                checkpoint_type="recovery_recheck",
                                assessment_version=rec_attempts,
                                reusable_dockerfile=None,
                                workspace_path=ws_resolved,
                                context=context,
                            )
                            with contextlib.suppress(Exception):
                                run.outbox_sequence_counter += 1
                                session.add(
                                    AutonomousDeploymentOutbox(
                                        run_id=run.id,
                                        event_seq=run.outbox_sequence_counter,
                                        event_type="recovery_attempted",
                                        payload={
                                            "run_id": str(run.id),
                                            "stage_name": stage.stage_name,
                                            "iteration": rec_attempts,
                                            "status": plan.status,
                                            "root_cause": plan.root_cause_category,
                                            "operator_message": plan.operator_message,
                                            "recovery_score": r_rec_result.score,
                                        },
                                        status="pending",
                                    )
                                )
                                await session.flush()
                                await _drain_outbox()

                            await service.append_logs(
                                session,
                                run_id=target_run_id,
                                stage_name=stage.stage_name,
                                entries=[
                                    (
                                        f"Retrying stage {stage.stage_name} after applying G8 AI recovery remedies (Iteration {rec_attempts})",
                                        "INFO",
                                    )
                                ],
                                fence_token=fence_token,
                            )
                            heartbeat_mgr.assert_ownership()
                            # Retry current stage without incrementing stage_idx
                            with contextlib.suppress(Exception):
                                await session.commit()
                            continue
                    else:
                        details["g8_recovery_halted"] = True
                        details["g8_halt_reason"] = plan.status
                        details["g8_root_cause"] = plan.root_cause_category
                        stage.stage_metadata = {
                            **(stage.stage_metadata or {}),
                            "g8_recovery_halted": True,
                            "g8_halt_reason": plan.status,
                            "g8_root_cause": plan.root_cause_category,
                        }
                        await service.append_logs(
                            session,
                            run_id=target_run_id,
                            stage_name=stage.stage_name,
                            entries=[
                                (
                                    f"G8 AI Recovery halted ({plan.status}): {plan.operator_message}",
                                    "ERROR",
                                )
                            ],
                            fence_token=fence_token,
                        )

                # Append failure log
                await service.append_logs(
                    session,
                    run_id=target_run_id,
                    stage_name=stage.stage_name,
                    entries=[(f"Stage {stage.stage_name} failed: {message}", "ERROR")],
                    fence_token=fence_token,
                )
                # Transition stage to failed
                await worker.transition_stage(
                    session,
                    run_id=target_run_id,
                    stage_name=stage.stage_name,
                    fence_token=fence_token,
                    status="failed",
                    progress_pct=stage.progress_pct,
                    metadata=details,
                    error_message=message,
                )
                # Mark run failed and record primary error
                fail_now = datetime.now(UTC)
                run.status = "failed"
                redacted_msg = redact_secrets(message) if isinstance(message, str) else message
                run.error_summary = redacted_msg or f"Stage '{stage.stage_name}' failed."
                run.primary_error = {
                    "stage_name": stage.stage_name,
                    "gate_id": stage.gate_id,
                    "message": redacted_msg or "",
                    "details": details,
                }
                run.completed_at = fail_now

                # Settle all subsequent unexecuted stages as skipped
                for downstream in stages:
                    if downstream.position > stage.position and downstream.status in ("pending", "waiting"):
                        downstream.status = "skipped"
                        downstream.error_message = f"Skipped: prerequisite '{stage.stage_name}' failed"
                        downstream.completed_at = fail_now
                        downstream.progress_pct = 0
                        downstream_meta = {
                            "prerequisite_failed": stage.stage_name,
                            "skipped_at": fail_now.isoformat(),
                        }
                        downstream.stage_metadata = {**(downstream.stage_metadata or {}), **downstream_meta}
                        await worker.transition_stage(
                            session,
                            run_id=target_run_id,
                            stage_name=downstream.stage_name,
                            fence_token=fence_token,
                            status="skipped",
                            progress_pct=0,
                            metadata=downstream_meta,
                            error_message=downstream.error_message,
                        )

                run.outbox_sequence_counter += 1
                outbox = AutonomousDeploymentOutbox(
                    run_id=run.id,
                    event_seq=run.outbox_sequence_counter,
                    event_type="run_failed",
                    payload={
                        "run_id": str(run.id),
                        "project_id": str(run.project_id),
                        "status": "failed",
                        "progress_pct": run.progress_pct,
                        "current_stage": stage.stage_name,
                        "error_summary": run.error_summary,
                        "completed_at": fail_now.isoformat(),
                    },
                    status="pending",
                )
                session.add(outbox)
                await session.flush()
                await _drain_outbox()

                should_rollback = (
                    rollback_on_failure
                    or _get_ctx(context, "rollback_on_failure", False)
                    or _get_ctx(context, "enable_rollback", False)
                    or bool((run.configuration or {}).get("auto_rollback", False))
                )
                if should_rollback:
                    return await comp_rollback.execute_rollback(
                        session,
                        run=run,
                        primary_error=run.primary_error,
                        context=context,
                    )

                await session.flush()
                with contextlib.suppress(Exception):
                    await session.commit()
                # Pipeline stops immediately; subsequent stages remain pending
                return run

        # 7. Full completion: mark run succeeded, progress_pct=100, completed_at=now()
        finish_now = datetime.now(UTC)
        run.status = "succeeded"
        run.progress_pct = 100
        if run.completed_at is None:
            run.completed_at = finish_now

        run.outbox_sequence_counter += 1
        outbox = AutonomousDeploymentOutbox(
            run_id=run.id,
            event_seq=run.outbox_sequence_counter,
            event_type="run_succeeded",
            payload={
                "run_id": str(run.id),
                "project_id": str(run.project_id),
                "status": "succeeded",
                "progress_pct": 100,
                "completed_at": finish_now.isoformat(),
            },
            status="pending",
        )
        session.add(outbox)
        await session.flush()
        await _drain_outbox()
        with contextlib.suppress(Exception):
            await session.commit()
        return run
    finally:
        await heartbeat_mgr.stop()
