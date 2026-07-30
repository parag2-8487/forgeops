# SPDX-License-Identifier: FSL-1.1-ALv2
"""GenerationRun (design.md §6.2, §6.5 `0008`, §11.5, Appendix E criterion 3).

`iterations_used BETWEEN 0 AND 3` is the §3.8 bound expressed a third time. It is
already in the type (`Literal[3]` on `generation_max_iterations`, so an environment
variable cannot raise it) and in the property (Q-08's termination proof). Three
independent expressions of one invariant is not redundancy here: it is what makes a
regression in any single layer impossible to ship quietly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

#: §3.8's hard bound. Named so the migration and the loop read the same number.
MAX_GENERATION_ITERATIONS = 3

GENERATION_STATUSES: tuple[str, ...] = (
    "running",
    "accepted",
    "template_fallback",
    "unavailable",
    "failed",
)

SERVED_FROM: tuple[str, ...] = ("l1", "l2", "l3", "provider", "template")


def in_list(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({rendered})"


class GenerationRun(SQLModel, table=True):
    """One §1.5 generation attempt, with the NFR-04 cost and latency evidence."""

    __tablename__ = "generation_runs"
    __table_args__ = (
        CheckConstraint(
            f"iterations_used BETWEEN 0 AND {MAX_GENERATION_ITERATIONS}",
            name="ck_generation_runs_iterations_bounded",
        ),
        CheckConstraint(in_list("status", GENERATION_STATUSES), name="ck_generation_runs_status_allowed"),
        CheckConstraint(in_list("served_from", SERVED_FROM), name="ck_generation_runs_served_from_allowed"),
        Index("ix_generation_runs_project_id_created_at", "project_id", "created_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    requested_by: uuid.UUID | None = Field(default=None, foreign_key="users.id", ondelete="SET NULL")
    status: str = Field(max_length=32)
    iterations_used: int = Field(default=0)
    served_from: str = Field(max_length=16)
    tier: str = Field(max_length=32)
    endpoint_id: str | None = Field(default=None, max_length=100)
    # Advisory only. §11.5.5 makes the deterministic gate blocking and the rubric
    # informational, so this column must never be read as a pass/fail.
    rubric: dict | None = Field(default=None, sa_column=Column("rubric", JSONB, nullable=True))
    retrieval: dict | None = Field(default=None, sa_column=Column("retrieval", JSONB, nullable=True))
    prompt_tokens: int = Field(default=0)
    completion_tokens: int = Field(default=0)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    finished_at: datetime | None = Field(
        default=None, sa_column=Column("finished_at", DateTime(timezone=True), nullable=True)
    )
