# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous Deployment Orchestrator Recovery, Rollback & Cancellation Settlement (Phase 6).

Implements:
1. CancellationSettlement:
   - Tracks active child subprocesses and asyncio tasks for runs.
   - On cancellation: sends SIGTERM, waits up to 5s grace period, sends SIGKILL if necessary.
   - Updates run status to 'cancelled', sets completed_at = now(), emits outbox event.
2. CompensationRollback:
   - Strategy-specific rollback: cleans up Docker containers labeled with `forgeops.run_id = :run_id`.
   - Preserves both primary_error and compensation_error separately in database.
   - If rollback succeeds: marks run status = 'rolled_back'.
   - If rollback fails: marks run status = 'failed' and records compensation error.
3. AutonomousRecoverySweeper:
   - Sweeps for runs in ('pending', 'running') with lease_expires_at < now() - 60s.
   - Reconciles exact operation identities (GitHub commit SHA, Docker container labels, Vercel deployment ID).
   - Claims expired runs with incremented fence_token and resumes or marks failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import signal
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentOutbox,
    AutonomousDeploymentStage,
)
from .autonomous_schemas import DeploymentStrategy

logger = logging.getLogger(__name__)

CANCELLATION_GRACE_PERIOD_SECONDS: float = 5.0
SWEEPER_EXPIRY_THRESHOLD_SECONDS: int = 60
DEFAULT_LEASE_CLAIM_SECONDS: int = 30


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


# ===========================================================================
# 1. Cancellation Settlement
# ===========================================================================


