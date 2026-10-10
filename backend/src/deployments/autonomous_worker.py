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

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentOutbox,
    AutonomousDeploymentStage,
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
)

logger = logging.getLogger(__name__)

LEASE_DURATION_SECONDS: int = 30
HEARTBEAT_INTERVAL_SECONDS: int = 10

NON_CLAIMABLE_STATUSES: tuple[str, ...] = (
    "cancelling",
    "cancelled",
    "succeeded",
    "failed",
    "rolled_back",
)

CLAIMABLE_STATUSES: tuple[str, ...] = ("pending", "running")

DEFAULT_STRATEGY_WEIGHTS: dict[str, dict[str, float]] = {
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
        return dt.replace(tzinfo=timezone.utc)
    return dt


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

        if status in ("succeeded", "skipped"):
            effective_pct = 100.0
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


class AutonomousWorker:
    """Execution worker implementing atomic lease claim, fencing token management,
    and stage state transitions for autonomous deployments.
    """

    def __init__(
        self,
        worker_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.session = session

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

        now = datetime.now(timezone.utc)
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
        now = datetime.now(timezone.utc)
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
        now = datetime.now(timezone.utc)
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
        await self.guarded_update(
            session,
            run_id=target_run_id,
            fence_token=fence_token,
            values={"current_stage": stage_name},
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
        now = datetime.now(timezone.utc)
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
            stage.error_message = error_message

        # 5. Recompute monotonic run progress using strategy weights
        new_run_progress = self.calculate_progress(run.stages, run.strategy)
        run.progress_pct = max(run.progress_pct, new_run_progress)

        if status == "running" and run.status == "pending":
            run.status = "running"
            run.started_at = run.started_at or now
        elif status == "failed":
            run.status = "failed"
            run.error_summary = error_message or f"Stage '{stage_name}' failed."
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
        now = datetime.now(timezone.utc)

        stmt = select(AutonomousDeployment).where(AutonomousDeployment.id == target_run_id)
        result = await session.execute(stmt)
        run = result.scalars().first()
        if run is None:
            raise WorkerFencingLostError(f"Autonomous deployment '{target_run_id}' not found.")

        if run.fence_token != fence_token:
            raise WorkerFencingLostError(
                f"Worker fence lost for run {target_run_id}: expected token {fence_token}, "
                f"current is {run.fence_token}."
            )

        if run.lease_expires_at is not None:
            lease = _normalize_datetime(run.lease_expires_at)
            if lease is not None and lease < now:
                raise WorkerFencingLostError(
                    f"Worker lease expired for run {target_run_id} at {run.lease_expires_at}."
                )

        return run.status in ("cancelling", "cancelled")
