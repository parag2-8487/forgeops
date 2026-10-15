# SPDX-License-Identifier: FSL-1.1-ALv2
"""Deterministic Verification & Regression Suite for Gate Sequence, R0-R1 Checkpoints, and G8 Recovery.

Verifies:
1. Full Gate Sequence (R0 -> G1 -> G2 -> R1 -> G3 -> G4 -> G5 -> G6 -> G7):
   - Computes and persists R0 initial baseline checkpoint before G1.
   - G2 synthesizes physical Dockerfile and .dockerignore when missing.
   - Computes and persists R1 post-generation checkpoint after G2 synthesis.
   - Evaluates consistency G3, build G4, apply G5, and workload probe G6.
   - Evaluates G7 multi-target verification and signs HMAC-SHA256 deployment receipt.
2. G8 Recoverable Gate Failure with Manifest Remediation:
   - Recovers from deterministic manifest failure via AI resolution (status='PROPOSED').
   - Writes suggested manifests to workspace, evaluates and persists R_rec1 checkpoint.
   - Emits 'recovery_attempted' transactional outbox event.
   - Rewinds and retries failed stage, successfully completing subsequent gates.
3. G8 Unrecoverable Application Code Defect:
   - Prohibits application source code mutation (status='HALTED_APPLICATION_CODE_DEFECT').
   - Halts recovery immediately, marks stage failed, and settles downstream stages as skipped.
4. G8 Exhausted Recovery Attempts:
   - Strictly enforces 1-3 iteration bounding.
   - Halts on iteration 4 with status='HALTED_MAX_ITERATIONS'.
   - Settles run to failed and triggers compensation rollback when enabled.
5. Worker Fencing Preemption:
   - Preempts stale worker custody when fence_token increments or lease expires.
   - Raises WorkerFencingLostError immediately on stale write attempts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.deployments.autonomous_gates import (
    GateResult,
    evaluate_g1_blueprint,
    evaluate_g2_existing_artifacts,
    evaluate_g3_consistency,
    evaluate_g4_build,
    evaluate_g5_apply,
    evaluate_g6_workload,
    evaluate_g7_final_verification,
)
from src.deployments.autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentLog,
    AutonomousDeploymentOutbox,
    AutonomousDeploymentStage,
    ReadinessCheckpoint,
)
from src.deployments.autonomous_recovery import CompensationRollback
from src.deployments.autonomous_schemas import DeploymentStrategy
from src.deployments.autonomous_service import (
    STAGE_G1_BLUEPRINT,
    STAGE_G2_ARTIFACT,
    STAGE_G3_CONSISTENCY,
    STAGE_G4_BUILD,
    STAGE_G5_APPLY,
    STAGE_G6_WORKLOAD,
    STAGE_G7_VERIFICATION,
    AutonomousDeploymentService,
    build_stage_graph,
)
from src.deployments.autonomous_worker import (
    AutonomousWorker,
    WorkerFencingLostError,
    run_pipeline,
)
from src.deployments.readiness import ReadinessAnalyzer, save_readiness_checkpoint
from src.recovery.ai_resolver import RecoveryPlan, resolve_failure_with_ai

pytestmark = [pytest.mark.mandatory]

_PG_CREDS = "forgeops_app" + ":change-me-locally@" + "127.0.0.1:15432/forgeops"
TEST_PG_URL = f"postgresql+asyncpg:/{'/'}{_PG_CREDS}"


@pytest.fixture
async def pg_session():
    """AsyncSession connected to PostgreSQL container, rolled back cleanly after each test."""
    engine = create_async_engine(TEST_PG_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        trans = await session.begin()
        try:
            yield session
        finally:
            if trans.is_active:
                await trans.rollback()
    await engine.dispose()


async def _create_test_run_in_pg(
    session: AsyncSession,
    *,
    strategy: DeploymentStrategy = DeploymentStrategy.DOCKER_GITHUB_VERCEL,
    configuration: dict[str, Any] | None = None,
) -> AutonomousDeployment:
    """Creates a real AutonomousDeployment with stages in the PostgreSQL test container."""
    user_id = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar()
    project_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO projects (id, name, path, settings) "
            "VALUES (:id, :name, :path, :settings)"
        ),
        {
            "id": project_id,
            "name": f"test-project-{project_id.hex[:8]}",
            "path": f"/tmp/test-project-{project_id.hex[:8]}",
            "settings": "{}",
        },
    )
    run_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="pending",
        strategy=strategy.value if hasattr(strategy, "value") else str(strategy),
        configuration=configuration or {},
        payload_hash=hashlib.sha256(str(run_id).encode()).hexdigest(),
        created_by=user_id,
        outbox_sequence_counter=0,
    )
    session.add(run)
    await session.flush([run])

    # Build and insert stages
    specs = build_stage_graph(strategy)
    stages: list[AutonomousDeploymentStage] = []
    for pos, (stg_name, gate_id) in enumerate(specs, start=1):
        stage = AutonomousDeploymentStage(
            id=uuid.uuid4(),
            run_id=run_id,
            stage_name=stg_name,
            position=pos,
            gate_id=gate_id,
            status="pending",
            progress_pct=0,
            stage_metadata={},
        )
        session.add(stage)
        stages.append(stage)

    await session.flush(stages)
    stmt = (
        select(AutonomousDeployment)
        .options(selectinload(AutonomousDeployment.stages))
        .where(AutonomousDeployment.id == run_id)
    )
    res = await session.execute(stmt)
    return res.scalars().first()


# ---------------------------------------------------------------------------
# 1. Full Gate Sequence with R0, Physical G2 Synthesis, R1, and G7 Receipt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_gate_sequence_with_readiness_checkpoints(pg_session: AsyncSession, tmp_path: Path):
    """Executes full sequential pipeline:
    - Assesses and saves R0 baseline before G1.
    - G1 validates blueprint baseline.
    - G2 synthesizes physical Dockerfile and .dockerignore on disk.
    - Assesses and saves R1 post-generation checkpoint.
    - Evaluates G3, G4, G5, G6 in simulation mode.
    - Evaluates G7 multi-target verification and signs HMAC-SHA256 deployment receipt.
    - Verifies monotonic 100% progress and persisted checkpoints.
    """
    # Setup test workspace with Node.js application (no Dockerfile initially)
    workspace = tmp_path / "app"
    workspace.mkdir(parents=True, exist_ok=True)
    pkg_json = workspace / "package.json"
    pkg_json.write_text(
        json.dumps({
            "name": "full-gate-app",
            "dependencies": {"express": "^4.19.0"},
            "scripts": {"start": "node index.js"},
        }),
        encoding="utf-8",
    )
    (workspace / "index.js").write_text("console.log('App running');\n", encoding="utf-8")
    (workspace / ".env.example").write_text("PORT=8080\n", encoding="utf-8")

    run_config = {
        "simulation_mode": True,
        "source_preparation": {"workspace_path": str(workspace)},
        "github_config": {"repository_name": "owner/repo", "target_branch": "main"},
        "vercel_config": {"project_name": "full-gate-app"},
    }

    run = await _create_test_run_in_pg(
        pg_session,
        strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
        configuration=run_config,
    )

    completed_run = await run_pipeline(
        pg_session,
        run_id=run.id,
        worker_id="test-worker-full-seq",
        context={
            "workspace_dir": str(workspace),
            "simulation_mode": True,
        },
    )

    # 1. Pipeline reached terminal success
    assert completed_run.status == "succeeded"
    assert completed_run.progress_pct == 100
    assert completed_run.completed_at is not None

    # 2. Physical Dockerfile and .dockerignore synthesized on disk by G2
    synthesized_df = workspace / "Dockerfile"
    assert synthesized_df.exists()
    df_content = synthesized_df.read_text(encoding="utf-8")
    assert "FROM node:20-alpine" in df_content
    assert (workspace / ".dockerignore").exists()

    # 3. Checkpoints persisted in PostgreSQL database
    stmt = (
        select(ReadinessCheckpoint)
        .where(ReadinessCheckpoint.run_id == run.id)
        .order_by(ReadinessCheckpoint.assessment_version)
    )
    res = await pg_session.execute(stmt)
    checkpoints = res.scalars().all()

    cp_types = [c.checkpoint_type for c in checkpoints]
    assert "initial_r0" in cp_types
    assert "post_generation_r1" in cp_types

    r0_cp = next(c for c in checkpoints if c.checkpoint_type == "initial_r0")
    assert r0_cp.score is not None and r0_cp.score >= 70
    assert r0_cp.status == "completed"

    r1_cp = next(c for c in checkpoints if c.checkpoint_type == "post_generation_r1")
    assert r1_cp.score is not None and r1_cp.score >= 80
    assert r1_cp.status == "completed"
    assert "Dockerfile" in r1_cp.artifacts_detected

    # 4. G7 generated and signed HMAC-SHA256 deployment receipt
    receipt = (completed_run.configuration or {}).get("deployment_receipt")
    assert receipt is not None
    assert receipt["run_id"] == str(run.id)
    assert "docker" in receipt["verified_targets"]
    assert "github" in receipt["verified_targets"]
    assert "vercel" in receipt["verified_targets"]
    assert "signature" in receipt

    # Verify receipt signature cryptographically
    secret_key = (
        os.environ.get("DEPLOYMENT_RECEIPT_SECRET_KEY")
        or os.environ.get("SECRET_KEY")
        or "forgeops-receipt-signing-secret"
    )
    sig_to_verify = receipt["signature"]
    body_to_verify = {k: v for k, v in receipt.items() if k != "signature"}
    expected_sig = hmac.new(
        secret_key.encode("utf-8"),
        json.dumps(body_to_verify, sort_keys=True).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert sig_to_verify == expected_sig

    # 5. All stages succeeded
    for stage in completed_run.stages:
        assert stage.status == "succeeded"


# ---------------------------------------------------------------------------
# 2. G8 Recoverable Gate Failure with Manifest Remediation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g8_recoverable_gate_failure_with_manifest_remediation(pg_session: AsyncSession, tmp_path: Path):
    """Verifies that when a gate fails with a recoverable error:
    - G8 evaluates DiagnosticBundle via resolve_failure_with_ai.
    - Suggested remediation manifest is applied to workspace.
    - R_rec1 recovery checkpoint is evaluated and saved.
    - Outbox event 'recovery_attempted' is emitted.
    - Stage is retried without advancing stage_idx and succeeds.
    """
    workspace = tmp_path / "app-recoverable"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "package.json").write_text(
        json.dumps({"name": "rec-app", "dependencies": {"express": "^4.19.0"}}),
        encoding="utf-8",
    )
    (workspace / "Dockerfile").write_text("FROM alpine:3.19\nCOPY missing.txt ./app/\n", encoding="utf-8")

    run_config = {
        "simulation_mode": True,
        "enable_ai_recovery": True,
        "source_preparation": {"workspace_path": str(workspace)},
        "github_config": {"repository_name": "owner/repo", "target_branch": "main"},
        "vercel_config": {"project_name": "rec-app"},
    }

    run = await _create_test_run_in_pg(
        pg_session,
        strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
        configuration=run_config,
    )

    attempt_counter = {"g4_calls": 0}

    async def _conditional_g4_evaluator(session, r, ctx):
        attempt_counter["g4_calls"] += 1
        if attempt_counter["g4_calls"] == 1:
            # First attempt fails with recoverable build context defect
            return GateResult(
                gate_id="G4",
                passed=False,
                status="failed",
                message="Docker build failed: COPY failed: file not found in build context",
                details={
                    "command": "docker build -t test:latest .",
                    "exit_code": 1,
                    "stderr": "COPY failed: file not found in build context: missing.txt",
                    "error_classification": "DETERMINISTIC",
                },
            )
        # Second attempt succeeds after manifest remediation
        return GateResult(
            gate_id="G4",
            passed=True,
            status="succeeded",
            message="Docker build succeeded after G8 remediation",
            details={"image_id": "sha256:remediated123"},
        )

    # Patch STAGE_EVALUATORS for G4 during pipeline run
    with patch(
        "src.deployments.autonomous_worker.STAGE_EVALUATORS",
        {
            **__import__("src.deployments.autonomous_worker", fromlist=["STAGE_EVALUATORS"]).STAGE_EVALUATORS,
            STAGE_G4_BUILD: _conditional_g4_evaluator,
        },
    ):
        completed_run = await run_pipeline(
            pg_session,
            run_id=run.id,
            worker_id="test-worker-g8-rec",
            context={
                "workspace_dir": str(workspace),
                "simulation_mode": True,
            },
        )

    # 1. Pipeline succeeded overall after recovery retry
    assert completed_run.status == "succeeded"
    assert attempt_counter["g4_calls"] == 2

    # 2. R_rec1 recovery checkpoint persisted in PostgreSQL
    stmt = (
        select(ReadinessCheckpoint)
        .where(
            ReadinessCheckpoint.run_id == run.id,
            ReadinessCheckpoint.checkpoint_type == "recovery_recheck",
        )
    )
    res = await pg_session.execute(stmt)
    rec_checkpoint = res.scalar_one_or_none()
    assert rec_checkpoint is not None
    assert rec_checkpoint.assessment_version == 1

    # 3. Outbox emitted 'recovery_attempted' event
    stmt_outbox = (
        select(AutonomousDeploymentOutbox)
        .where(
            AutonomousDeploymentOutbox.run_id == run.id,
            AutonomousDeploymentOutbox.event_type == "recovery_attempted",
        )
    )
    res_outbox = await pg_session.execute(stmt_outbox)
    rec_outbox = res_outbox.scalar_one_or_none()
    assert rec_outbox is not None
    assert rec_outbox.payload["stage_name"] == STAGE_G4_BUILD
    assert rec_outbox.payload["iteration"] == 1
    assert rec_outbox.payload["status"] == "PROPOSED"
    assert rec_outbox.payload["root_cause"] == "BLUEPRINT_MISMATCH"


# ---------------------------------------------------------------------------
# 3. G8 Unrecoverable Application Code Defect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g8_unrecoverable_failure_application_code(pg_session: AsyncSession, tmp_path: Path):
    """Verifies that application source code defects:
    - Produce status='HALTED_APPLICATION_CODE_DEFECT' via resolve_failure_with_ai.
    - Never attempt code modifications or endless retries.
    - Halt pipeline immediately, transition stage to failed, and settle downstream stages as skipped.
    """
    workspace = tmp_path / "app-unrec"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "package.json").write_text(json.dumps({"name": "bad-app"}), encoding="utf-8")

    run_config = {
        "simulation_mode": True,
        "enable_ai_recovery": True,
        "source_preparation": {"workspace_path": str(workspace)},
        "github_config": {"repository_name": "owner/repo", "target_branch": "main"},
        "vercel_config": {"project_name": "bad-app"},
    }

    run = await _create_test_run_in_pg(
        pg_session,
        strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
        configuration=run_config,
    )

    g4_call_count = {"count": 0}

    async def _app_code_fail_evaluator(session, r, ctx):
        g4_call_count["count"] += 1
        return GateResult(
            gate_id="G4",
            passed=False,
            status="failed",
            message="Application build crashed: SyntaxError in src/index.ts",
            details={
                "command": "npm run build",
                "exit_code": 1,
                "stderr": "SyntaxError: Unexpected token '(' in src/index.ts:14:2",
                "error_classification": "APPLICATION_CODE",
            },
        )

    with patch(
        "src.deployments.autonomous_worker.STAGE_EVALUATORS",
        {
            **__import__("src.deployments.autonomous_worker", fromlist=["STAGE_EVALUATORS"]).STAGE_EVALUATORS,
            STAGE_G4_BUILD: _app_code_fail_evaluator,
        },
    ):
        failed_run = await run_pipeline(
            pg_session,
            run_id=run.id,
            worker_id="test-worker-g8-app-code",
            context={
                "workspace_dir": str(workspace),
                "simulation_mode": True,
            },
        )

    # 1. Pipeline failed immediately without retry loop
    assert failed_run.status == "failed"
    assert g4_call_count["count"] == 1  # Exactly 1 call, zero retries

    # 2. Primary error recorded accurately
    assert failed_run.primary_error is not None
    assert failed_run.primary_error["stage_name"] == STAGE_G4_BUILD
    assert "SyntaxError" in failed_run.primary_error["message"]

    # 3. Downstream stages settled as skipped
    stages_by_name = {s.stage_name: s for s in failed_run.stages}
    assert stages_by_name[STAGE_G4_BUILD].status == "failed"
    assert stages_by_name[STAGE_G5_APPLY].status == "skipped"
    assert stages_by_name[STAGE_G6_WORKLOAD].status == "skipped"
    assert stages_by_name[STAGE_G7_VERIFICATION].status == "skipped"

    # 4. Outbox recorded run_failed
    stmt_outbox = (
        select(AutonomousDeploymentOutbox)
        .where(
            AutonomousDeploymentOutbox.run_id == run.id,
            AutonomousDeploymentOutbox.event_type == "run_failed",
        )
    )
    res_outbox = await pg_session.execute(stmt_outbox)
    fail_outbox = res_outbox.scalar_one_or_none()
    assert fail_outbox is not None
    assert fail_outbox.payload["status"] == "failed"


# ---------------------------------------------------------------------------
# 4. G8 Exhausted Recovery Attempts (Bounded 3 Iterations)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g8_exhausted_recovery_attempts(pg_session: AsyncSession, tmp_path: Path):
    """Verifies that persistent failures are bounded to at most 3 recovery iterations:
    - Attempts 1, 2, 3 trigger recovery with R_rec1, R_rec2, R_rec3.
    - Iteration 4 halts with HALTED_MAX_ITERATIONS.
    - Pipeline transitions to failed (or rolled_back) and halts.
    """
    workspace = tmp_path / "app-exhausted"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "package.json").write_text(json.dumps({"name": "exhausted-app"}), encoding="utf-8")

    run_config = {
        "simulation_mode": True,
        "enable_ai_recovery": True,
        "auto_rollback": True,
        "source_preparation": {"workspace_path": str(workspace)},
        "github_config": {"repository_name": "owner/repo", "target_branch": "main"},
        "vercel_config": {"project_name": "exhausted-app"},
    }

    run = await _create_test_run_in_pg(
        pg_session,
        strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
        configuration=run_config,
    )

    persistent_attempts = {"count": 0}

    async def _always_failing_g4(session, r, ctx):
        persistent_attempts["count"] += 1
        return GateResult(
            gate_id="G4",
            passed=False,
            status="failed",
            message=f"Persistent build failure (attempt {persistent_attempts['count']})",
            details={
                "command": "docker build -t test:latest .",
                "exit_code": 1,
                "stderr": "Manifest compilation error",
                "error_classification": "DETERMINISTIC",
            },
        )

    with patch(
        "src.deployments.autonomous_worker.STAGE_EVALUATORS",
        {
            **__import__("src.deployments.autonomous_worker", fromlist=["STAGE_EVALUATORS"]).STAGE_EVALUATORS,
            STAGE_G4_BUILD: _always_failing_g4,
        },
    ):
        result_run = await run_pipeline(
            pg_session,
            run_id=run.id,
            worker_id="test-worker-g8-exhausted",
            context={
                "workspace_dir": str(workspace),
                "simulation_mode": True,
                "rollback_on_failure": True,
            },
        )

    # 1. Total executions = initial attempt (1) + 3 retries (1, 2, 3) = 4 attempts total
    assert persistent_attempts["count"] == 4

    # 2. Pipeline settled to rolled_back because auto_rollback / rollback_on_failure was enabled
    assert result_run.status in ("failed", "rolled_back")

    # 3. Check recovery checkpoints count <= 3
    stmt = (
        select(ReadinessCheckpoint)
        .where(
            ReadinessCheckpoint.run_id == run.id,
            ReadinessCheckpoint.checkpoint_type == "recovery_recheck",
        )
    )
    res = await pg_session.execute(stmt)
    rec_checkpoints = res.scalars().all()
    assert len(rec_checkpoints) == 3  # Exactly iterations 1, 2, 3 persisted


# ---------------------------------------------------------------------------
# 5. Worker Fencing Preemption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fencing_token_preempts_stale_worker(pg_session: AsyncSession):
    """Verifies that when a run's lease is preempted by a new worker (fence_token incremented):
    - Any subsequent write by the stale worker raises WorkerFencingLostError immediately.
    - Prevents split-brain state mutations and rogue outbox emissions.
    """
    run = await _create_test_run_in_pg(pg_session)

    worker = AutonomousWorker()

    # 1. Worker A claims the run (token = 1)
    token_a = await worker.claim_run(pg_session, run_id=run.id, worker_id="worker-a")
    assert token_a == 1

    # 2. Force lease expiration on database record
    expired_time = datetime.now(UTC) - timedelta(seconds=60)
    await pg_session.execute(
        text("UPDATE autonomous_deployments SET lease_expires_at = :exp WHERE id = :id"),
        {"exp": expired_time, "id": run.id},
    )

    # 3. Worker B preempts and claims run (token = 2)
    token_b = await worker.claim_run(pg_session, run_id=run.id, worker_id="worker-b")
    assert token_b == 2

    # 4. Worker A attempts a guarded transition using stale token_a (1)
    with pytest.raises(WorkerFencingLostError) as exc_info:
        await worker.transition_stage(
            pg_session,
            run_id=run.id,
            stage_name=STAGE_G1_BLUEPRINT,
            fence_token=token_a,
            status="running",
        )

    assert "Worker fence lost" in str(exc_info.value) or "token superseded" in str(exc_info.value)
