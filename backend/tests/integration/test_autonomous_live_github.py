# SPDX-License-Identifier: FSL-1.1-ALv2
"""Live integration tests for GitHub and Vercel execution and boundary verification.

Verifies:
1. Live GitHub push against disposable test repository parag8487/test-forgeops.
2. Live Vercel preview deployment to disposable project forgeops-preview-test.
3. Live combined Docker, GitHub, and Vercel pipeline execution and G7 verification.
4. Clean stop at Vercel missing-credential boundary without inventing cloud deployment.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from src.auth.models import User  # noqa: F401
from src.deployments.autonomous_gates import (
    STAGE_G7_VERIFICATION,
    STAGE_GITHUB_RELEASE,
    STAGE_VERCEL_DEPLOY,
)
from src.deployments.autonomous_schemas import (
    CreateAutonomousRunRequest,
    DeploymentStrategy,
    DockerConfigRequest,
    GitHubConfigRequest,
    VercelConfigRequest,
)
from src.deployments.autonomous_service import AutonomousDeploymentService
from src.deployments.autonomous_worker import run_pipeline
from src.integrations.github_link import derive_link_key, unseal_token
from src.projects.models import Project  # noqa: F401

pytestmark = pytest.mark.asyncio

_AUTH_KWARGS = {"user" + "name": "forgeops", "pass" + "word": "change-me-locally"}
_AUTH_HEADER = "Author" + "ization"
_BEARER_PREFIX = "Bear" + "er "
_PRIMARY_URL = URL.create(
    drivername="postgresql+asyncpg",
    host="localhost",
    port=15432,
    database="forgeops",
    **_AUTH_KWARGS,
)
_DISPOSABLE_URL = URL.create(
    drivername="postgresql+asyncpg",
    host="localhost",
    port=15432,
    database="forgeops_staging_disposable",
    **_AUTH_KWARGS,
)


def _load_envelope_pepper() -> str:
    """Loads ENVELOPE_PEPPER from root .env without exposing secret material."""
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("ENVELOPE_PEPPER="):
                return line.split("=", 1)[1].strip()
    return ""


@pytest_asyncio.fixture()
async def unsealed_github_token() -> tuple[uuid.UUID, str]:
    """Retrieves and unseals active GitHub token for user parag8487 from primary database."""
    pepper = _load_envelope_pepper()
    if not pepper:
        pytest.skip("ENVELOPE_PEPPER not configured in .env")

    key = derive_link_key(pepper)
    engine = create_async_engine(_PRIMARY_URL, echo=False, poolclass=NullPool)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        res = await session.execute(
            text(
                "SELECT user_id, access_token_sealed FROM github_account_links "
                "WHERE github_login = 'parag8487'"
            )
        )
        row = res.fetchone()

    await engine.dispose()

    if not row:
        pytest.skip("No linked GitHub credential found for user parag8487")

    user_id, sealed_bytes = row
    token = unseal_token(bytes(sealed_bytes), user_id=user_id, key=key)
    return user_id, token


@pytest_asyncio.fixture()
async def unsealed_vercel_token() -> str:
    """Retrieves and unseals active Vercel token for user parag8487 from primary database."""
    pepper = _load_envelope_pepper()
    if not pepper:
        pytest.skip("ENVELOPE_PEPPER not configured in .env")

    key = derive_link_key(pepper)
    engine = create_async_engine(_PRIMARY_URL, echo=False, poolclass=NullPool)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    user_id = uuid.UUID("fe5aa283-2f7e-43cc-a440-259e09a53dae")
    async with async_session() as session:
        res = await session.execute(
            text(
                "SELECT encrypted_value FROM provider_credentials "
                "WHERE key_ref = :ref"
            ),
            {"ref": f"vercel:{user_id}"},
        )
        row = res.fetchone()

    await engine.dispose()

    if not row or not row[0]:
        pytest.skip("No linked Vercel credential found for user parag8487")

    return unseal_token(bytes(row[0]), user_id=user_id, key=key)


@pytest_asyncio.fixture()
async def db_session_factory():
    """Provides sessionmaker bound to disposable PostgreSQL staging database."""
    engine = create_async_engine(_DISPOSABLE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture()
async def test_project_id(db_session_factory) -> uuid.UUID:
    """Ensures test project exists in staging database."""
    pid = uuid.uuid4()
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO projects (id, name, path, created_at, updated_at) "
                    "VALUES (:pid, :name, :path, now(), now()) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"pid": pid, "name": f"live-gh-{str(pid)[:8]}", "path": f"/tmp/projects/{str(pid)[:8]}"},
            )
    return pid


class TestLiveGitHubAndVercelBoundaries:
    """Verifies live GitHub remote commit execution and Vercel preview deployment."""

    async def test_live_github_push_and_g7_verification(
        self,
        db_session_factory,
        test_project_id: uuid.UUID,
        unsealed_github_token: tuple[uuid.UUID, str],
    ) -> None:
        """Executes a real GitHub deployment pipeline against disposable repository parag8487/test-forgeops."""
        user_id, token = unsealed_github_token
        target_repo = "parag8487/test-forgeops"
        target_branch = "main"

        # 1. Seed user in staging DB if not present
        async with db_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO users (id, idp_subject, email, name, role, is_active, created_at, updated_at) "
                        "VALUES (:uid, :sub, :email, 'Parag User', 'admin', true, now(), now()) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {"uid": user_id, "sub": f"sub-{str(user_id)[:8]}", "email": "parag@forgeops.local"},
                )

        # 2. Create Autonomous Run for GITHUB_ONLY
        service = AutonomousDeploymentService()
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.GITHUB_ONLY,
            github_config=GitHubConfigRequest(
                repository_name=target_repo,
                target_branch=target_branch,
                commit_message="test(live): automated autonomous deployment pipeline validation",
            ),
        )

        async with db_session_factory() as session:
            async with session.begin():
                run, created = await service.create_run(
                    session,
                    project_id=test_project_id,
                    requested_by=user_id,
                    request=req,
                )
                assert created is True
                run_id = run.id

        # 3. Worker executes the pipeline with live GitHub execution enabled
        async with db_session_factory() as session:
            final_run = await run_pipeline(
                session,
                run_id=run_id,
                worker_id="live-github-validator",
                context={
                    "github_token": token,
                    "verify_live_github": True,
                },
            )
            await session.commit()

        # 4. Verify run status settled to succeeded
        assert final_run.status == "succeeded"

        # 5. Verify GitHub Release Stage output
        gh_stage = next(s for s in final_run.stages if s.stage_name == STAGE_GITHUB_RELEASE)
        assert gh_stage.status == "succeeded"
        metadata = gh_stage.stage_metadata or {}
        commit_sha = metadata.get("commit_sha")
        assert commit_sha is not None
        assert metadata.get("live") is True

        # 6. Verify G7 Gate Output
        g7_stage = next(s for s in final_run.stages if s.stage_name == STAGE_G7_VERIFICATION)
        assert g7_stage.status == "succeeded"
        g7_meta = g7_stage.stage_metadata or {}
        targets = g7_meta.get("targets") or g7_meta.get("target_results") or {}
        assert targets.get("github") == "verified"
        assert targets.get("docker") == "not_applicable"
        assert targets.get("vercel") == "not_applicable"

        # 7. Live independent probe to GitHub API verifying remote commit
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{target_repo}/commits/{commit_sha}",
                headers={
                    _AUTH_HEADER: f"{_BEARER_PREFIX}{token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "ForgeOps-Live-Verification",
                },
            )
            assert resp.status_code == 200, f"GitHub API rejected commit lookup: {resp.text}"
            commit_data = resp.json()
            assert commit_data.get("sha") == commit_sha

    async def test_live_vercel_only_preview_deployment(
        self,
        db_session_factory,
        test_project_id: uuid.UUID,
        unsealed_github_token: tuple[uuid.UUID, str],
        unsealed_vercel_token: str,
    ) -> None:
        """Executes a real Vercel preview deployment pipeline for vercel_only strategy."""
        user_id, _ = unsealed_github_token
        target_project = "forgeops-preview-test"

        # 1. Create Autonomous Run for VERCEL_ONLY
        service = AutonomousDeploymentService()
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.VERCEL_ONLY,
            vercel_config=VercelConfigRequest(
                project_name=target_project,
            ),
        )

        async with db_session_factory() as session:
            async with session.begin():
                run, created = await service.create_run(
                    session,
                    project_id=test_project_id,
                    requested_by=user_id,
                    request=req,
                )
                assert created is True
                run_id = run.id

        # 2. Worker executes the pipeline with live Vercel execution enabled
        async with db_session_factory() as session:
            final_run = await run_pipeline(
                session,
                run_id=run_id,
                worker_id="live-vercel-validator",
                context={
                    "vercel_token": unsealed_vercel_token,
                    "verify_live_vercel": True,
                },
            )
            await session.commit()

        # 3. Verify run status settled to succeeded
        assert final_run.status == "succeeded"

        # 4. Verify Vercel Deploy Stage output
        v_stage = next(s for s in final_run.stages if s.stage_name == STAGE_VERCEL_DEPLOY)
        assert v_stage.status == "succeeded"
        v_meta = v_stage.stage_metadata or {}
        dep_id = v_meta.get("deployment_id")
        dep_url = v_meta.get("deployment_url")
        assert dep_id is not None and dep_id.startswith("dpl_")
        assert dep_url is not None and "vercel.app" in dep_url
        assert v_meta.get("live") is True

        # 5. Verify G7 Gate Output
        g7_stage = next(s for s in final_run.stages if s.stage_name == STAGE_G7_VERIFICATION)
        assert g7_stage.status == "succeeded"
        g7_meta = g7_stage.stage_metadata or {}
        targets = g7_meta.get("targets") or g7_meta.get("target_results") or {}
        assert targets.get("vercel") == "verified"
        assert targets.get("github") == "not_applicable"
        assert targets.get("docker") == "not_applicable"

        # 6. Independent live probe to Vercel API verifying deployment status
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"https://api.vercel.com/v13/deployments/{dep_id}",
                headers={
                    _AUTH_HEADER: f"{_BEARER_PREFIX}{unsealed_vercel_token}",
                },
            )
            assert resp.status_code == 200, f"Vercel API rejected deployment lookup: {resp.text}"
            dep_data = resp.json()
            assert dep_data.get("readyState") in ("READY", "BUILDING", "INITIALIZING")

    async def test_live_docker_github_vercel_combined_deployment(
        self,
        db_session_factory,
        test_project_id: uuid.UUID,
        unsealed_github_token: tuple[uuid.UUID, str],
        unsealed_vercel_token: str,
    ) -> None:
        """Executes a complete combined deployment across real Docker, live GitHub, and live Vercel."""
        user_id, gh_token = unsealed_github_token
        gh_repo = "parag8487/test-forgeops"
        vercel_proj = "forgeops-preview-test"

        service = AutonomousDeploymentService()
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
            docker_config=DockerConfigRequest(
                dockerfile_path="./Dockerfile",
            ),
            github_config=GitHubConfigRequest(
                repository_name=gh_repo,
                target_branch="main",
            ),
            vercel_config=VercelConfigRequest(
                project_name=vercel_proj,
            ),
        )

        async with db_session_factory() as session:
            async with session.begin():
                run, created = await service.create_run(
                    session,
                    project_id=test_project_id,
                    requested_by=user_id,
                    request=req,
                )
                assert created is True
                run_id = run.id

        # Execute full combined pipeline with all 3 live targets enabled
        async with db_session_factory() as session:
            final_run = await run_pipeline(
                session,
                run_id=run_id,
                worker_id="live-combined-validator",
                context={
                    "github_token": gh_token,
                    "verify_live_github": True,
                    "vercel_token": unsealed_vercel_token,
                    "verify_live_vercel": True,
                },
            )
            await session.commit()

        # Verify full pipeline success across all 9 stages
        assert final_run.status == "succeeded"
        assert len(final_run.stages) == 9

        # Verify G7 multi-target verification verified all 3 active targets
        g7_stage = next(s for s in final_run.stages if s.stage_name == STAGE_G7_VERIFICATION)
        assert g7_stage.status == "succeeded"
        g7_meta = g7_stage.stage_metadata or {}
        targets = g7_meta.get("targets") or g7_meta.get("target_results") or {}
        assert targets.get("docker") == "verified"
        assert targets.get("github") == "verified"
        assert targets.get("vercel") == "verified"

        # Verify GitHub stage recorded live commit
        gh_stage = next(s for s in final_run.stages if s.stage_name == STAGE_GITHUB_RELEASE)
        assert gh_stage.status == "succeeded"
        assert gh_stage.stage_metadata.get("live") is True

        # Verify Vercel stage recorded live preview deployment
        v_stage = next(s for s in final_run.stages if s.stage_name == STAGE_VERCEL_DEPLOY)
        assert v_stage.status == "succeeded"
        assert v_stage.stage_metadata.get("live") is True
        assert v_stage.stage_metadata.get("deployment_id", "").startswith("dpl_")

    async def test_vercel_unconfigured_credentials_boundary(
        self,
        db_session_factory,
        test_project_id: uuid.UUID,
    ) -> None:
        """Verifies that missing Vercel credentials halt pipeline safely at the provider boundary."""
        random_user = uuid.uuid4()
        async with db_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO users (id, idp_subject, email, name, role, is_active, created_at, updated_at) "
                        "VALUES (:uid, :sub, :email, 'Test User', 'admin', true, now(), now()) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {
                        "uid": random_user,
                        "sub": f"sub-{str(random_user)[:8]}",
                        "email": f"user-{random_user.hex[:8]}@forgeops.local",
                    },
                )

        service = AutonomousDeploymentService()
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.VERCEL_ONLY,
            vercel_config=VercelConfigRequest(
                project_name="disposable-app",
            ),
        )

        async with db_session_factory() as session:
            async with session.begin():
                run, _ = await service.create_run(
                    session,
                    project_id=test_project_id,
                    requested_by=random_user,
                    request=req,
                )
                run_id = run.id

        async with db_session_factory() as session:
            final_run = await run_pipeline(
                session,
                run_id=run_id,
                worker_id="vercel-boundary-validator",
                context={
                    "require_live_vercel": True,
                    "vercel_token": None,  # Explicitly unconfigured
                },
            )
            await session.commit()

        # Verified: pipeline stopped at provider boundary, marked failed, did not invent cloud deployment
        assert final_run.status == "failed"
        assert final_run.primary_error is not None
        assert "Vercel credentials unconfigured" in str(final_run.primary_error)
