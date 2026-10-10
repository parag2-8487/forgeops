# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous Deployment Orchestrator service layer.

Implements AutonomousDeploymentService:
- Idempotent run creation with SHA-256 payload fingerprinting and RFC 9457 idempotency conflict detection.
- Strategy-aware stage graph generation (G1-G7 canonical gates + operational stages).
- Initial transactional outbox emission.
- Authoritative snapshot lookup with eager stage loading.
- Immutable retry run chaining with parent_run_id and incremented attempt_number.
- Monotonic cursor-paginated log retrieval.
- Atomic log appending with fencing token enforcement, 5,000-line cap with terminal truncation warning,
  and secret redaction.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.errors import problem
from ..core.logging import redact_secrets
from .autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentLog,
    AutonomousDeploymentOutbox,
    AutonomousDeploymentStage,
)
from .autonomous_schemas import CreateAutonomousRunRequest, DeploymentStrategy

MAX_LOG_LINES: int = 5000
LOG_TRUNCATION_WARNING: str = "[SYSTEM WARNING] Log line limit reached (5,000 lines). Further output truncated."

STAGE_G1_BLUEPRINT: str = "G1_blueprint"
STAGE_G2_ARTIFACT: str = "G2_artifact"
STAGE_G3_CONSISTENCY: str = "G3_consistency"
STAGE_G4_BUILD: str = "G4_build"
STAGE_G5_APPLY: str = "G5_apply"
STAGE_G6_WORKLOAD: str = "G6_workload"
STAGE_GITHUB_RELEASE: str = "github_release"
STAGE_VERCEL_DEPLOY: str = "vercel_deploy"
STAGE_G7_VERIFICATION: str = "G7_verification"


