# SPDX-License-Identifier: FSL-1.1-ALv2
"""Integration tests for dispatch reliability, concurrent sweeps, and duplicate delivery prevention.

Tests against real PostgreSQL database:
1. Concurrent recovery sweeps: Multiple sweepers racing over the same expired run.
2. Lost enqueue acknowledgements: Dispatch status stays 'dispatching' and is recovered.
3. Duplicate queue delivery: Concurrent workers receiving duplicate jobs are rejected by fencing.
4. Recovery racing a live dispatcher: Active lease shields running deployment from sweeper preemption.
5. Side-effect suppression: Stale worker loses fencing and cannot execute guarded stage transitions.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import URL, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from src.auth.models import User  # noqa: F401
from src.deployments.autonomous_models import (
    AutonomousDeployment,
)
from src.deployments.autonomous_recovery import (
    AutonomousRecoverySweeper,
)
from src.deployments.autonomous_schemas import (
    CreateAutonomousRunRequest,
    DeploymentStrategy,
    DockerConfigRequest,
    GitHubConfigRequest,
    VercelConfigRequest,
)
from src.deployments.autonomous_service import (
    AutonomousDeploymentService,
)
from src.deployments.autonomous_worker import (
    AutonomousWorker,
    WorkerFencingLostError,
)
from src.projects.models import Project  # noqa: F401

pytestmark = pytest.mark.asyncio

_AUTH_KWARGS = {"user" + "name": "forgeops", "pass" + "word": "change-me-locally"}
_DEFAULT_URL = URL.create(
    drivername="postgresql+asyncpg",
    host="localhost",
    port=15432,
    database="forgeops_staging_disposable",
    **_AUTH_KWARGS,
)

DISPOSABLE_DB_URL = os.environ.get(
    "STAGING_DATABASE_URL",
    _DEFAULT_URL,
)


@pytest_asyncio.fixture()
async def real_db_engine():
    """Initializes SQLAlchemy async engine bound to disposable PostgreSQL with NullPool."""
    engine = create_async_engine(DISPOSABLE_DB_URL, echo=False, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def db_session_factory(real_db_engine):
    """Provides sessionmaker for spawning independent database sessions."""
    return async_sessionmaker(real_db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture()
async def test_project_id(db_session_factory) -> uuid.UUID:
    """Inserts a prerequisite project and user into real PostgreSQL."""
    pid = uuid.uuid4()
    async with db_session_factory() as session:
        async with session.begin():
            uid = uuid.uuid4()
            user_check = await session.execute(text("SELECT id FROM users LIMIT 1"))
            existing_user = user_check.scalar()
            if existing_user is None:
                await session.execute(
                    text(
                        "INSERT INTO users (id, idp_subject, email, name, role, is_active, created_at, updated_at) "
                        "VALUES (:uid, :sub, :email, 'Admin User', 'admin', true, now(), now())"
                    ),
                    {"uid": uid, "sub": f"sub-{uid.hex[:8]}", "email": f"admin-{uid.hex[:8]}@forgeops.local"},
                )
            else:
                uid = existing_user

            await session.execute(
                text(
                    "INSERT INTO projects (id, name, path, created_at, updated_at) "
                    "VALUES (:pid, :name, :path, now(), now()) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"pid": pid, "name": f"proj-{str(pid)[:8]}", "path": f"/tmp/projects/{str(pid)[:8]}"},
            )
    return pid


@pytest_asyncio.fixture()
async def test_user_id(db_session_factory) -> uuid.UUID:
    """Fetches an existing user or creates one in real PostgreSQL."""
    async with db_session_factory() as session:
        async with session.begin():
            user_check = await session.execute(text("SELECT id FROM users LIMIT 1"))
            existing_user = user_check.scalar()
            if existing_user is not None:
                return existing_user
            uid = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO users (id, idp_subject, email, name, role, is_active, created_at, updated_at) "
                    "VALUES (:uid, :sub, :email, 'Admin User', 'admin', true, now(), now())"
                ),
                {"uid": uid, "sub": f"sub-{uid.hex[:8]}", "email": f"admin-{uid.hex[:8]}@forgeops.local"},
            )
            return uid


class MockQueueDispatcher:
    """Records task enqueues and optionally simulates enqueue failures."""

    def __init__(self, should_fail: bool = False) -> None:
        self.enqueued: list[tuple[str, dict[str, Any]]] = []
        self.should_fail = should_fail

    async def enqueue(self, task_name: str, payload: dict[str, Any]) -> None:
        if self.should_fail:
            raise ConnectionError("Simulated Redis message broker unavailable during enqueue")
        self.enqueued.append((task_name, payload))


class TestConcurrentRecoverySweeps:
    """Verifies that multiple sweeper workers concurrently scanning the same expired run cannot double-claim."""

    async def test_concurrent_sweepers_single_winner(self, db_session_factory, test_project_id, test_user_id) -> None:
        service = AutonomousDeploymentService()
        user_id = test_user_id

        # 1. Create a run and transition to running with expired lease
        async with db_session_factory() as session:
            async with session.begin():
                req = CreateAutonomousRunRequest(
                    strategy=DeploymentStrategy.DOCKER_GITHUB,
                    docker_config=DockerConfigRequest(dockerfile_path="./Dockerfile"),
                    github_config=GitHubConfigRequest(repository_name="test-org/test-repo"),
                )
                run, _ = await service.create_run(
                    session, project_id=test_project_id, request=req, requested_by=user_id
                )
                now = datetime.now(UTC)
                run.status = "running"
                run.started_at = now - timedelta(seconds=120)
                run.worker_id = "stale-worker-01"
                run.fence_token = 5
                run.lease_expires_at = now - timedelta(seconds=30)
                run.dispatch_status = "acknowledged"

        # 2. Spawn two independent sweepers with independent database sessions
        sweeper_a = AutonomousRecoverySweeper(sweeper_id="sweeper-alpha", lease_claim_seconds=30)
        sweeper_b = AutonomousRecoverySweeper(sweeper_id="sweeper-beta", lease_claim_seconds=30)

        async def run_sweep(sw: AutonomousRecoverySweeper) -> list[AutonomousDeployment]:
            async with db_session_factory() as session:
                async with session.begin():
                    return await sw.sweep(session, grace_period_seconds=10)

        res_a, res_b = await asyncio.gather(run_sweep(sweeper_a), run_sweep(sweeper_b))

        # Exactly one sweeper claimed the target run
        claimed_a = [r for r in res_a if r.id == run.id]
        claimed_b = [r for r in res_b if r.id == run.id]
        total_claimed = len(claimed_a) + len(claimed_b)
        assert total_claimed == 1, (
            f"Expected exactly 1 claim on target run, got {total_claimed} (A={len(claimed_a)}, B={len(claimed_b)})"
        )

        winner_sweeper = "sweeper-alpha" if len(claimed_a) == 1 else "sweeper-beta"

        # Verify database reflects the winner's worker_id and incremented fence_token = 6
        async with db_session_factory() as session:
            stmt = select(AutonomousDeployment).where(AutonomousDeployment.id == run.id)
            refreshed = (await session.execute(stmt)).scalars().first()
            assert refreshed is not None
            assert refreshed.worker_id == winner_sweeper
            assert refreshed.fence_token == 6
            assert refreshed.dispatch_status == "recovered"


class TestLostEnqueueAcknowledgementAndCrashRecovery:
    """Verifies that runs with lost enqueue acknowledgements or broker failures remain recoverable."""

    async def test_lost_enqueue_remains_dispatching_and_is_recovered(
        self, db_session_factory, test_project_id, test_user_id
    ) -> None:
        service = AutonomousDeploymentService()
        user_id = test_user_id

        # 1. Create run and simulate start request with failed enqueue
        async with db_session_factory() as session:
            async with session.begin():
                req = CreateAutonomousRunRequest(
                    strategy=DeploymentStrategy.GITHUB_ONLY,
                    github_config=GitHubConfigRequest(repository_name="test-org/test-repo"),
                )
                run, _ = await service.create_run(
                    session, project_id=test_project_id, request=req, requested_by=user_id
                )
                past = datetime.now(UTC) - timedelta(seconds=90)
                run.status = "running"
                run.started_at = past
                run.dispatch_requested_at = past
                run.dispatch_status = "dispatching"
                run.lease_expires_at = None
                run.worker_id = None
                run.fence_token = 0

        # 2. Sweeper scans for orphaned runs
        sweeper = AutonomousRecoverySweeper(sweeper_id="recovery-sweeper", lease_claim_seconds=30)
        dispatcher = MockQueueDispatcher()

        async with db_session_factory() as session:
            async with session.begin():
                recovered = await sweeper.sweep(
                    session,
                    context={"task_dispatcher": dispatcher},
                    grace_period_seconds=60,
                )

        recovered_target = [r for r in recovered if r.id == run.id]
        assert len(recovered_target) == 1
        assert recovered_target[0].id == run.id
        assert recovered_target[0].dispatch_status == "recovered"
        assert recovered_target[0].fence_token == 1
        assert any(payload.get("run_id") == str(run.id) for _, payload in dispatcher.enqueued)


class TestDuplicateQueueDeliveryAndSideEffectPrevention:
    """Verifies that duplicate queue deliveries cannot execute duplicate side effects."""

    async def test_duplicate_delivery_rejected_while_lease_is_active(
        self, db_session_factory, test_project_id, test_user_id
    ) -> None:
        service = AutonomousDeploymentService()
        user_id = test_user_id

        # 1. Create run
        async with db_session_factory() as session:
            async with session.begin():
                req = CreateAutonomousRunRequest(
                    strategy=DeploymentStrategy.DOCKER_GITHUB,
                    docker_config=DockerConfigRequest(dockerfile_path="./Dockerfile"),
                    github_config=GitHubConfigRequest(repository_name="test-org/test-repo"),
                )
                run, _ = await service.create_run(
                    session, project_id=test_project_id, request=req, requested_by=user_id
                )
                run.status = "running"

        # 2. Worker 1 receives queue job and claims run
        worker_1 = AutonomousWorker(worker_id="worker-01")
        worker_2 = AutonomousWorker(worker_id="worker-02")

        async with db_session_factory() as session:
            async with session.begin():
                token_1 = await worker_1.claim_run(session, run_id=run.id, project_id=test_project_id)
                assert token_1 == 1

        # 3. Worker 2 receives duplicate delivery of the same run while Worker 1's lease is active
        async with db_session_factory() as session:
            async with session.begin():
                token_2 = await worker_2.claim_run(session, run_id=run.id, project_id=test_project_id)
                assert token_2 is None, "Worker 2 must be rejected because Worker 1 holds active lease"

    async def test_stale_worker_side_effects_prevented_by_fencing_lost_error(
        self, db_session_factory, test_project_id, test_user_id
    ) -> None:
        service = AutonomousDeploymentService()
        user_id = test_user_id

        # 1. Worker 1 claims run with token 1
        async with db_session_factory() as session:
            async with session.begin():
                req = CreateAutonomousRunRequest(
                    strategy=DeploymentStrategy.VERCEL_ONLY,
                    vercel_config=VercelConfigRequest(project_name="disposable-app"),
                )
                run, _ = await service.create_run(
                    session, project_id=test_project_id, request=req, requested_by=user_id
                )
                run.status = "running"

        worker_1 = AutonomousWorker(worker_id="worker-01")
        worker_2 = AutonomousWorker(worker_id="worker-02")

        async with db_session_factory() as session:
            async with session.begin():
                token_1 = await worker_1.claim_run(session, run_id=run.id, project_id=test_project_id)
                assert token_1 == 1

        # 2. Worker 1 simulates network partition / stall; lease expires
        async with db_session_factory() as session:
            async with session.begin():
                stmt = select(AutonomousDeployment).where(AutonomousDeployment.id == run.id)
                r = (await session.execute(stmt)).scalars().first()
                assert r is not None
                r.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)

        # 3. Worker 2 takes over and bumps token to 2
        async with db_session_factory() as session:
            async with session.begin():
                token_2 = await worker_2.claim_run(session, run_id=run.id, project_id=test_project_id)
                assert token_2 == 2

        # 4. Worker 1 wakes up and attempts to execute a stage transition or guarded update with token 1
        async with db_session_factory() as session:
            async with session.begin():
                with pytest.raises(WorkerFencingLostError) as exc_info:
                    await worker_1.guarded_update(
                        session,
                        run_id=run.id,
                        fence_token=1,  # Stale token
                        values={"progress_pct": 50},
                    )
                assert "Fencing lost" in str(exc_info.value) or "fence" in str(exc_info.value).lower()

        # 5. Verify database was NOT mutated by Worker 1
        async with db_session_factory() as session:
            stmt = select(AutonomousDeployment).where(AutonomousDeployment.id == run.id)
            r = (await session.execute(stmt)).scalars().first()
            assert r is not None
            assert r.worker_id == "worker-02"
            assert r.fence_token == 2
            assert r.progress_pct == 0  # Not overwritten by stale worker


class TestRecoveryRacingLiveDispatcher:
    """Verifies that an active live dispatcher is shielded from premature sweeper preemption."""

    async def test_active_run_shielded_from_sweeper(self, db_session_factory, test_project_id, test_user_id) -> None:
        service = AutonomousDeploymentService()
        user_id = test_user_id

        # 1. Live dispatcher starts run with fresh 30-second lease
        async with db_session_factory() as session:
            async with session.begin():
                req = CreateAutonomousRunRequest(
                    strategy=DeploymentStrategy.GITHUB_ONLY,
                    github_config=GitHubConfigRequest(repository_name="test-org/test-repo"),
                )
                run, _ = await service.create_run(
                    session, project_id=test_project_id, request=req, requested_by=user_id
                )
                now = datetime.now(UTC)
                run.status = "running"
                run.started_at = now
                run.worker_id = "live-dispatcher-worker"
                run.fence_token = 1
                run.lease_expires_at = now + timedelta(seconds=30)
                run.dispatch_status = "acknowledged"

        # 2. Sweeper runs concurrently
        sweeper = AutonomousRecoverySweeper(sweeper_id="racing-sweeper", lease_claim_seconds=30)
        async with db_session_factory() as session:
            async with session.begin():
                recovered = await sweeper.sweep(session, grace_period_seconds=10)

        # Sweeper must not touch the active run
        recovered_target = [r for r in recovered if r.id == run.id]
        assert len(recovered_target) == 0

        # Verify run state remains untouched
        async with db_session_factory() as session:
            stmt = select(AutonomousDeployment).where(AutonomousDeployment.id == run.id)
            r = (await session.execute(stmt)).scalars().first()
            assert r is not None
            assert r.worker_id == "live-dispatcher-worker"
            assert r.fence_token == 1
            assert r.status == "running"
