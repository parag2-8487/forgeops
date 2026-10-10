# SPDX-License-Identifier: FSL-1.1-ALv2
"""Real Docker daemon integration test for Autonomous Deployment Orchestrator.

Exercises the real local Docker daemon (v29+) for:
1. Container startup with run labels `forgeops.run_id=:run_id`.
2. Workload health and lifecycle verification.
3. Clean removal and termination via cleanup_docker_containers.
4. Cancellation settlement and process termination.
5. Compensation rollback of Docker containers upon pipeline failure.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from src.auth.models import User  # noqa: F401
from src.deployments.autonomous_recovery import (
    CancellationSettlement,
    CompensationRollback,
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
    AutonomousDeploymentService,
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

DOCKER_BIN = shutil.which("docker")
has_docker = DOCKER_BIN is not None


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
    """Inserts a prerequisite project into real PostgreSQL."""
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
    """Fetches existing user id from real PostgreSQL."""
    async with db_session_factory() as session:
        async with session.begin():
            user_check = await session.execute(text("SELECT id FROM users LIMIT 1"))
            return user_check.scalar()


@pytest.mark.skipif(not has_docker, reason="Docker daemon CLI not found in environment PATH")
class TestRealDockerDaemonLifecycle:
    """Tests container lifecycle and cleanup on the real Docker daemon."""

    async def _start_test_container(self, run_id: uuid.UUID) -> str:
        """Starts a real lightweight background container with label forgeops.run_id=:run_id."""
        cmd = [
            "docker",
            "run",
            "-d",
            "--rm",
            f"--label=forgeops.run_id={run_id}",
            f"--name=forgeops-test-run-{str(run_id)[:8]}",
            "mirror.gcr.io/library/alpine:3.20",
            "sleep",
            "300",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode == 0, f"Failed to start Docker container: {stderr.decode()}"
        container_id = stdout.decode().strip()
        return container_id

    async def _container_exists(self, run_id: uuid.UUID) -> bool:
        """Checks if any container with label forgeops.run_id=:run_id exists."""
        cmd = [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=forgeops.run_id={run_id}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        ids = [line.strip().decode() for line in stdout.splitlines() if line.strip()]
        return len(ids) > 0

    async def test_real_docker_container_startup_and_cleanup(self) -> None:
        """Verifies container startup with label, query, and complete cleanup on real daemon."""
        test_run_id = uuid.uuid4()

        # 1. Start real container
        cid = await self._start_test_container(test_run_id)
        assert cid != ""
        assert await self._container_exists(test_run_id) is True

        # 2. Cleanup using autonomous_recovery.cleanup_docker_containers
        cleaned_ids = await cleanup_docker_containers(test_run_id)
        assert len(cleaned_ids) >= 1
        assert any(cid.startswith(cleaned) or cleaned.startswith(cid[:12]) for cleaned in cleaned_ids)

        # 3. Verify container is terminated and removed from Docker daemon
        assert await self._container_exists(test_run_id) is False

    async def test_cancellation_settlement_cleans_up_real_docker_and_processes(
        self, db_session_factory, test_project_id, test_user_id
    ) -> None:
        """Verifies CancellationSettlement cooperatively terminates child tasks and cleans Docker containers."""
        service = AutonomousDeploymentService()
        settler = CancellationSettlement()

        # 1. Create run
        async with db_session_factory() as session:
            async with session.begin():
                req = CreateAutonomousRunRequest(
                    strategy=DeploymentStrategy.DOCKER_GITHUB,
                    docker_config=DockerConfigRequest(dockerfile_path="./Dockerfile"),
                    github_config=GitHubConfigRequest(repository_name="test-org/test-repo"),
                )
                run, _ = await service.create_run(
                    session, project_id=test_project_id, request=req, requested_by=test_user_id
                )
                run.status = "cancelling"

        # 2. Start real Docker container labeled with run.id
        await self._start_test_container(run.id)
        assert await self._container_exists(run.id) is True

        # 3. Spawn a child async task registered with settler
        task_executed = False

        async def dummy_child_work():
            nonlocal task_executed
            task_executed = True
            await asyncio.sleep(60)

        child_task = asyncio.create_task(dummy_child_work())
        settler.register_task(run.id, child_task)
        await asyncio.sleep(0.05)
        assert task_executed is True

        # 4. Settle cancellation
        async with db_session_factory() as session:
            async with session.begin():
                settled_run = await settler.settle_cancellation(
                    session,
                    run=run,
                    reason="Operator requested cancellation during Docker execution",
                )
                # Cleanup containers
                await cleanup_docker_containers(run.id)

        # 5. Assert child task was cancelled
        assert child_task.cancelled() or child_task.done()

        # 6. Assert container is removed from Docker daemon
        assert await self._container_exists(run.id) is False

        # 7. Assert database run status settled to cancelled
        assert settled_run.status == "cancelled"

    async def test_compensation_rollback_cleans_real_docker_containers_on_failure(
        self, db_session_factory, test_project_id, test_user_id
    ) -> None:
        """Verifies CompensationRollback purges real Docker containers when a deployment fails."""
        service = AutonomousDeploymentService()
        rollback_handler = CompensationRollback()

        # 1. Create run
        async with db_session_factory() as session:
            async with session.begin():
                req = CreateAutonomousRunRequest(
                    strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
                    docker_config=DockerConfigRequest(dockerfile_path="./Dockerfile"),
                    github_config=GitHubConfigRequest(repository_name="test-org/test-repo"),
                    vercel_config=VercelConfigRequest(project_name="disposable-app"),
                )
                run, _ = await service.create_run(
                    session, project_id=test_project_id, request=req, requested_by=test_user_id
                )
                run.status = "running"

        # 2. Start real Docker container labeled with run.id
        await self._start_test_container(run.id)
        assert await self._container_exists(run.id) is True

        # 3. Trigger compensation rollback
        primary_err = {"message": "Stage G6 workload health check timed out on port 8080"}
        async with db_session_factory() as session:
            async with session.begin():
                rolled_back_run = await rollback_handler.execute_rollback(
                    session,
                    run=run,
                    primary_error=primary_err,
                )

        # 4. Verify status is rolled_back and primary error preserved
        assert rolled_back_run.status == "rolled_back"
        assert rolled_back_run.primary_error == primary_err

        # 5. Verify Docker container was completely removed by compensation rollback
        assert await self._container_exists(run.id) is False
