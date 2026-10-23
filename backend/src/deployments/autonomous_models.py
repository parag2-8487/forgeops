# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous Deployment Orchestrator schema models (Phase 1).

Four tables declared here matching Alembic migration `0038_autonomous_deployments.py`:
1. `AutonomousDeployment`: Authoritative deployment run and attempt state.
2. `AutonomousDeploymentStage`: Operational stages and G1-G7 verification gate results.
3. `AutonomousDeploymentLog`: Monotonic line-by-line log output.
4. `AutonomousDeploymentOutbox`: Transactional outbox events for SSE/WS streaming relay.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy import (
    inspect as sa_inspect,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.orm.base import NO_VALUE
from sqlmodel import Field, Relationship, SQLModel
from src.auth.models import User  # noqa: F401
from src.projects.models import Project  # noqa: F401, TID251

AUTONOMOUS_RUN_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "cancelling",
    "cancelled",
    "succeeded",
    "failed",
    "rolled_back",
)

AUTONOMOUS_STAGE_STATUSES: tuple[str, ...] = (
    "pending",
    "waiting",
    "running",
    "cancelling",
    "cancelled",
    "succeeded",
    "failed",
    "skipped",
    "rolled_back",
)

AUTONOMOUS_LOG_LEVELS: tuple[str, ...] = ("INFO", "WARN", "ERROR")
AUTONOMOUS_OUTBOX_STATUSES: tuple[str, ...] = ("pending", "published")
READINESS_CHECKPOINT_TYPES: tuple[str, ...] = ("initial_r0", "post_generation_r1", "recovery_recheck")
READINESS_STATUSES: tuple[str, ...] = ("completed", "failed")