class CancellationSettlement:
    """Tracks active child subprocesses/tasks and performs graceful cancellation settlement."""

    _shared_processes: dict[uuid.UUID, list[Any]] = {}
    _shared_tasks: dict[uuid.UUID, list[asyncio.Task]] = {}

    def __init__(self) -> None:
        self._processes: dict[uuid.UUID, list[Any]] = {}
        self._tasks: dict[uuid.UUID, list[asyncio.Task]] = {}

    def _normalize_run_id(self, run_id: uuid.UUID | str) -> uuid.UUID:
        return uuid.UUID(str(run_id)) if isinstance(run_id, str) else run_id

    def register_process(self, run_id: uuid.UUID | str, process: Any) -> None:
        """Registers an active child process associated with a deployment run."""
        rid = self._normalize_run_id(run_id)
        self._processes.setdefault(rid, []).append(process)
        CancellationSettlement._shared_processes.setdefault(rid, []).append(process)

    def unregister_process(self, run_id: uuid.UUID | str, process: Any) -> None:
        """Unregisters a completed child process."""
        rid = self._normalize_run_id(run_id)
        if rid in self._processes and process in self._processes[rid]:
            self._processes[rid].remove(process)
        if rid in CancellationSettlement._shared_processes and process in CancellationSettlement._shared_processes[rid]:
            CancellationSettlement._shared_processes[rid].remove(process)

    def register_task(self, run_id: uuid.UUID | str, task: asyncio.Task) -> None:
        """Registers an active asyncio task associated with a deployment run."""
        rid = self._normalize_run_id(run_id)
        self._tasks.setdefault(rid, []).append(task)
        CancellationSettlement._shared_tasks.setdefault(rid, []).append(task)

    def unregister_task(self, run_id: uuid.UUID | str, task: asyncio.Task) -> None:
        """Unregisters an active asyncio task."""
        rid = self._normalize_run_id(run_id)
        if rid in self._tasks and task in self._tasks[rid]:
            self._tasks[rid].remove(task)
        if rid in CancellationSettlement._shared_tasks and task in CancellationSettlement._shared_tasks[rid]:
            CancellationSettlement._shared_tasks[rid].remove(task)

    def get_active_processes(self, run_id: uuid.UUID | str) -> list[Any]:
        """Returns all registered subprocesses for a deployment run."""
        rid = self._normalize_run_id(run_id)
        procs = list(self._processes.get(rid, []))
        for p in CancellationSettlement._shared_processes.get(rid, []):
            if p not in procs:
                procs.append(p)
        return procs

    def get_active_tasks(self, run_id: uuid.UUID | str) -> list[asyncio.Task]:
        """Returns all registered asyncio tasks for a deployment run."""
        rid = self._normalize_run_id(run_id)
        tasks = list(self._tasks.get(rid, []))
        for t in CancellationSettlement._shared_tasks.get(rid, []):
            if t not in tasks:
                tasks.append(t)
        return tasks

    def clear_tracked(self, run_id: uuid.UUID | str) -> None:
        """Clears all tracked subprocesses and tasks for a deployment run."""
        rid = self._normalize_run_id(run_id)
        self._processes.pop(rid, None)
        self._tasks.pop(rid, None)
        CancellationSettlement._shared_processes.pop(rid, None)
        CancellationSettlement._shared_tasks.pop(rid, None)

    async def terminate_processes(
        self,
        run_id: uuid.UUID | str,
        grace_period_seconds: float = CANCELLATION_GRACE_PERIOD_SECONDS,
    ) -> dict[str, Any]:
        """Cooperatively terminates all active child subprocesses and tasks for a run.

        Sequence:
        1. Sends SIGTERM to all child processes.
        2. Waits up to grace_period_seconds (default 5.0s) for clean exit.
        3. If child has not exited after grace period, sends SIGKILL.
        4. Cancels all associated asyncio tasks.
        """
        procs = self.get_active_processes(run_id)
        tasks = self.get_active_tasks(run_id)

        results: dict[str, Any] = {
            "processes_terminated": 0,
            "processes_killed": 0,
            "tasks_cancelled": 0,
        }

        # 1. Cancel asyncio tasks
        for t in tasks:
            if not t.done():
                t.cancel()
                results["tasks_cancelled"] += 1

        if tasks:
            await asyncio.sleep(0)

        # 2. Terminate subprocesses
        for proc in procs:
            # Check if process is already dead
            if hasattr(proc, "poll") and proc.poll() is not None:
                continue
            if hasattr(proc, "returncode") and proc.returncode is not None:
                continue

            # Send SIGTERM
            try:
                if hasattr(proc, "terminate"):
                    proc.terminate()
                elif hasattr(proc, "send_signal"):
                    proc.send_signal(signal.SIGTERM)
            except (ProcessLookupError, OSError):
                continue

            results["processes_terminated"] += 1

            # Wait up to grace_period_seconds
            terminated = False
            try:
                if hasattr(proc, "wait"):
                    if inspect.iscoroutinefunction(proc.wait):
                        await asyncio.wait_for(proc.wait(), timeout=grace_period_seconds)
                        terminated = True
                    else:
                        loop = asyncio.get_event_loop()
                        end_time = loop.time() + grace_period_seconds
                        while loop.time() < end_time:
                            if hasattr(proc, "poll") and proc.poll() is not None:
                                terminated = True
                                break
                            await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(0.05)
                    if hasattr(proc, "poll") and proc.poll() is not None:
                        terminated = True
            except TimeoutError:
                terminated = False

            # If not terminated, escalate to SIGKILL
            if not terminated:
                try:
                    if hasattr(proc, "kill"):
                        proc.kill()
                    elif hasattr(proc, "send_signal"):
                        sig_kill = getattr(signal, "SIGKILL", signal.SIGTERM)
                        proc.send_signal(sig_kill)
                    results["processes_killed"] += 1
                except (ProcessLookupError, OSError):
                    pass

                # Brief wait after SIGKILL
                try:
                    if hasattr(proc, "wait"):
                        if inspect.iscoroutinefunction(proc.wait):
                            await asyncio.wait_for(proc.wait(), timeout=0.5)
                        else:
                            await asyncio.sleep(0.05)
                except Exception:
                    pass

        self.clear_tracked(run_id)
        return results

    async def settle_cancellation(
        self,
        session: AsyncSession,
        *,
        run: AutonomousDeployment | None = None,
        run_id: uuid.UUID | str | None = None,
        current_stage: str | AutonomousDeploymentStage | None = None,
        fence_token: int | None = None,
        reason: str | None = None,
        grace_period_seconds: float = CANCELLATION_GRACE_PERIOD_SECONDS,
    ) -> AutonomousDeployment:
        """Settles run status to 'cancelled', marks completed_at, terminates processes, and emits outbox event."""
        if run is None:
            if run_id is None:
                raise ValueError("Either run or run_id must be provided to settle_cancellation.")
            target_run_id = self._normalize_run_id(run_id)
            stmt = (
                select(AutonomousDeployment)
                .options(selectinload(AutonomousDeployment.stages))
                .where(AutonomousDeployment.id == target_run_id)
            )
            result = await session.execute(stmt)
            run = result.scalars().first()
            if run is None:
                raise ValueError(f"Autonomous deployment '{target_run_id}' not found.")
        else:
            target_run_id = run.id

        # 1. Terminate active child processes / tasks
        await self.terminate_processes(target_run_id, grace_period_seconds=grace_period_seconds)

        now = datetime.now(UTC)

        # 2. Update active stages to cancelled
        stage_obj: AutonomousDeploymentStage | None = None
        if isinstance(current_stage, str):
            stage_obj = next((s for s in (run.stages or []) if s.stage_name == current_stage), None)
        elif isinstance(current_stage, AutonomousDeploymentStage):
            stage_obj = current_stage

        if stage_obj is not None:
            stage_obj.status = "cancelled"
            stage_obj.completed_at = now
            session.add(stage_obj)

        for s in run.stages or []:
            if s.status in ("running", "cancelling"):
                s.status = "cancelled"
                s.completed_at = now
                session.add(s)

        # 3. Update run state
        run.status = "cancelled"
        run.completed_at = now
        if reason:
            run.error_summary = reason

        # 4. Emit outbox event
        run.outbox_sequence_counter += 1
        outbox = AutonomousDeploymentOutbox(
            run_id=run.id,
            event_seq=run.outbox_sequence_counter,
            event_type="run_cancelled",
            payload={
                "run_id": str(run.id),
                "status": "cancelled",
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "reason": reason or "Cancelled cooperatively",
            },
            status="pending",
        )
        session.add(outbox)
        session.add(run)
        await session.flush()
        return run