def _normalize_dict_for_canonical_json(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively strip None values and normalize enum values for deterministic hashing."""
    normalized: dict[str, Any] = {}
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, dict):
            normalized[k] = _normalize_dict_for_canonical_json(v)
        elif hasattr(v, "value"):
            normalized[k] = v.value
        else:
            normalized[k] = v
    return normalized


def canonical_payload_hash(request: CreateAutonomousRunRequest | dict[str, Any]) -> str:
    """Computes a deterministic SHA-256 hash of the canonical JSON request payload.

    Excludes idempotency_key so that changes to configuration under the same
    idempotency key are detected as conflicts.
    """
    if isinstance(request, CreateAutonomousRunRequest):
        raw_dict = request.model_dump(mode="json", exclude={"idempotency_key"}, exclude_none=True)
    elif isinstance(request, dict):
        raw_dict = {k: v for k, v in request.items() if k != "idempotency_key" and v is not None}
    else:
        raise TypeError(f"Unsupported request type: {type(request)}")

    norm_dict = _normalize_dict_for_canonical_json(raw_dict)
    canonical_json = json.dumps(norm_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_stage_graph(strategy: str | DeploymentStrategy) -> list[tuple[str, str | None]]:
    """Generates strategy-aware (stage_name, gate_id) pairs in canonical execution order.

    Order:
    1. G1_blueprint (gate_id='G1')
    2. G2_artifact (gate_id='G2')
    3. G3_consistency (gate_id='G3')
    4. [If Docker strategy: G4_build, G5_apply, G6_workload]
    5. [If GitHub strategy: github_release]
    6. [If Vercel strategy: vercel_deploy]
    7. G7_verification (gate_id='G7')
    """
    strat = strategy.value if isinstance(strategy, DeploymentStrategy) else str(strategy)

    stages: list[tuple[str, str | None]] = [
        (STAGE_G1_BLUEPRINT, "G1"),
        (STAGE_G2_ARTIFACT, "G2"),
        (STAGE_G3_CONSISTENCY, "G3"),
    ]

    # Docker strategy evaluates G4-G6
    if strat in (DeploymentStrategy.DOCKER_GITHUB_VERCEL.value, DeploymentStrategy.DOCKER_GITHUB.value):
        stages.extend([
            (STAGE_G4_BUILD, "G4"),
            (STAGE_G5_APPLY, "G5"),
            (STAGE_G6_WORKLOAD, "G6"),
        ])

    # GitHub release operational stage
    if strat in (
        DeploymentStrategy.DOCKER_GITHUB_VERCEL.value,
        DeploymentStrategy.DOCKER_GITHUB.value,
        DeploymentStrategy.GITHUB_ONLY.value,
    ):
        stages.append((STAGE_GITHUB_RELEASE, None))

    # Vercel deploy operational stage
    if strat in (
        DeploymentStrategy.DOCKER_GITHUB_VERCEL.value,
        DeploymentStrategy.VERCEL_ONLY.value,
    ):
        stages.append((STAGE_VERCEL_DEPLOY, None))

    # G7 final verification always
    stages.append((STAGE_G7_VERIFICATION, "G7"))

    return stages


class AutonomousDeploymentService:
    """Service layer managing autonomous deployment lifecycle, idempotency, and log streaming."""

    def __init__(self) -> None:
        pass

    @staticmethod
    def compute_payload_hash(request: CreateAutonomousRunRequest | dict[str, Any]) -> str:
        """Alias for canonical_payload_hash."""
        return canonical_payload_hash(request)

    @staticmethod
    def get_stage_graph(strategy: str | DeploymentStrategy) -> list[tuple[str, str | None]]:
        """Alias for build_stage_graph."""
        return build_stage_graph(strategy)

    async def create_run(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        requested_by: uuid.UUID,
        request: CreateAutonomousRunRequest | dict[str, Any],
    ) -> tuple[AutonomousDeployment, bool]:
        """Create or return an existing autonomous deployment run with idempotency checking.

        Returns:
            (run, is_created):
            - (existing_run, False) if matching (project_id, idempotency_key) has identical payload_hash.
            - (new_run, True) if newly created.

        Raises:
            ProblemException (status 409, type "idempotency-conflict") if key matches but payload differs.
        """
        if isinstance(request, dict):
            request = CreateAutonomousRunRequest(**request)

        payload_hash = canonical_payload_hash(request)

        if request.idempotency_key:
            stmt = (
                select(AutonomousDeployment)
                .options(selectinload(AutonomousDeployment.stages))
                .where(
                    AutonomousDeployment.project_id == project_id,
                    AutonomousDeployment.idempotency_key == request.idempotency_key,
                )
            )
            result = await session.execute(stmt)
            existing_run = result.scalars().first()

            if existing_run is not None:
                if existing_run.payload_hash == payload_hash:
                    return existing_run, False
                raise problem(
                    "idempotency-conflict",
                    detail=(
                        f"Idempotency key '{request.idempotency_key}' was previously used "
                        "with a different payload configuration."
                    ),
                )

        strat = request.strategy.value if isinstance(request.strategy, DeploymentStrategy) else str(request.strategy)
        config_dict = request.model_dump(mode="json", exclude={"idempotency_key"}, exclude_none=True)

        run_id = uuid.uuid4()
        run = AutonomousDeployment(
            id=run_id,
            project_id=project_id,
            parent_run_id=None,
            attempt_number=1,
            status="pending",
            strategy=strat,
            configuration=config_dict,
            progress_pct=0,
            payload_hash=payload_hash,
            idempotency_key=request.idempotency_key,
            created_by=requested_by,
            dispatch_status="pending",
            fence_token=0,
            log_sequence_counter=0,
            outbox_sequence_counter=1,
        )
        session.add(run)

        stage_defs = build_stage_graph(request.strategy)
        stages: list[AutonomousDeploymentStage] = []
        for pos, (stage_name, gate_id) in enumerate(stage_defs, start=1):
            stage = AutonomousDeploymentStage(
                id=uuid.uuid4(),
                run_id=run.id,
                stage_name=stage_name,
                gate_id=gate_id,
                position=pos,
                status="pending",
                progress_pct=0,
                stage_metadata={},
            )
            stages.append(stage)
            session.add(stage)

        run.stages = stages

        outbox = AutonomousDeploymentOutbox(
            run_id=run.id,
            event_seq=1,
            event_type="run_created",
            payload={
                "run_id": str(run.id),
                "project_id": str(run.project_id),
                "status": run.status,
                "strategy": run.strategy,
                "attempt_number": run.attempt_number,
            },
            status="pending",
        )
        run.outbox_events = [outbox]
        session.add(outbox)

        await session.flush()
        return run, True

    async def get_run(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> AutonomousDeployment | None:
        """Fetch authoritative run with eager-loaded stages, validating project ownership."""
        stmt = (
            select(AutonomousDeployment)
            .options(selectinload(AutonomousDeployment.stages))
            .where(
                AutonomousDeployment.id == run_id,
                AutonomousDeployment.project_id == project_id,
            )
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    async def retry_run(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
        requested_by: uuid.UUID,
    ) -> AutonomousDeployment:
        """Create a new attempt for a failed or rolled-back run, leaving prior runs immutable.

        Raises:
            ProblemException (status 404, type "deployment-absent") if source run not found.
            ProblemException (status 409, type "autonomous-run-conflict") if source run is active or succeeded.
        """
        stmt = (
            select(AutonomousDeployment)
            .options(selectinload(AutonomousDeployment.stages))
            .where(
                AutonomousDeployment.id == run_id,
                AutonomousDeployment.project_id == project_id,
            )
        )
        result = await session.execute(stmt)
        source_run = result.scalars().first()
        if source_run is None:
            raise problem(
                "deployment-absent",
                detail=f"Autonomous deployment '{run_id}' not found in project '{project_id}'.",
            )

        if source_run.status not in ("failed", "rolled_back"):
            raise problem(
                "autonomous-run-conflict",
                detail=(
                    f"Cannot retry run '{run_id}' in status '{source_run.status}'. "
                    "Only runs in 'failed' or 'rolled_back' status may be retried."
                ),
            )

        new_run_id = uuid.uuid4()
        retry_run = AutonomousDeployment(
            id=new_run_id,
            project_id=project_id,
            parent_run_id=source_run.id,
            attempt_number=source_run.attempt_number + 1,
            status="pending",
            strategy=source_run.strategy,
            configuration=copy.deepcopy(source_run.configuration),
            progress_pct=0,
            payload_hash=source_run.payload_hash,
            idempotency_key=None,
            created_by=requested_by,
            dispatch_status="pending",
            fence_token=0,
            log_sequence_counter=0,
            outbox_sequence_counter=1,
        )
        session.add(retry_run)

        stage_defs = build_stage_graph(source_run.strategy)
        stages: list[AutonomousDeploymentStage] = []
        for pos, (stage_name, gate_id) in enumerate(stage_defs, start=1):
            stage = AutonomousDeploymentStage(
                id=uuid.uuid4(),
                run_id=retry_run.id,
                stage_name=stage_name,
                gate_id=gate_id,
                position=pos,
                status="pending",
                progress_pct=0,
                stage_metadata={},
            )
            stages.append(stage)
            session.add(stage)

        retry_run.stages = stages

        outbox = AutonomousDeploymentOutbox(
            run_id=retry_run.id,
            event_seq=1,
            event_type="run_created",
            payload={
                "run_id": str(retry_run.id),
                "parent_run_id": str(source_run.id),
                "project_id": str(retry_run.project_id),
                "status": retry_run.status,
                "strategy": retry_run.strategy,
                "attempt_number": retry_run.attempt_number,
            },
            status="pending",
        )
        retry_run.outbox_events = [outbox]
        session.add(outbox)

        await session.flush()
        return retry_run

    async def get_logs(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        since_log_seq: int = 0,
        limit: int = 500,
        stage_name: str | None = None,
    ) -> tuple[list[AutonomousDeploymentLog], bool, int]:
        """Cursor-paginated log retrieval strictly ordered by monotonic log_seq ASC.

        Returns:
            (logs, has_more, next_log_seq)
        """
        clamped_limit = max(1, min(limit, 1000))
        stmt = select(AutonomousDeploymentLog).where(
            AutonomousDeploymentLog.run_id == run_id,
            AutonomousDeploymentLog.log_seq > since_log_seq,
        )
        if stage_name:
            stmt = stmt.where(AutonomousDeploymentLog.stage_name == stage_name)
        stmt = stmt.order_by(AutonomousDeploymentLog.log_seq.asc()).limit(clamped_limit + 1)

        result = await session.execute(stmt)
        rows = list(result.scalars().all())

        has_more = len(rows) > clamped_limit
        logs = rows[:clamped_limit]
        next_log_seq = logs[-1].log_seq if logs else since_log_seq
        return logs, has_more, next_log_seq

    async def append_logs(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        stage_name: str,
        entries: list[dict[str, Any] | tuple[str, str] | str],
        fence_token: int,
    ) -> list[AutonomousDeploymentLog]:
        """Atomically appends execution logs for a stage under worker fence custody.

        Enforces:
        - Strict fence_token equality check.
        - Monotonic allocation of log_seq using run.log_sequence_counter.
        - 5,000-line cap: writes terminal truncation warning at 5,000, discards entries past 5,000.
        - Secret redaction before log entity persistence.

        Raises:
            ProblemException (status 404, type "deployment-absent") if run does not exist.
            ProblemException (status 409, type "autonomous-run-conflict") on fence token mismatch.
        """
        stmt = select(AutonomousDeployment).where(AutonomousDeployment.id == run_id)
        result = await session.execute(stmt)
        run = result.scalars().first()
        if run is None:
            raise problem("deployment-absent", detail=f"Autonomous deployment '{run_id}' not found.")

        if run.fence_token != fence_token:
            raise problem(
                "autonomous-run-conflict",
                detail=f"Worker fence token mismatch: expected {run.fence_token}, got {fence_token}.",
            )

        current_counter = run.log_sequence_counter
        remaining = MAX_LOG_LINES - current_counter
        if remaining <= 0 or not entries:
            return []

        normalized_entries: list[tuple[str, str]] = []
        for entry in entries:
            if isinstance(entry, str):
                normalized_entries.append((entry, "INFO"))
            elif isinstance(entry, dict):
                normalized_entries.append((entry.get("message", ""), entry.get("level", "INFO")))
            elif isinstance(entry, (list, tuple)):
                msg = entry[0] if len(entry) > 0 else ""
                lvl = entry[1] if len(entry) > 1 else "INFO"
                normalized_entries.append((msg, lvl))
            else:
                normalized_entries.append((str(entry), "INFO"))

        to_create: list[tuple[str, str]] = []
        if len(normalized_entries) >= remaining:
            to_create.extend(normalized_entries[: remaining - 1])
            to_create.append((LOG_TRUNCATION_WARNING, "WARN"))
        else:
            to_create.extend(normalized_entries)

        persisted: list[AutonomousDeploymentLog] = []
        for idx, (raw_msg, level) in enumerate(to_create, start=1):
            seq = current_counter + idx
            redacted_msg = redact_secrets(raw_msg)
            log_record = AutonomousDeploymentLog(
                run_id=run_id,
                stage_name=stage_name,
                log_seq=seq,
                level=level if level in ("INFO", "WARN", "ERROR") else "INFO",
                message=redacted_msg,
            )
            session.add(log_record)
            persisted.append(log_record)

        run.log_sequence_counter = current_counter + len(persisted)
        session.add(run)
        await session.flush()
        return persisted