class AutonomousDeploymentStage(SQLModel, table=True):
    """Operational stage or gate (G1-G7) within an autonomous deployment run."""

    __tablename__ = "autonomous_deployment_stages"
    __table_args__ = (
        UniqueConstraint("run_id", "stage_name", name="uq_run_stage"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in AUTONOMOUS_STAGE_STATUSES) + ")",
            name="chk_stage_status",
        ),
        Index("idx_auto_deploy_stages_run", "run_id", "position"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column("id", Uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    )
    run_id: uuid.UUID = Field(
        sa_column=Column(
            "run_id",
            Uuid(),
            ForeignKey("autonomous_deployments.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    stage_name: str = Field(
        sa_column=Column("stage_name", String(length=64), nullable=False),
    )
    gate_id: str | None = Field(
        default=None,
        sa_column=Column("gate_id", String(length=16), nullable=True),
    )
    position: int = Field(
        sa_column=Column("position", Integer, nullable=False),
    )
    status: str = Field(
        default="pending",
        sa_column=Column("status", String(length=32), nullable=False, server_default=text("'pending'")),
    )
    progress_pct: int = Field(
        default=0,
        sa_column=Column("progress_pct", Integer, nullable=False, server_default=text("0")),
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column("started_at", DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column("completed_at", DateTime(timezone=True), nullable=True),
    )
    error_message: str | None = Field(
        default=None,
        sa_column=Column("error_message", Text, nullable=True),
    )
    stage_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )

    run: Optional["AutonomousDeployment"] = Relationship(
        sa_relationship=relationship("AutonomousDeployment", back_populates="stages")
    )

    def __init__(self, **data: Any):
        if "metadata" in data and "stage_metadata" not in data:
            data["stage_metadata"] = data.pop("metadata")
        super().__init__(**data)


class AutonomousDeploymentLog(SQLModel, table=True):
    """Line-by-line log record attached to an autonomous deployment run."""

    __tablename__ = "autonomous_deployment_logs"
    __table_args__ = (
        UniqueConstraint("run_id", "log_seq", name="uq_run_log_seq"),
        CheckConstraint(
            "level IN (" + ", ".join(f"'{lvl}'" for lvl in AUTONOMOUS_LOG_LEVELS) + ")",
            name="chk_log_level",
        ),
        Index("idx_auto_deploy_logs_query", "run_id", "log_seq"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column("id", BigInteger, primary_key=True, autoincrement=True),
    )
    run_id: uuid.UUID = Field(
        sa_column=Column(
            "run_id",
            Uuid(),
            ForeignKey("autonomous_deployments.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    stage_name: str = Field(
        sa_column=Column("stage_name", String(length=64), nullable=False),
    )
    log_seq: int = Field(
        sa_column=Column("log_seq", Integer, nullable=False),
    )
    level: str = Field(
        default="INFO",
        sa_column=Column("level", String(length=16), nullable=False, server_default=text("'INFO'")),
    )
    message: str = Field(
        sa_column=Column("message", Text, nullable=False),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )

    run: Optional["AutonomousDeployment"] = Relationship(
        sa_relationship=relationship("AutonomousDeployment", back_populates="logs")
    )


class AutonomousDeploymentOutbox(SQLModel, table=True):
    """Transactional outbox event record for real-time streaming relay."""

    __tablename__ = "autonomous_deployment_outbox"
    __table_args__ = (
        UniqueConstraint("run_id", "event_seq", name="uq_run_outbox_seq"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in AUTONOMOUS_OUTBOX_STATUSES) + ")",
            name="chk_outbox_status",
        ),
        Index("idx_auto_deploy_outbox_pending", "status", "id"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column("id", BigInteger, primary_key=True, autoincrement=True),
    )
    run_id: uuid.UUID = Field(
        sa_column=Column(
            "run_id",
            Uuid(),
            ForeignKey("autonomous_deployments.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    event_seq: int = Field(
        sa_column=Column("event_seq", Integer, nullable=False),
    )
    event_type: str = Field(
        sa_column=Column("event_type", String(length=32), nullable=False),
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("payload", JSONB, nullable=False),
    )
    status: str = Field(
        default="pending",
        sa_column=Column("status", String(length=16), nullable=False, server_default=text("'pending'")),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )

    run: Optional["AutonomousDeployment"] = Relationship(
        sa_relationship=relationship("AutonomousDeployment", back_populates="outbox_events")
    )


class AutonomousDeployment(SQLModel, table=True):
    """Authoritative state machine row for an autonomous deployment run."""

    __tablename__ = "autonomous_deployments"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_proj_idempotency"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in AUTONOMOUS_RUN_STATUSES) + ")",
            name="chk_run_status",
        ),
        CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="chk_progress_range",
        ),
        Index("idx_auto_deploy_proj_status", "project_id", "status"),
        Index("idx_auto_deploy_lease", "status", "lease_expires_at"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column("id", Uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    )
    project_id: uuid.UUID = Field(
        sa_column=Column("project_id", Uuid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    )
    parent_run_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            "parent_run_id",
            Uuid(),
            ForeignKey("autonomous_deployments.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    attempt_number: int = Field(
        default=1,
        sa_column=Column("attempt_number", Integer, nullable=False, server_default=text("1")),
    )
    status: str = Field(
        default="pending",
        sa_column=Column("status", String(length=32), nullable=False, server_default=text("'pending'")),
    )
    strategy: str = Field(
        sa_column=Column("strategy", String(length=32), nullable=False),
    )
    configuration: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("configuration", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    progress_pct: int = Field(
        default=0,
        sa_column=Column("progress_pct", Integer, nullable=False, server_default=text("0")),
    )
    current_stage: str | None = Field(
        default=None,
        sa_column=Column("current_stage", String(length=64), nullable=True),
    )
    error_summary: str | None = Field(
        default=None,
        sa_column=Column("error_summary", Text, nullable=True),
    )
    primary_error: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column("primary_error", JSONB, nullable=True),
    )
    compensation_error: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column("compensation_error", JSONB, nullable=True),
    )

    # Worker Fencing and Lease Custody
    worker_id: str | None = Field(
        default=None,
        sa_column=Column("worker_id", String(length=128), nullable=True),
    )
    fence_token: int = Field(
        default=0,
        sa_column=Column("fence_token", BigInteger, nullable=False, server_default=text("0")),
    )
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    )

    # Dispatch and Lifecycle Tracking
    dispatch_status: str = Field(
        default="pending",
        sa_column=Column("dispatch_status", String(length=32), nullable=False, server_default=text("'pending'")),
    )
    dispatch_requested_at: datetime | None = Field(
        default=None,
        sa_column=Column("dispatch_requested_at", DateTime(timezone=True), nullable=True),
    )
    idempotency_key: str | None = Field(
        default=None,
        sa_column=Column("idempotency_key", String(length=128), nullable=True),
    )
    payload_hash: str = Field(
        sa_column=Column("payload_hash", String(length=64), nullable=False),
    )

    # Atomic Sequence Allocators
    log_sequence_counter: int = Field(
        default=0,
        sa_column=Column("log_sequence_counter", Integer, nullable=False, server_default=text("0")),
    )
    outbox_sequence_counter: int = Field(
        default=0,
        sa_column=Column("outbox_sequence_counter", Integer, nullable=False, server_default=text("0")),
    )

    created_by: uuid.UUID = Field(sa_column=Column("created_by", Uuid(), ForeignKey("users.id"), nullable=False))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column("started_at", DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column("completed_at", DateTime(timezone=True), nullable=True),
    )

    # Relationships
    stages: list[AutonomousDeploymentStage] = Relationship(
        sa_relationship=relationship(
            "AutonomousDeploymentStage",
            back_populates="run",
            cascade="all, delete-orphan",
            order_by="AutonomousDeploymentStage.position",
            lazy="selectin",
        )
    )
    logs: list[AutonomousDeploymentLog] = Relationship(
        sa_relationship=relationship(
            "AutonomousDeploymentLog",
            back_populates="run",
            cascade="all, delete-orphan",
            order_by="AutonomousDeploymentLog.log_seq",
        )
    )
    outbox_events: list[AutonomousDeploymentOutbox] = Relationship(
        sa_relationship=relationship(
            "AutonomousDeploymentOutbox",
            back_populates="run",
            cascade="all, delete-orphan",
            order_by="AutonomousDeploymentOutbox.event_seq",
        )
    )
    readiness_checkpoints: list["ReadinessCheckpoint"] = Relationship(
        sa_relationship=relationship(
            "ReadinessCheckpoint",
            back_populates="run",
            cascade="all, delete-orphan",
            order_by="ReadinessCheckpoint.created_at",
        )
    )

    def _get_loaded_checkpoints(self) -> list["ReadinessCheckpoint"]:
        try:
            insp = sa_inspect(self)
            if insp is not None:
                attr = insp.attrs.get("readiness_checkpoints")
                if attr is not None and attr.loaded_value is not NO_VALUE:
                    return attr.loaded_value or []
                return []
        except Exception:
            pass
        return getattr(self, "readiness_checkpoints", []) or []

    @property
    def initial_readiness(self) -> Optional["ReadinessCheckpoint"]:
        for cp in self._get_loaded_checkpoints():
            if cp.checkpoint_type in ("initial_r0", "initial_readiness"):
                return cp
        return None

    @property
    def post_generation_readiness(self) -> Optional["ReadinessCheckpoint"]:
        matching = [
            cp
            for cp in self._get_loaded_checkpoints()
            if cp.checkpoint_type in ("post_generation_r1", "post_generation_readiness")
        ]
        if not matching:
            return None
        return max(matching, key=lambda cp: cp.assessment_version)


class ReadinessCheckpoint(SQLModel, table=True):
    """Immutable project readiness evaluation checkpoint."""

    __tablename__ = "deployment_readiness_checkpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "checkpoint_type", "assessment_version", name="uq_run_checkpoint_version"),
        CheckConstraint(
            "checkpoint_type IN (" + ", ".join(f"'{c}'" for c in READINESS_CHECKPOINT_TYPES) + ")",
            name="chk_readiness_checkpoint_type",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in READINESS_STATUSES) + ")",
            name="chk_readiness_status",
        ),
        Index("idx_readiness_run_lookup", "run_id", "created_at"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column("id", Uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    )
    run_id: uuid.UUID = Field(
        sa_column=Column(
            "run_id",
            Uuid(),
            ForeignKey("autonomous_deployments.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    checkpoint_type: str = Field(
        sa_column=Column("checkpoint_type", String(length=32), nullable=False),
    )
    assessment_version: int = Field(
        default=1,
        sa_column=Column("assessment_version", Integer, nullable=False, server_default=text("1")),
    )
    status: str = Field(
        sa_column=Column("status", String(length=20), nullable=False),
    )
    score: int | None = Field(
        default=None,
        sa_column=Column("score", Integer, nullable=True),
    )
    max_score: int = Field(
        default=100,
        sa_column=Column("max_score", Integer, nullable=False, server_default=text("100")),
    )
    score_delta: int | None = Field(
        default=None,
        sa_column=Column("score_delta", Integer, nullable=True),
    )
    selected_strategy: str = Field(
        sa_column=Column("selected_strategy", String(length=32), nullable=False),
    )
    detected_stack: str = Field(
        sa_column=Column("detected_stack", String(length=64), nullable=False),
    )
    snapshot_identity: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("snapshot_identity", JSONB, nullable=False),
    )
    dimension_scores: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("dimension_scores", JSONB, nullable=False),
    )
    dimension_explanations: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("dimension_explanations", JSONB, nullable=False),
    )
    blockers: list[str] = Field(
        default_factory=list,
        sa_column=Column("blockers", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    warnings: list[str] = Field(
        default_factory=list,
        sa_column=Column("warnings", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    artifacts_detected: list[str] = Field(
        default_factory=list,
        sa_column=Column("artifacts_detected", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    resolved_blockers: list[str] = Field(
        default_factory=list,
        sa_column=Column("resolved_blockers", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    new_blockers: list[str] = Field(
        default_factory=list,
        sa_column=Column("new_blockers", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    explanation: str = Field(
        sa_column=Column("explanation", Text, nullable=False),
    )
    assessment_error: str | None = Field(
        default=None,
        sa_column=Column("assessment_error", Text, nullable=True),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )

    run: Optional["AutonomousDeployment"] = Relationship(
        sa_relationship=relationship("AutonomousDeployment", back_populates="readiness_checkpoints")
    )
