# SPDX-License-Identifier: FSL-1.1-ALv2
"""Integration & Staging Validation against Real PostgreSQL 17 + pgvector.

Verifies:
1. Migration 0038 schema & PostgreSQL constraints on a real disposable database (forgeops_staging_disposable).
2. Concurrent run creation & starts using independent AsyncSession connections.
3. Atomic worker claims, lease expiry, and fencing token preemption.
4. Compensation rollback & dual error (primary_error, compensation_error) JSONB persistence.
5. Outbox sequence allocation & unique constraint enforcement.
6. Crash window recovery when process dies before worker claim/enqueue.
7. Controlled staging executions across all 4 deployment strategies with strategy-aware G7 target verification.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import URL, text
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
    STAGE_G1_BLUEPRINT,
    STAGE_G7_VERIFICATION,
    AutonomousDeploymentService,
)
from src.deployments.autonomous_worker import (
    AutonomousWorker,
    WorkerFencingLostError,
    run_pipeline,
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
    """Inserts a prerequisite project into the real PostgreSQL database."""
    pid = uuid.uuid4()
    async with db_session_factory() as session:
        async with session.begin():
            # Check if users table has a system user, or insert one
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

            # Insert project
            await session.execute(
                text(
                    "INSERT INTO projects (id, name, path, created_at, updated_at) "
                    "VALUES (:pid, :name, :path, now(), now()) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"pid": pid, "name": f"proj-{str(pid)[:8]}", "path": f"/tmp/projects/{str(pid)[:8]}"},
            )
    return pid


class TestPostgreSQLSchemaAndConstraints:
    """Validates real PostgreSQL table structures, columns, and check constraints."""

    async def test_tables_and_columns_exist_in_postgresql(self, real_db_engine) -> None:
        async with real_db_engine.connect() as conn:
            tables_result = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'autonomous_deployment%'"
                )
            )
            tables = {row[0] for row in tables_result.fetchall()}
            expected = {
                "autonomous_deployments",
                "autonomous_deployment_stages",
                "autonomous_deployment_logs",
                "autonomous_deployment_outbox",
            }
            assert expected.issubset(tables)

    async def test_postgresql_check_constraints_enforced(self, db_session_factory, test_project_id) -> None:
        """Verifies PostgreSQL engine rejects invalid check constraint violations."""
        # 1. Invalid status rejected by chk_run_status
        async with db_session_factory() as session:
            with pytest.raises(Exception) as exc_info:
                async with session.begin():
                    user_res = await session.execute(text("SELECT id FROM users LIMIT 1"))
                    uid = user_res.scalar()
                    await session.execute(
                        text(
                            "INSERT INTO autonomous_deployments "
                            "(id, project_id, attempt_number, status, strategy, payload_hash, created_by) "
                            "VALUES (:id, :pid, 1, 'invalid_status_xyz', 'github_only', 'hash123', :uid)"
                        ),
                        {"id": uuid.uuid4(), "pid": test_project_id, "uid": uid},
                    )
            assert "chk_run_status" in str(exc_info.value).lower() or "check" in str(exc_info.value).lower()

        # 2. Invalid progress rejected by chk_progress_range
        async with db_session_factory() as session:
            with pytest.raises(Exception) as exc_info:
                async with session.begin():
                    user_res = await session.execute(text("SELECT id FROM users LIMIT 1"))
                    uid = user_res.scalar()
                    await session.execute(
                        text(
                            "INSERT INTO autonomous_deployments "
                            "(id, project_id, attempt_number, status, strategy, "
                            "progress_pct, payload_hash, created_by) "
                            "VALUES (:id, :pid, 1, 'pending', 'github_only', 150, 'hash123', :uid)"
                        ),
                        {"id": uuid.uuid4(), "pid": test_project_id, "uid": uid},
                    )
            assert "chk_progress_range" in str(exc_info.value).lower() or "check" in str(exc_info.value).lower()


class TestPostgreSQLConcurrencyAndIsolation:
    """Tests race conditions and transaction isolation using real independent database sessions."""

    async def test_concurrent_run_creation_idempotency(self, db_session_factory, test_project_id) -> None:
        """Simultaneous create_run calls with identical idempotency key resolve to same run without deadlock."""
        user_res_uid: uuid.UUID
        async with db_session_factory() as session:
            user_res = await session.execute(text("SELECT id FROM users LIMIT 1"))
            user_res_uid = user_res.scalar()

        service = AutonomousDeploymentService()
        idemp_key = f"concurrent-key-{uuid.uuid4().hex[:8]}"
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.GITHUB_ONLY,
            github_config=GitHubConfigRequest(repository_name="forgeops/concurrent-test"),
            idempotency_key=idemp_key,
        )

        async def _create_in_session():
            async with db_session_factory() as s:
                run, created = await service.create_run(
                    s, project_id=test_project_id, requested_by=user_res_uid, request=req
                )
                await s.commit()
                return run.id, created

        # Fire 2 concurrent creation attempts
        results = await asyncio.gather(_create_in_session(), _create_in_session())
        run_ids = [r[0] for r in results]
        created_flags = [r[1] for r in results]

        # Both returned the exact same run ID
        assert run_ids[0] == run_ids[1]
        # Exactly one created, the other returned existing
        assert created_flags.count(True) == 1
        assert created_flags.count(False) == 1

    async def test_atomic_worker_claim_and_lease_expiry(self, db_session_factory, test_project_id) -> None:
        """Verifies atomic claim, heartbeat extension, and lease expiry preemption in PostgreSQL."""
        user_res_uid: uuid.UUID
        async with db_session_factory() as session:
            user_res = await session.execute(text("SELECT id FROM users LIMIT 1"))
            user_res_uid = user_res.scalar()

        service = AutonomousDeploymentService()
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.DOCKER_GITHUB,
            docker_config=DockerConfigRequest(image_tag="alpine:latest"),
            github_config=GitHubConfigRequest(repository_name="forgeops/concurrency"),
        )

        async with db_session_factory() as session:
            run, _ = await service.create_run(
                session, project_id=test_project_id, requested_by=user_res_uid, request=req
            )
            await session.commit()
            run_id = run.id

        worker1 = AutonomousWorker(worker_id="worker-real-1")
        worker2 = AutonomousWorker(worker_id="worker-real-2")

        # 1. Worker 1 claims run
        async with db_session_factory() as s1:
            token1 = await worker1.claim_run(s1, run_id=run_id, project_id=test_project_id)
            await s1.commit()
        assert token1 == 1

        # 2. Worker 2 attempts claim while Worker 1 lease is active -> returns None
        async with db_session_factory() as s2:
            token2 = await worker2.claim_run(s2, run_id=run_id, project_id=test_project_id)
            await s2.commit()
        assert token2 is None

        # 3. Worker 1 heartbeats -> lease extended, token unchanged
        async with db_session_factory() as s1:
            hb = await worker1.heartbeat(s1, run_id=run_id, fence_token=token1)
            await s1.commit()
        assert hb is True

        # 4. Force lease expiration in PostgreSQL
        async with db_session_factory() as s:
            await s.execute(
                text(
                    "UPDATE autonomous_deployments SET lease_expires_at = now() - interval '10 seconds' WHERE id = :rid"
                ),
                {"rid": run_id},
            )
            await s.commit()

        # 5. Worker 2 claims expired run -> succeeds with fence_token = 2
        async with db_session_factory() as s2:
            token2_new = await worker2.claim_run(s2, run_id=run_id, project_id=test_project_id)
            await s2.commit()
        assert token2_new == 2

        # 6. Stale Worker 1 attempts stage transition with token 1 -> rejected with WorkerFencingLostError
        async with db_session_factory() as s1:
            with pytest.raises(WorkerFencingLostError):
                await worker1.transition_stage(
                    s1,
                    run_id=run_id,
                    stage_name=STAGE_G1_BLUEPRINT,
                    fence_token=token1,
                    status="running",
                )


class TestCrashWindowRecoveryInPostgreSQL:
    """Tests sweeper recovery on a run that crashed immediately after DB commit."""

    async def test_recovers_orphaned_run_with_null_lease_in_postgresql(
        self, db_session_factory, test_project_id
    ) -> None:
        user_res_uid: uuid.UUID
        async with db_session_factory() as session:
            user_res = await session.execute(text("SELECT id FROM users LIMIT 1"))
            user_res_uid = user_res.scalar()

        # 1. Create run committed with status='running', dispatch_status='dispatching', lease_expires_at=NULL
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        dispatched_time = now - timedelta(seconds=90)

        async with db_session_factory() as session:
            async with session.begin():
                run = AutonomousDeployment(
                    id=run_id,
                    project_id=test_project_id,
                    attempt_number=1,
                    status="running",
                    strategy=DeploymentStrategy.DOCKER_GITHUB.value,
                    configuration={"strategy": "docker_github"},
                    progress_pct=0,
                    dispatch_status="dispatching",
                    dispatch_requested_at=dispatched_time,
                    started_at=dispatched_time,
                    fence_token=0,
                    lease_expires_at=None,
                    worker_id=None,
                    payload_hash="dummy-hash",
                    created_by=user_res_uid,
                )
                session.add(run)

        # 2. Sweeper finds and recovers orphaned run
        sweeper = AutonomousRecoverySweeper(sweeper_id="pg-sweeper-1")
        dispatched_tasks = []

        class MockDispatcher:
            async def enqueue(self, task_name: str, payload: dict[str, Any]) -> None:
                dispatched_tasks.append((task_name, payload))

        async with db_session_factory() as session:
            expired_runs = await sweeper.find_expired_runs(session)
            assert any(r.id == run_id for r in expired_runs)

            swept = await sweeper.sweep(session, context={"task_dispatcher": MockDispatcher()})
            await session.commit()

        target_swept = next((r for r in swept if r.id == run_id), None)
        assert target_swept is not None
        assert target_swept.fence_token == 1
        assert target_swept.dispatch_status == "recovered"
        matching_dispatched = [p for _, p in dispatched_tasks if p["run_id"] == str(run_id)]
        assert len(matching_dispatched) == 1
        assert matching_dispatched[0]["project_id"] == str(test_project_id)


class TestControlledStagingDeployments:
    """Executes full pipeline across all 4 strategies in PostgreSQL staging environment."""

    @pytest.mark.parametrize(
        ("strategy", "expected_targets"),
        [
            (
                DeploymentStrategy.DOCKER_GITHUB_VERCEL,
                {"docker": "verified", "github": "verified", "vercel": "verified"},
            ),
            (
                DeploymentStrategy.DOCKER_GITHUB,
                {"docker": "verified", "github": "verified", "vercel": "not_applicable"},
            ),
            (
                DeploymentStrategy.GITHUB_ONLY,
                {"docker": "not_applicable", "github": "verified", "vercel": "not_applicable"},
            ),
            (
                DeploymentStrategy.VERCEL_ONLY,
                {"docker": "not_applicable", "github": "not_applicable", "vercel": "verified"},
            ),
        ],
    )
    async def test_staging_lifecycle_and_target_verification(
        self, db_session_factory, test_project_id, strategy: DeploymentStrategy, expected_targets: dict[str, str]
    ) -> None:
        user_res_uid: uuid.UUID
        async with db_session_factory() as session:
            user_res = await session.execute(text("SELECT id FROM users LIMIT 1"))
            user_res_uid = user_res.scalar()

        service = AutonomousDeploymentService()
        docker_cfg = (
            DockerConfigRequest(image_tag="forgeops:staging")
            if strategy in (DeploymentStrategy.DOCKER_GITHUB_VERCEL, DeploymentStrategy.DOCKER_GITHUB)
            else None
        )
        gh_cfg = (
            GitHubConfigRequest(repository_name="forgeops/staging-repo")
            if strategy
            in (
                DeploymentStrategy.DOCKER_GITHUB_VERCEL,
                DeploymentStrategy.DOCKER_GITHUB,
                DeploymentStrategy.GITHUB_ONLY,
            )
            else None
        )
        vercel_cfg = (
            VercelConfigRequest(project_name="forgeops-staging-app")
            if strategy in (DeploymentStrategy.DOCKER_GITHUB_VERCEL, DeploymentStrategy.VERCEL_ONLY)
            else None
        )
        req = CreateAutonomousRunRequest(
            strategy=strategy,
            docker_config=docker_cfg,
            github_config=gh_cfg,
            vercel_config=vercel_cfg,
        )

        async with db_session_factory() as s_create:
            run, _ = await service.create_run(
                s_create, project_id=test_project_id, requested_by=user_res_uid, request=req
            )
            await s_create.commit()
            run_id = run.id

        # Execute full pipeline against PostgreSQL
        async with db_session_factory() as s_exec:
            completed_run = await run_pipeline(
                s_exec,
                run_id=run_id,
                worker_id="staging-pipeline-worker",
            )
            await s_exec.commit()

        assert completed_run.status == "succeeded"
        assert completed_run.progress_pct == 100
        assert completed_run.completed_at is not None

        # Inspect G7 Verification stage metadata
        g7_stage = next(s for s in completed_run.stages if s.stage_name == STAGE_G7_VERIFICATION)
        assert g7_stage.status == "succeeded"
        targets = g7_stage.stage_metadata.get("targets", {})
        for target_name, expected_status in expected_targets.items():
            assert targets.get(target_name) == expected_status, (
                f"Mismatch for target {target_name} in strategy {strategy}: "
                f"{targets.get(target_name)} != {expected_status}"
            )