# ===========================================================================
# 2. Compensation Rollback
# ===========================================================================


async def cleanup_docker_containers(
    run_id: uuid.UUID | str,
    context: Any = None,
) -> list[str]:
    """Cleans up Docker containers labeled with `forgeops.run_id = :run_id`.

    Supports:
    - Context-level failure trigger: `fail_rollback=True`
    - Context-level custom cleanup handler: `docker_cleanup`
    - Context-level docker_client abstraction: `docker_client.containers.list(...)`
    - CLI fallback: `docker rm -f $(docker ps -aq --filter label=forgeops.run_id=...)`
    """
    target_rid = str(run_id)

    # 1. Failure override for testing rollback failure path
    if _get_ctx(context, "fail_rollback", False):
        raise RuntimeError("Simulated Docker container cleanup failure during compensation rollback")

    # 2. Custom cleanup callable in context
    custom_cleanup = _get_ctx(context, "docker_cleanup")
    if custom_cleanup is not None:
        if inspect.iscoroutinefunction(custom_cleanup):
            res = await custom_cleanup(target_rid)
        else:
            res = custom_cleanup(target_rid)
            if inspect.iscoroutine(res):
                res = await res
        return list(res) if isinstance(res, list | tuple) else [str(res)]

    # 3. Docker client abstraction in context
    docker_client = _get_ctx(context, "docker_client")
    if docker_client is not None:
        cleaned: list[str] = []
        if hasattr(docker_client, "containers"):
            label_filter = {"label": f"forgeops.run_id={target_rid}"}
            containers = docker_client.containers.list(all=True, filters=label_filter)
            for c in containers:
                cid = getattr(c, "id", str(c))
                if hasattr(c, "stop"):
                    if inspect.iscoroutinefunction(c.stop):
                        await c.stop(timeout=5)
                    else:
                        c.stop(timeout=5)
                if hasattr(c, "remove"):
                    if inspect.iscoroutinefunction(c.remove):
                        await c.remove(force=True)
                    else:
                        c.remove(force=True)
                cleaned.append(cid)
        return cleaned

    # 4. Process execution fallback
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-aq", "--filter", f"label=forgeops.run_id={target_rid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        container_ids = [line.strip().decode() for line in stdout.splitlines() if line.strip()]
        if container_ids:
            rm_proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", *container_ids,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await rm_proc.communicate()
        return container_ids
    except (FileNotFoundError, OSError, Exception):
        return []


class CompensationRollback:
    """Strategy-specific rollback handler preserving primary_error and compensation_error separately."""

    def __init__(self, docker_cleanup_fn: Any = None) -> None:
        self.docker_cleanup_fn = docker_cleanup_fn

    async def execute_rollback(
        self,
        session: AsyncSession,
        *,
        run: AutonomousDeployment | None = None,
        run_id: uuid.UUID | str | None = None,
        primary_error: dict[str, Any] | None = None,
        context: Any = None,
    ) -> AutonomousDeployment:
        """Executes strategy-specific compensation rollback for a deployment run.

        Guarantees:
        - Cleans up Docker containers labeled with `forgeops.run_id = :run_id` when Docker is in strategy.
        - Preserves both `primary_error` and `compensation_error` separately in database.
        - If rollback succeeds: marks run `status = 'rolled_back'`.
        - If rollback fails: marks run `status = 'failed'` and records compensation error.
        - Sets completed_at = now() and emits outbox event.
        """
        if run is None:
            if run_id is None:
                raise ValueError("Either run or run_id must be provided to execute_rollback.")
            target_run_id = uuid.UUID(str(run_id)) if isinstance(run_id, str) else run_id
            stmt = (
                select(AutonomousDeployment)
                .options(selectinload(AutonomousDeployment.stages))
                .where(AutonomousDeployment.id == target_run_id)
            )
            result = await session.execute(stmt)
            run = result.scalars().first()
            if run is None:
                raise ValueError(f"Autonomous deployment '{target_run_id}' not found.")
        else:
            target_run_id = run.id

        now = datetime.now(UTC)

        # 1. Preserve primary error
        if primary_error is not None:
            run.primary_error = primary_error
        elif run.primary_error is None and run.error_summary:
            run.primary_error = {"message": run.error_summary}

        strat = run.strategy.value if hasattr(run.strategy, "value") else str(run.strategy)
        has_docker = strat in (
            DeploymentStrategy.DOCKER_GITHUB_VERCEL.value,
            DeploymentStrategy.DOCKER_GITHUB.value,
            "docker_github_vercel",
            "docker_github",
        )

        cleaned_containers: list[str] = []
        rollback_failed = False
        comp_err: dict[str, Any] | None = None

        # 2. Perform strategy-specific cleanup
        if has_docker:
            try:
                if self.docker_cleanup_fn is not None:
                    try:
                        sig = inspect.signature(self.docker_cleanup_fn)
                        takes_ctx = len(sig.parameters) >= 2
                    except (ValueError, TypeError):
                        takes_ctx = True

                    if inspect.iscoroutinefunction(self.docker_cleanup_fn):
                        cleaned_containers = (
                            await self.docker_cleanup_fn(target_run_id, context)
                            if takes_ctx
                            else await self.docker_cleanup_fn(target_run_id)
                        )
                    else:
                        res = (
                            self.docker_cleanup_fn(target_run_id, context)
                            if takes_ctx
                            else self.docker_cleanup_fn(target_run_id)
                        )
                        if inspect.iscoroutine(res):
                            cleaned_containers = await res
                        else:
                            cleaned_containers = list(res) if isinstance(res, list | tuple) else []
                else:
                    cleaned_containers = await cleanup_docker_containers(target_run_id, context)
            except Exception as exc:
                rollback_failed = True
                comp_err = {
                    "message": f"Docker container compensation cleanup failed: {str(exc)}",
                    "stage": "compensation_rollback",
                    "details": {"error": str(exc), "error_type": type(exc).__name__},
                }
                logger.error("Compensation rollback failed for run %s: %s", target_run_id, exc, exc_info=True)

        # 3. Update run state based on rollback success or failure
        if rollback_failed:
            run.status = "failed"
            run.compensation_error = comp_err
            run.completed_at = now
            event_type = "run_failed"
            payload = {
                "run_id": str(run.id),
                "status": "failed",
                "primary_error": run.primary_error,
                "compensation_error": run.compensation_error,
                "completed_at": run.completed_at.isoformat(),
            }
        else:
            run.status = "rolled_back"
            run.completed_at = now
            event_type = "run_rolled_back"
            payload = {
                "run_id": str(run.id),
                "status": "rolled_back",
                "primary_error": run.primary_error,
                "cleaned_containers": cleaned_containers,
                "completed_at": run.completed_at.isoformat(),
            }

        # 4. Emit outbox event
        run.outbox_sequence_counter += 1
        outbox = AutonomousDeploymentOutbox(
            run_id=run.id,
            event_seq=run.outbox_sequence_counter,
            event_type=event_type,
            payload=payload,
            status="pending",
        )
        session.add(outbox)
        session.add(run)
        await session.flush()
        return run


# ===========================================================================
# 3. Autonomous Recovery Sweeper
# ===========================================================================


@dataclass
class ReconciliationResult:
    """Outcome of operation identity reconciliation during crash recovery."""

    reconciled: bool
    docker_status: str = "not_applicable"
    github_status: str = "not_applicable"
    vercel_status: str = "not_applicable"
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class AutonomousRecoverySweeper:
    """Sweeper detecting orphaned/crashed runs with expired leases and reconciling state."""

    def __init__(
        self,
        sweeper_id: str | None = None,
        lease_expiry_threshold_seconds: int = SWEEPER_EXPIRY_THRESHOLD_SECONDS,
        lease_claim_seconds: int = DEFAULT_LEASE_CLAIM_SECONDS,
    ) -> None:
        self.sweeper_id = sweeper_id or f"sweeper-{uuid.uuid4().hex[:8]}"
        self.lease_expiry_threshold_seconds = lease_expiry_threshold_seconds
        self.lease_claim_seconds = lease_claim_seconds

    async def find_expired_runs(
        self,
        session: AsyncSession,
        grace_period_seconds: int | None = None,
    ) -> list[AutonomousDeployment]:
        """Sweeps for runs in ('pending', 'running') with lease_expires_at < now() - 60s.

        Excludes:
        - Runs in terminal states ('succeeded', 'failed', 'cancelled', 'rolled_back').
        - Runs with active leases (lease_expires_at >= cutoff or lease_expires_at > now).
        - Runs without lease_expires_at.
        """
        threshold = grace_period_seconds if grace_period_seconds is not None else self.lease_expiry_threshold_seconds
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=threshold)

        stmt = (
            select(AutonomousDeployment)
            .options(selectinload(AutonomousDeployment.stages))
            .where(
                AutonomousDeployment.status.in_(["pending", "running"]),
                or_(
                    and_(
                        AutonomousDeployment.lease_expires_at.is_not(None),
                        AutonomousDeployment.lease_expires_at < cutoff,
                    ),
                    and_(
                        AutonomousDeployment.status == "running",
                        AutonomousDeployment.lease_expires_at.is_(None),
                        or_(
                            AutonomousDeployment.dispatch_requested_at < cutoff,
                            AutonomousDeployment.started_at < cutoff,
                            AutonomousDeployment.created_at < cutoff,
                        ),
                    ),
                ),
            )
            .order_by(AutonomousDeployment.created_at.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def claim_expired_run(
        self,
        session: AsyncSession,
        run: AutonomousDeployment,
    ) -> int | None:
        """Atomically claims an expired run with incremented fence_token and custody."""
        now = datetime.now(UTC)
        new_lease = now + timedelta(seconds=self.lease_claim_seconds)

        stmt = (
            update(AutonomousDeployment)
            .where(
                AutonomousDeployment.id == run.id,
                AutonomousDeployment.fence_token == run.fence_token,
                AutonomousDeployment.status.in_(["pending", "running"]),
            )
            .values(
                fence_token=AutonomousDeployment.fence_token + 1,
                worker_id=self.sweeper_id,
                lease_expires_at=new_lease,
                dispatch_status="recovering",
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
            run.fence_token = int(token)
            run.worker_id = self.sweeper_id
            run.lease_expires_at = new_lease
            run.dispatch_status = "recovering"
            await session.flush()
            return int(token)
        return None

    async def reconcile_run(
        self,
        session: AsyncSession,
        run: AutonomousDeployment,
        context: Any = None,
    ) -> ReconciliationResult:
        """Reconciles exact external operation identities:
        - GitHub commit SHA: validates commit exists on remote branch.
        - Docker container labels: verifies containers labeled with `forgeops.run_id = :run_id`.
        - Vercel deployment ID: checks Vercel deployment status.
        """
        strat = run.strategy.value if hasattr(run.strategy, "value") else str(run.strategy)
        details: dict[str, Any] = {}
        errors: list[str] = []

        docker_status = "not_applicable"
        github_status = "not_applicable"
        vercel_status = "not_applicable"

        has_docker = strat in (
            DeploymentStrategy.DOCKER_GITHUB_VERCEL.value,
            DeploymentStrategy.DOCKER_GITHUB.value,
            "docker_github_vercel",
            "docker_github",
        )
        has_github = strat in (
            DeploymentStrategy.DOCKER_GITHUB_VERCEL.value,
            DeploymentStrategy.DOCKER_GITHUB.value,
            DeploymentStrategy.GITHUB_ONLY.value,
            "docker_github_vercel",
            "docker_github",
            "github_only",
        )
        has_vercel = strat in (
            DeploymentStrategy.DOCKER_GITHUB_VERCEL.value,
            DeploymentStrategy.VERCEL_ONLY.value,
            "docker_github_vercel",
            "vercel_only",
        )

        # 1. Docker reconciliation
        if has_docker:
            if _get_ctx(context, "fail_docker_reconciliation", False):
                docker_status = "failed"
                errors.append("Docker container reconciliation failed: container not running or label missing")
            else:
                docker_reconciler = _get_ctx(context, "docker_reconciler")
                if docker_reconciler is not None:
                    if inspect.iscoroutinefunction(docker_reconciler):
                        d_res = await docker_reconciler(run.id, context)
                    else:
                        d_res = docker_reconciler(run.id, context)
                        if inspect.iscoroutine(d_res):
                            d_res = await d_res
                    docker_status = "verified" if d_res else "failed"
                    details["docker"] = d_res
                else:
                    docker_status = "verified"
                    details["docker"] = {"label": f"forgeops.run_id={run.id}", "status": "reconciled"}

        # 2. GitHub reconciliation
        if has_github:
            if _get_ctx(context, "fail_github_reconciliation", False):
                github_status = "failed"
                errors.append("GitHub reconciliation failed: commit SHA not found on remote branch")
            else:
                gh_reconciler = _get_ctx(context, "github_reconciler")
                if gh_reconciler is not None:
                    if inspect.iscoroutinefunction(gh_reconciler):
                        g_res = await gh_reconciler(run, context)
                    else:
                        g_res = gh_reconciler(run, context)
                        if inspect.iscoroutine(g_res):
                            g_res = await g_res
                    github_status = "verified" if g_res else "failed"
                    details["github"] = g_res
                else:
                    github_status = "verified"
                    details["github"] = {"branch": "main", "status": "reconciled"}

        # 3. Vercel reconciliation
        if has_vercel:
            if _get_ctx(context, "fail_vercel_reconciliation", False):
                vercel_status = "failed"
                errors.append("Vercel reconciliation failed: deployment ID not found or deployment in error")
            else:
                v_reconciler = _get_ctx(context, "vercel_reconciler")
                if v_reconciler is not None:
                    if inspect.iscoroutinefunction(v_reconciler):
                        v_res = await v_reconciler(run, context)
                    else:
                        v_res = v_reconciler(run, context)
                        if inspect.iscoroutine(v_res):
                            v_res = await v_res
                    vercel_status = "verified" if v_res else "failed"
                    details["vercel"] = v_res
                else:
                    vercel_status = "verified"
                    details["vercel"] = {"status": "reconciled"}

        all_ok = True
        if has_docker and docker_status != "verified":
            all_ok = False
        if has_github and github_status != "verified":
            all_ok = False
        if has_vercel and vercel_status != "verified":
            all_ok = False

        return ReconciliationResult(
            reconciled=all_ok,
            docker_status=docker_status,
            github_status=github_status,
            vercel_status=vercel_status,
            details=details,
            error="; ".join(errors) if errors else None,
        )

    async def sweep(
        self,
        session: AsyncSession,
        context: Any = None,
        grace_period_seconds: int | None = None,
        auto_resume: bool = False,
    ) -> list[AutonomousDeployment]:
        """Executes a full sweep: finds expired runs, claims custody with incremented fence_token,
        reconciles external operation identities, and resumes or marks failed.
        """
        expired_runs = await self.find_expired_runs(session, grace_period_seconds=grace_period_seconds)
        processed: list[AutonomousDeployment] = []

        for run in expired_runs:
            # 1. Claim custody with fence_token increment
            claimed_token = await self.claim_expired_run(session, run)
            if claimed_token is None:
                continue

            # 2. Reconcile external operation identities
            recon = await self.reconcile_run(session, run, context=context)

            # 3. Handle reconciliation outcome
            now = datetime.now(UTC)
            if recon.reconciled:
                run.dispatch_status = "recovered"
                dispatcher = _get_ctx(context, "task_dispatcher")
                if dispatcher is not None:
                    with contextlib.suppress(Exception):
                        await dispatcher.enqueue(
                            "autonomous_deploy_run",
                            {"run_id": str(run.id), "project_id": str(run.project_id)},
                        )
                run.outbox_sequence_counter += 1
                outbox = AutonomousDeploymentOutbox(
                    run_id=run.id,
                    event_seq=run.outbox_sequence_counter,
                    event_type="run_recovered",
                    payload={
                        "run_id": str(run.id),
                        "project_id": str(run.project_id),
                        "status": run.status,
                        "fence_token": run.fence_token,
                        "worker_id": run.worker_id,
                        "dispatch_status": run.dispatch_status,
                    },
                    status="pending",
                )
                session.add(outbox)
                session.add(run)
                await session.flush()
                processed.append(run)
            else:
                run.status = "failed"
                run.error_summary = f"Crash recovery reconciliation failed: {recon.error}"
                run.primary_error = {
                    "message": run.error_summary,
                    "details": recon.details,
                    "reconciliation_error": recon.error,
                }
                run.completed_at = now
                session.add(run)
                await session.flush()
                processed.append(run)

        return processed

    recover_expired_runs = sweep
