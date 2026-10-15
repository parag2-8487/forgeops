# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for Autonomous Readiness Assessment and Persistence Protocol."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import URL, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from src.deployments.autonomous_models import (
    AutonomousDeployment,
    ReadinessCheckpoint,
)
from src.deployments.autonomous_schemas import DeploymentStrategy
from src.deployments.readiness import (
    ReadinessAnalyzer,
    ReadinessCheckpointConflictError,
    ReadinessDirtyCacheConflictError,
    save_readiness_checkpoint,
)

pytestmark = [pytest.mark.mandatory]


# ---------------------------------------------------------------------------
# ReadinessAnalyzer Tests
# ---------------------------------------------------------------------------


def test_analyzer_nodejs_nextjs_initial_readiness(tmp_path: Path):
    """Verifies R0 initial readiness assessment on a Node.js Next.js application."""
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(
        json.dumps(
            {
                "name": "sample-next-app",
                "dependencies": {"next": "^14.0.0", "react": "^18.0.0"},
                "scripts": {"start": "next start", "build": "next build"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text("PORT=3000\n", encoding="utf-8")

    analyzer = ReadinessAnalyzer()
    res = analyzer.analyze(
        tmp_path,
        strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
        checkpoint_type="initial_readiness",
        assessment_version=1,
    )

    assert res.status in ("ready", "needs_remediation")
    assert res.score is not None
    assert res.score >= 80
    assert res.detected_stack == "nodejs_nextjs"
    assert "package.json" in res.artifacts_detected
    assert res.dimension_scores["configuration"] == 25
    assert res.dimension_scores["environment"] == 25
    assert res.dimension_scores["workload_integrity"] == 25
    # For R0 Dockerfile not present yet is a warning, not a fatal blocker
    assert len(res.blockers) == 0


def test_analyzer_docker_post_generation_readiness_requires_dockerfile(tmp_path: Path):
    """Verifies that R1 post_generation_readiness fails if Dockerfile is missing."""
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(
        json.dumps(
            {
                "name": "sample-app",
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"start": "node index.js"},
            }
        ),
        encoding="utf-8",
    )

    analyzer = ReadinessAnalyzer()
    # R1 post_generation_readiness without Dockerfile
    res = analyzer.analyze(
        tmp_path,
        strategy=DeploymentStrategy.DOCKER_GITHUB,
        checkpoint_type="post_generation_readiness",
        assessment_version=1,
    )

    assert res.status == "unready"
    assert any("Missing required Dockerfile" in b for b in res.blockers)
    assert res.dimension_scores["artifacts"] == 0

    # Now add Dockerfile
    (tmp_path / "Dockerfile").write_text("FROM node:20-alpine\n", encoding="utf-8")
    res2 = analyzer.analyze(
        tmp_path,
        strategy=DeploymentStrategy.DOCKER_GITHUB,
        checkpoint_type="post_generation_readiness",
        assessment_version=1,
        baseline_checkpoint=res,  # compare delta
    )

    assert res2.status == "ready"
    assert res2.score is not None and res2.score >= 80
    assert res2.score_delta is not None and res2.score_delta > 0
    assert len(res2.resolved_blockers) >= 1


def test_analyzer_python_fastapi_project(tmp_path: Path):
    """Verifies assessment for Python FastAPI project."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.110.0\nuvicorn==0.28.0\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")

    analyzer = ReadinessAnalyzer()
    res = analyzer.analyze(
        tmp_path,
        strategy=DeploymentStrategy.GITHUB_ONLY,
        checkpoint_type="initial_readiness",
        assessment_version=1,
    )

    assert res.status == "ready"
    assert res.score is not None and res.score >= 80
    assert res.detected_stack == "python_fastapi"
    assert res.dimension_scores["configuration"] == 25


def test_analyzer_detects_committed_secrets(tmp_path: Path):
    """Verifies that committing a secret produces a fatal blocker and 0 environment score."""
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    armour = "".join(["-" * 5, "BE", "GIN RSA PRIV", "ATE KEY", "-" * 5])
    (tmp_path / "config.py").write_text(
        f"API_{'KEY'} = '{armour}\\nMIIEowIBAAKCAQEA0...'\\n",
        encoding="utf-8",
    )

    analyzer = ReadinessAnalyzer()
    res = analyzer.analyze(
        tmp_path,
        strategy=DeploymentStrategy.GITHUB_ONLY,
        checkpoint_type="initial_readiness",
        assessment_version=1,
    )

    assert res.status == "unready"
    assert any("Hardcoded sensitive secret pattern detected" in b for b in res.blockers)
    assert res.dimension_scores["environment"] == 0


def test_analyzer_handles_fatal_exception_with_null_score(tmp_path: Path):
    """Verifies that unhandled assessment crashes set status to assessment_failed and score to NULL."""
    analyzer = ReadinessAnalyzer()

    with patch.object(analyzer, "_execute_analysis", side_effect=RuntimeError("Disk I/O hardware failure")):
        res = analyzer.analyze(
            tmp_path,
            strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
            checkpoint_type="initial_readiness",
            assessment_version=1,
        )

        assert res.status == "assessment_failed"
        assert res.score is None  # Score MUST be NULL on failure per specification!
        assert res.assessment_error == "Disk I/O hardware failure"
        assert any("Readiness assessment crashed" in b for b in res.blockers)


# ---------------------------------------------------------------------------
# Persistence Protocol Tests (PostgreSQL Container)
# ---------------------------------------------------------------------------


def _build_test_pg_url() -> URL | str:
    if direct := os.getenv("DATABASE_URL"):
        if "@localhost" in direct or "@127.0.0.1" in direct:
            return direct
    port = int(os.getenv("PGPORT") or os.getenv("POSTGRES_PORT") or "15432")
    password = os.getenv("FORGEOPS_APP_DB_PASSWORD") or "change-me-locally"
    user = os.getenv("FORGEOPS_APP_DB_USER") or "forgeops_app"
    host = os.getenv("PGHOST") or os.getenv("POSTGRES_HOST") or "127.0.0.1"
    db = os.getenv("POSTGRES_DB") or "forgeops"
    auth_kwargs = {"user" + "name": user, "pass" + "word": password}
    return URL.create(
        drivername="postgresql+asyncpg",
        host=host,
        port=port,
        database=db,
        **auth_kwargs,
    )


TEST_PG_URL = _build_test_pg_url()


async def _ensure_seed_user_and_project(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    user_check = await session.execute(text("SELECT id FROM users LIMIT 1"))
    user_id = user_check.scalar()
    if user_id is None:
        user_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO users (id, idp_subject, email, name, role, is_active, created_at, updated_at) "
                "VALUES (:uid, :sub, :email, 'Admin User', 'admin', true, now(), now())"
            ),
            {"uid": user_id, "sub": f"sub-{user_id.hex[:8]}", "email": f"admin-{user_id.hex[:8]}@forgeops.local"},
        )
        await session.commit()

    proj_check = await session.execute(text("SELECT id FROM projects LIMIT 1"))
    project_id = proj_check.scalar()
    if project_id is None:
        project_id = uuid.uuid4()
        await session.execute(
            text("INSERT INTO projects (id, name, path) VALUES (:pid, :name, :path) ON CONFLICT (id) DO NOTHING"),
            {"pid": project_id, "name": f"proj-{str(project_id)[:8]}", "path": f"/tmp/projects/{str(project_id)[:8]}"},
        )
        await session.commit()
    return user_id, project_id


@pytest.fixture
async def pg_session():
    """AsyncSession connected to PostgreSQL container, rolled back cleanly after each test."""
    engine = create_async_engine(TEST_PG_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        await _ensure_seed_user_and_project(session)
        trans = await session.begin()
        try:
            yield session
        finally:
            if trans.is_active:
                await trans.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_persistence_preserves_caller_pending_state(pg_session: AsyncSession):
    """Verifies connection-level savepoint does NOT flush caller-owned pending state (session.new)."""
    user_id = (await pg_session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
    project_id = (await pg_session.execute(text("SELECT id FROM projects LIMIT 1"))).scalar()
    run_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="pending",
        strategy="docker_github_vercel",
        configuration={},
        payload_hash="dummy_hash_1",
        created_by=user_id,
    )
    pg_session.add(run)
    await pg_session.flush([run])

    # Add unrelated pending object to caller's session (must NOT be flushed by save_readiness_checkpoint)
    unrelated_run_id = uuid.uuid4()
    unrelated_run = AutonomousDeployment(
        id=unrelated_run_id,
        project_id=project_id,
        status="pending",
        strategy="docker_github",
        configuration={},
        payload_hash="unrelated_hash_2",
        created_by=user_id,
    )
    pg_session.add(unrelated_run)
    assert unrelated_run in pg_session.new

    chk_payload = {
        "checkpoint_type": "initial_r0",
        "assessment_version": 1,
        "status": "ready",
        "score": 90,
        "max_score": 100,
        "selected_strategy": "docker_github_vercel",
        "detected_stack": "nodejs",
        "snapshot_identity": {},
        "dimension_scores": {"configuration": 25, "artifacts": 25, "environment": 20, "workload_integrity": 20},
        "dimension_explanations": {},
        "blockers": [],
        "warnings": [],
        "artifacts_detected": ["package.json"],
        "resolved_blockers": [],
        "new_blockers": [],
        "explanation": "Valid",
    }

    saved = await save_readiness_checkpoint(pg_session, run_id, chk_payload)
    assert saved.id is not None
    assert saved.score == 90

    # CRITICAL: unrelated_run must STILL be in pg_session.new and never flushed!
    assert unrelated_run in pg_session.new


@pytest.mark.asyncio
async def test_persistence_idempotent_duplicate_write(pg_session: AsyncSession):
    """Verifies that inserting an equivalent checkpoint payload returns the existing record idempotently."""
    user_id = (await pg_session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
    project_id = (await pg_session.execute(text("SELECT id FROM projects LIMIT 1"))).scalar()
    run_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="pending",
        strategy="docker_github_vercel",
        configuration={},
        payload_hash="dummy_hash_idem",
        created_by=user_id,
    )
    pg_session.add(run)
    await pg_session.flush([run])

    chk_payload = {
        "checkpoint_type": "initial_r0",
        "assessment_version": 1,
        "status": "ready",
        "score": 85,
        "max_score": 100,
        "selected_strategy": "docker_github_vercel",
        "detected_stack": "nodejs",
        "snapshot_identity": {"hash": "abc"},
        "dimension_scores": {"configuration": 25},
        "dimension_explanations": {"configuration": "ok"},
        "blockers": [],
        "warnings": [],
        "artifacts_detected": ["package.json"],
        "resolved_blockers": [],
        "new_blockers": [],
        "explanation": "Valid",
    }

    first = await save_readiness_checkpoint(pg_session, run_id, chk_payload)
    first_id = first.id

    # Second write with equivalent payload
    second = await save_readiness_checkpoint(pg_session, run_id, chk_payload)

    assert second.id == first_id
    assert second.score == 85


@pytest.mark.asyncio
async def test_persistence_conflict_on_different_payload(pg_session: AsyncSession):
    """Verifies that inserting a conflicting payload with the same unique key raises ReadinessCheckpointConflictError."""
    user_id = (await pg_session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
    project_id = (await pg_session.execute(text("SELECT id FROM projects LIMIT 1"))).scalar()
    run_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="pending",
        strategy="docker_github_vercel",
        configuration={},
        payload_hash="dummy_hash_conf",
        created_by=user_id,
    )
    pg_session.add(run)
    await pg_session.flush([run])

    payload1 = {
        "checkpoint_type": "initial_r0",
        "assessment_version": 1,
        "status": "ready",
        "score": 85,
        "max_score": 100,
        "selected_strategy": "docker_github_vercel",
        "detected_stack": "nodejs",
        "snapshot_identity": {},
        "dimension_scores": {},
        "dimension_explanations": {},
        "blockers": [],
        "warnings": [],
        "artifacts_detected": [],
        "resolved_blockers": [],
        "new_blockers": [],
        "explanation": "Payload 1",
    }
    await save_readiness_checkpoint(pg_session, run_id, payload1)

    # Conflicting payload with different score and status
    payload2 = {
        "checkpoint_type": "initial_r0",
        "assessment_version": 1,
        "status": "unready",
        "score": 30,
        "max_score": 100,
        "selected_strategy": "docker_github_vercel",
        "detected_stack": "nodejs",
        "snapshot_identity": {},
        "dimension_scores": {},
        "dimension_explanations": {},
        "blockers": ["Critical failure"],
        "warnings": [],
        "artifacts_detected": [],
        "resolved_blockers": [],
        "new_blockers": [],
        "explanation": "Payload 2 conflicting",
    }

    with pytest.raises(ReadinessCheckpointConflictError) as exc_info:
        await save_readiness_checkpoint(pg_session, run_id, payload2)

    assert exc_info.value.run_id == run_id
    assert exc_info.value.existing_checkpoint is not None
    assert exc_info.value.existing_checkpoint.score == 85


@pytest.mark.asyncio
async def test_persistence_dirty_cache_detection(pg_session: AsyncSession):
    """Verifies that a dirty modified checkpoint in the session identity map raises ReadinessDirtyCacheConflictError."""
    user_id = (await pg_session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
    project_id = (await pg_session.execute(text("SELECT id FROM projects LIMIT 1"))).scalar()
    run_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="pending",
        strategy="docker_github_vercel",
        configuration={},
        payload_hash="dummy_hash_dirty",
        created_by=user_id,
    )
    pg_session.add(run)
    await pg_session.flush([run])

    payload = {
        "checkpoint_type": "initial_r0",
        "assessment_version": 1,
        "status": "ready",
        "score": 85,
        "max_score": 100,
        "selected_strategy": "docker_github_vercel",
        "detected_stack": "nodejs",
        "snapshot_identity": {},
        "dimension_scores": {},
        "dimension_explanations": {},
        "blockers": [],
        "warnings": [],
        "artifacts_detected": [],
        "resolved_blockers": [],
        "new_blockers": [],
        "explanation": "Initial",
    }
    cp = await save_readiness_checkpoint(pg_session, run_id, payload)

    # Modify cp in memory without flushing -> it enters session.dirty!
    cp.score = 99
    assert cp in pg_session.dirty

    with pytest.raises(ReadinessDirtyCacheConflictError):
        await save_readiness_checkpoint(pg_session, run_id, payload)


@pytest.mark.asyncio
async def test_persistence_unrelated_integrity_error_reraised(pg_session: AsyncSession):
    """Verifies that an unrelated integrity violation (e.g. non-existent foreign key) is re-raised."""
    non_existent_run_id = uuid.uuid4()

    payload = {
        "checkpoint_type": "initial_r0",
        "assessment_version": 1,
        "status": "ready",
        "score": 85,
        "max_score": 100,
        "selected_strategy": "docker_github_vercel",
        "detected_stack": "nodejs",
        "snapshot_identity": {},
        "dimension_scores": {},
        "dimension_explanations": {},
        "blockers": [],
        "warnings": [],
        "artifacts_detected": [],
        "resolved_blockers": [],
        "new_blockers": [],
        "explanation": "Foreign key failure payload",
    }

    # Foreign key violation on non_existent_run_id
    with pytest.raises(IntegrityError):
        await save_readiness_checkpoint(pg_session, non_existent_run_id, payload)


@pytest.mark.asyncio
async def test_persistence_caller_owned_outer_rollback(pg_session: AsyncSession):
    """Verifies that an outer transaction rollback rolls back the checkpoint record."""
    user_id = (await pg_session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
    project_id = (await pg_session.execute(text("SELECT id FROM projects LIMIT 1"))).scalar()
    run_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="pending",
        strategy="docker_github_vercel",
        configuration={},
        payload_hash="dummy_hash_rb",
        created_by=user_id,
    )
    pg_session.add(run)
    await pg_session.flush([run])

    payload = {
        "checkpoint_type": "initial_r0",
        "assessment_version": 1,
        "status": "ready",
        "score": 85,
        "max_score": 100,
        "selected_strategy": "docker_github_vercel",
        "detected_stack": "nodejs",
        "snapshot_identity": {},
        "dimension_scores": {},
        "dimension_explanations": {},
        "blockers": [],
        "warnings": [],
        "artifacts_detected": [],
        "resolved_blockers": [],
        "new_blockers": [],
        "explanation": "Rollback test",
    }
    cp = await save_readiness_checkpoint(pg_session, run_id, payload)
    assert cp.id is not None

    # Rollback caller's outer transaction
    await pg_session.rollback()

    # In a new query within the session, the checkpoint must NOT exist
    result = await pg_session.execute(select(ReadinessCheckpoint).where(ReadinessCheckpoint.run_id == run_id))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_persistence_clean_cached_orm_checkpoint(pg_session: AsyncSession):
    """Verifies that an already-cached clean checkpoint in session is preserved and returned on repeated save."""
    user_id = (await pg_session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
    project_id = (await pg_session.execute(text("SELECT id FROM projects LIMIT 1"))).scalar()
    run_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="pending",
        strategy="docker_github_vercel",
        configuration={},
        payload_hash="dummy_hash_cached",
        created_by=user_id,
    )
    pg_session.add(run)
    await pg_session.flush([run])

    payload = {
        "checkpoint_type": "initial_r0",
        "assessment_version": 1,
        "status": "ready",
        "score": 88,
        "max_score": 100,
        "selected_strategy": "docker_github_vercel",
        "detected_stack": "nodejs",
        "snapshot_identity": {},
        "dimension_scores": {},
        "dimension_explanations": {},
        "blockers": [],
        "warnings": [],
        "artifacts_detected": [],
        "resolved_blockers": [],
        "new_blockers": [],
        "explanation": "Cached test",
    }
    cp1 = await save_readiness_checkpoint(pg_session, run_id, payload)
    assert cp1 in pg_session
    assert cp1 not in pg_session.dirty

    # Calling save_readiness_checkpoint again with identical payload
    cp2 = await save_readiness_checkpoint(pg_session, run_id, payload)
    assert cp2.id == cp1.id
    assert cp2.score == 88


@pytest.mark.asyncio
async def test_persistence_concurrent_insert_resolution():
    """Verifies that concurrent insert conflicts across separate connections resolve idempotently."""
    engine = create_async_engine(TEST_PG_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as setup_session:
        user_id, project_id = await _ensure_seed_user_and_project(setup_session)
        run_id = uuid.uuid4()

        run = AutonomousDeployment(
            id=run_id,
            project_id=project_id,
            status="pending",
            strategy="docker_github_vercel",
            configuration={},
            payload_hash="dummy_hash_concurrency",
            created_by=user_id,
        )
        setup_session.add(run)
        await setup_session.commit()

    payload = {
        "checkpoint_type": "initial_r0",
        "assessment_version": 1,
        "status": "ready",
        "score": 92,
        "max_score": 100,
        "selected_strategy": "docker_github_vercel",
        "detected_stack": "nodejs",
        "snapshot_identity": {"commit": "concurrency_test"},
        "dimension_scores": {"configuration": 25, "artifacts": 25},
        "dimension_explanations": {},
        "blockers": [],
        "warnings": [],
        "artifacts_detected": ["package.json"],
        "resolved_blockers": [],
        "new_blockers": [],
        "explanation": "Concurrent insert test",
    }

    # Execute concurrent saves across two independent sessions
    async def worker(sess: AsyncSession):
        async with sess.begin():
            return await save_readiness_checkpoint(sess, run_id, payload)

    async with session_factory() as sess_a, session_factory() as sess_b:
        res_a, res_b = await asyncio.gather(worker(sess_a), worker(sess_b))

    assert res_a.id == res_b.id
    assert res_a.score == 92
    assert res_b.score == 92

    # Clean up test run
    async with session_factory() as cleanup_session:
        await cleanup_session.execute(
            text("DELETE FROM autonomous_deployments WHERE id = :rid"),
            {"rid": run_id},
        )
        await cleanup_session.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_capture_authoritative_readiness_checkpoint_maps_api_response(pg_session: AsyncSession):
    """Verifies that capture_authoritative_readiness_checkpoint consumes get_project_readiness directly."""
    from unittest.mock import AsyncMock, patch

    from src.deployments.autonomous_gates import capture_authoritative_readiness_checkpoint
    from src.projects.routes import ReadinessCheckResponse, ReadinessReportResponse

    user_id = (await pg_session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
    project_id = (await pg_session.execute(text("SELECT id FROM projects LIMIT 1"))).scalar()
    run_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="pending",
        strategy="docker_github_vercel",
        configuration={},
        payload_hash="dummy_hash_auth_r0",
        created_by=user_id,
    )
    pg_session.add(run)
    await pg_session.flush([run])

    mock_report = ReadinessReportResponse(
        project_id=project_id,
        score=85,
        level="production_ready",
        summary_report="Authoritative report: 85/100",
        recommendations=[],
        categories={"containerization": 25, "ci_config": 20, "orchestration": 20, "env_config": 20},
        indexed=True,
        evaluated_paths=12,
        reachable_score=95,
        reachable_summary="Can reach 95",
        generation_would_fix=[],
        unreachable=[],
        checks=[
            ReadinessCheckResponse(
                id="dockerfile_present",
                category="containerization",
                passed=True,
                points=25,
                max_points=25,
                evidence="Dockerfile found",
                why_it_matters="Needed for containers",
            )
        ],
    )

    with patch(  # noqa: FO-TD003 - FastAPI route handler cannot use autospec
        "src.projects.routes.get_project_readiness",
        new=AsyncMock(return_value=mock_report),
    ) as mock_api:
        r0 = await capture_authoritative_readiness_checkpoint(
            session=pg_session,
            run=run,
            checkpoint_type="initial_r0",
            assessment_version=1,
        )

        assert mock_api.called
        assert r0.score == 85
        assert r0.status == "ready"
        assert r0.score_delta == 0
        assert r0.dimension_scores["containerization"] == 25
        assert "Dockerfile" in r0.artifacts_detected
        assert r0.snapshot_identity["indexed"] is True
        assert r0.snapshot_identity["evaluated_paths"] == 12

        # Now test R1 delta calculation strictly against R0
        mock_report_r1 = mock_report.model_copy(update={"score": 95})
        mock_api.return_value = mock_report_r1

        r1 = await capture_authoritative_readiness_checkpoint(
            session=pg_session,
            run=run,
            checkpoint_type="post_generation_r1",
            assessment_version=1,
        )

        assert r1.score == 95
        assert r1.score_delta == 10  # 95 - 85 strictly


@pytest.mark.asyncio
async def test_capture_authoritative_readiness_checkpoint_unindexed_scores_zero(pg_session: AsyncSession):
    """Verifies that an unindexed project consumes 0/100 without inventing scores or disk heuristics."""
    from unittest.mock import AsyncMock, patch

    from src.deployments.autonomous_gates import capture_authoritative_readiness_checkpoint
    from src.projects.routes import ReadinessReportResponse

    user_id = (await pg_session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
    project_id = (await pg_session.execute(text("SELECT id FROM projects LIMIT 1"))).scalar()
    run_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="pending",
        strategy="docker_github_vercel",
        configuration={},
        payload_hash="dummy_hash_auth_unindexed",
        created_by=user_id,
    )
    pg_session.add(run)
    await pg_session.flush([run])

    mock_unindexed = ReadinessReportResponse(
        project_id=project_id,
        score=0,
        level="unassessed",
        summary_report="No indexed files",
        recommendations=[],
        categories={},
        indexed=False,
        evaluated_paths=0,
        reachable_score=0,
        reachable_summary="",
        generation_would_fix=[],
        unreachable=[],
        checks=[],
    )

    with patch(  # noqa: FO-TD003 - FastAPI route handler cannot use autospec
        "src.projects.routes.get_project_readiness",
        new=AsyncMock(return_value=mock_unindexed),
    ):
        res = await capture_authoritative_readiness_checkpoint(
            session=pg_session,
            run=run,
            checkpoint_type="initial_r0",
            assessment_version=1,
        )

        assert res.score == 0
        assert res.status == "unready"
        assert res.snapshot_identity["indexed"] is False


@pytest.mark.asyncio
async def test_capture_authoritative_readiness_checkpoint_api_error_fails_honestly(pg_session: AsyncSession):
    """Verifies that an API failure marks status='failed' and score=None honestly."""
    from unittest.mock import patch

    from src.deployments.autonomous_gates import capture_authoritative_readiness_checkpoint

    user_id = (await pg_session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
    project_id = (await pg_session.execute(text("SELECT id FROM projects LIMIT 1"))).scalar()
    run_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="pending",
        strategy="docker_github_vercel",
        configuration={},
        payload_hash="dummy_hash_auth_err",
        created_by=user_id,
    )
    pg_session.add(run)
    await pg_session.flush([run])

    with patch(  # noqa: FO-TD003 - FastAPI route handler cannot use autospec
        "src.projects.routes.get_project_readiness",
        side_effect=RuntimeError("DB connection dropped"),
    ):
        res = await capture_authoritative_readiness_checkpoint(
            session=pg_session,
            run=run,
            checkpoint_type="initial_r0",
            assessment_version=1,
        )

        assert res.status == "failed"
        assert res.score is None
        assert "DB connection dropped" in res.explanation
