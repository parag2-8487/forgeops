# SPDX-License-Identifier: FSL-1.1-ALv2
"""Policies, evaluations and published bundles (design.md §6.2, §6.5 `0005`, §11.7).

`policy_evaluations.side` is the column that makes double evaluation auditable. The
backend and the agent evaluate the same Rego against the same input, and §1.10's
whole claim is that they agree. Recording which side produced each verdict turns a
disagreement from an invisible bug into a row you can query for.

`policy_bundles` carries a **partial** unique index — `WHERE active` — so exactly
one bundle can be active per scope while any number of superseded bundles are kept.
A plain unique constraint on `(project_id, active)` could not express that, and
deleting superseded bundles would destroy the provenance of every device that
pinned one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

# Import related models for SQLAlchemy ForeignKey resolution
from ..projects.models import Project  # noqa: F401
from ..governance.models import ChangeSet  # noqa: F401

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlmodel import Field, SQLModel

POLICY_ENGINES: tuple[str, ...] = ("rego",)
EVALUATION_RESULTS: tuple[str, ...] = ("allow", "deny", "require_approval")
EVALUATION_SIDES: tuple[str, ...] = ("backend", "agent")


def in_list(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({rendered})"


class Policy(SQLModel, table=True):
    """PRD D6 `policies`. `project_id` NULL means the policy is global."""

    __tablename__ = "policies"
    __table_args__ = (
        CheckConstraint(in_list("engine", POLICY_ENGINES), name="ck_policies_engine_allowed"),
        UniqueConstraint("project_id", "name", name="uq_policies_project_id_name"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    name: str = Field(max_length=200)
    engine: str = Field(default="rego", max_length=16)
    rego_rules: str = Field(sa_column=Column("rego_rules", Text, nullable=False))
    enabled: bool = Field(default=True)
    template_id: str | None = Field(default=None, max_length=64)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        )
    )


class PolicyEvaluation(SQLModel, table=True):
    """PRD D6 `policy_evaluations`. Both sides of the double evaluation land here."""

    __tablename__ = "policy_evaluations"
    __table_args__ = (
        CheckConstraint(in_list("result", EVALUATION_RESULTS), name="ck_policy_evaluations_result_allowed"),
        CheckConstraint(in_list("side", EVALUATION_SIDES), name="ck_policy_evaluations_side_allowed"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Nullable when the verdict came from the bundle as a whole rather than one rule.
    policy_id: uuid.UUID | None = Field(default=None, foreign_key="policies.id", index=True, ondelete="SET NULL")
    change_set_id: uuid.UUID | None = Field(default=None, foreign_key="change_sets.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    operation: str = Field(max_length=64)
    result: str = Field(max_length=32)
    reason: str = Field(max_length=1024)
    side: str = Field(max_length=16)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))


class PolicyBundle(SQLModel, table=True):
    """PRD D6 `policy_bundles`. The signed artifact an agent pins itself to.

    Exactly one bundle may be active per scope, while every superseded bundle is
    kept — a device that pinned an old digest must still be explainable. That needs
    two PARTIAL unique indexes, not one constraint:

    * per project, uniqueness on `project_id` where the row is active;
    * globally, uniqueness on `active` where the row is active and unscoped.

    The second index looks odd until you notice that SQL treats NULLs as distinct,
    so the first index cannot constrain the global scope at all: two active bundles
    with `project_id IS NULL` do not collide. Within the second index's filtered
    set, `active` is always `true`, so uniqueness on that column admits exactly one
    row. An expression index on `(project_id IS NULL)` would work too, but Alembic
    renders expression indexes as opaque textual elements, so `alembic check` could
    never confirm the model and the database agree about it.
    """

    __tablename__ = "policy_bundles"
    __table_args__ = (
        UniqueConstraint("digest", name="uq_policy_bundles_digest"),
        Index(
            "uq_policy_bundles_one_active_per_project",
            "project_id",
            unique=True,
            postgresql_where=text("active AND project_id IS NOT NULL"),
        ),
        Index(
            "uq_policy_bundles_one_active_global",
            "active",
            unique=True,
            postgresql_where=text("active AND project_id IS NULL"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    digest: str = Field(max_length=71)  # "sha256:" + 64
    bundle: bytes = Field(sa_column=Column("bundle", LargeBinary, nullable=False))
    project_id: uuid.UUID | None = Field(default=None, foreign_key="projects.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    active: bool = Field(default=False)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
