# SPDX-License-Identifier: FSL-1.1-ALv2
"""The `deployments` row. §2.2.

WHY THIS FILE EXISTS AT ALL, since `service.py` writes the table with raw SQL and never instantiates the
class. Because `alembic check` compares `SQLModel.metadata` against the migrated schema and treats every
difference as a defect — and it is right to. Revision `0024` created three tables and two of them had no
model, so autogenerate proposed DROPPING all of them: a migration with no model looks exactly like a table
somebody forgot to remove. The integration suite caught it (`test_alembic_autogenerate_clean`), which is
the whole point of that gate.

So the declarations here are the SECOND statement of the schema, and the two are kept identical on
purpose: the migration is what runs, the model is what the drift check reads, and a disagreement between
them fails CI rather than surfacing as a mysterious column at runtime.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

#: The lifecycle, identical to revision `0024`'s CHECK and to `service.DEPLOYMENT_STATUSES`.
#:
#: `degraded` is the one worth explaining: the manifests are in the cluster and at least one workload did
#: not converge. Not a failure — nothing needs retrying — and not a success, because the thing the operator
#: asked for is not running.
DEPLOYMENT_STATUSES: tuple[str, ...] = (
    "pending_approval",
    "applying",
    "applied",
    "degraded",
    "failed",
    "rolled_back",
)


class Deployment(SQLModel, table=True):
    """One deployment of a manifest set to one environment."""

    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in DEPLOYMENT_STATUSES) + ")",
            name="ck_deployments_status",
        ),
        # A STABLE ROW MUST BE HEALTHY. The invariant that makes `stable` usable as a rollback target:
        # a deployment that was applied and never converged is not a state to return to.
        CheckConstraint(
            "NOT stable OR (healthy AND status = 'applied')",
            name="ck_deployments_stable_implies_healthy",
        ),
        Index("ix_deployments_project_created", "project_id", "created_at"),
        Index("ix_deployments_environment", "environment_id"),
        # The rollback-target lookup, read on every rollback. Partial, because `stable` is read by
        # nothing else.
        Index(
            "ix_deployments_stable_targets",
            "environment_id",
            "created_at",
            postgresql_where=text("stable"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    )
    #: RESTRICT, not CASCADE: deleting an environment must not erase the history of what was deployed to
    #: it. A rollback target that vanished when somebody tidied an environment list cannot be audited.
    environment_id: uuid.UUID = Field(
        sa_column=Column("environment_id", ForeignKey("environments.id", ondelete="RESTRICT"), nullable=False)
    )
    tenant_id: uuid.UUID | None = Field(default=None)
    change_set_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column("change_set_id", ForeignKey("change_sets.id", ondelete="SET NULL"), nullable=True),
    )
    status: str = Field(sa_column=Column("status", String(length=32), nullable=False))
    #: NULLABLE ON PURPOSE. `null` means nothing verified it — in flight, or an apply that failed before
    #: anything could be checked. `false` asserts the workloads were checked and were not ready.
    healthy: bool | None = Field(default=None, sa_column=Column("healthy", Boolean, nullable=True))
    stable: bool = Field(
        default=False,
        sa_column=Column("stable", Boolean, nullable=False, server_default=text("false")),
    )
    manifests: list[str] = Field(default_factory=list, sa_column=Column("manifests", JSONB, nullable=False))
    manifest_digest: str = Field(sa_column=Column("manifest_digest", String(length=71), nullable=False))
    cluster_context: str | None = Field(
        default=None, sa_column=Column("cluster_context", String(length=253), nullable=True)
    )
    namespace: str | None = Field(default=None, sa_column=Column("namespace", String(length=253), nullable=True))
    #: The agent's own report, stored whole. Its words rather than a parse of them: an operator debugging
    #: a rollout wants "1 of 3 updated replicas are available", and a summary loses it.
    report: dict[str, Any] | None = Field(default=None, sa_column=Column("report", JSONB, nullable=True))
    requested_by: uuid.UUID | None = Field(default=None)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column("completed_at", DateTime(timezone=True), nullable=True),
    )


class DeploymentCircuitBreaker(SQLModel, table=True):
    """One breaker per project and environment. 2.2, revision 0031.

    EVERY MIGRATION NEEDS ITS MODEL. Three tables once had migrations and no models, and `alembic check`
    proposed dropping all three -- so this exists to keep autogenerate's view of the schema equal to the
    migrations' view, not because anything loads breakers through the ORM. `breaker.py` uses raw SQL
    because its write is an `ON CONFLICT ... DO UPDATE` the ORM cannot express.
    """

    __tablename__ = "deployment_circuit_breakers"
    __table_args__ = (
        UniqueConstraint("project_id", "environment", name="uq_deployment_breakers_project_environment"),
        CheckConstraint(
            "state IN ('closed', 'open', 'half_open')",
            name="ck_deployment_breakers_state_allowed",
        ),
        CheckConstraint(
            "(state = 'open' AND opened_at IS NOT NULL) OR (state <> 'open')",
            name="ck_deployment_breakers_open_has_opened_at",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_deployment_breakers_failures_non_negative",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    # The environment NAME, not a foreign key: a breaker must survive an environment row being
    # recreated, because deleting and re-adding `production` should not clear a breaker protecting it.
    environment: str = Field(max_length=64)
    state: str = Field(max_length=16, default="closed")
    consecutive_failures: int = Field(default=0)
    # `Text` and not the default `VARCHAR`: the migration created it as TEXT, and `alembic check`
    # correctly proposed a type change until the model said the same thing. A reason can be a
    # cluster diagnostic several lines long.
    last_failure_reason: str | None = Field(default=None, sa_column=Column("last_failure_reason", Text, nullable=True))
    opened_at: datetime | None = Field(
        default=None, sa_column=Column("opened_at", DateTime(timezone=True), nullable=True)
    )
    updated_at: datetime = Field(
        sa_column=Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )


from src.deployments.autonomous_models import (  # noqa: E402
    AutonomousDeployment,
    AutonomousDeploymentLog,
    AutonomousDeploymentOutbox,
    AutonomousDeploymentStage,
)

__all__ = [
    "DEPLOYMENT_STATUSES",
    "Deployment",
    "DeploymentCircuitBreaker",
    "AutonomousDeployment",
    "AutonomousDeploymentStage",
    "AutonomousDeploymentLog",
    "AutonomousDeploymentOutbox",
]
