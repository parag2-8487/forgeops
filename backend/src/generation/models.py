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
    Text,
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

#: Where a completed run's content came from.
#:
#: `pending` is the state of a row that has been INSERTed as `running` and not yet resolved. It
#: exists because `routes.py::_insert_run` wrote the SQL literal `'template'` into that row — a
#: claim about which pipeline served a run, made before the run had started. Four of the five
#: values below were unreachable by construction as a result. A NOT NULL column needs *some*
#: value for a row in flight, and the honest one is the one that says "not yet".
SERVED_FROM: tuple[str, ...] = ("pending", "l1", "l2", "l3", "provider", "template")


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

    # ─── Revision 0016: the instruction the model was actually given ──────────
    #
    # DECLARED HERE, NOT ONLY IN THE MIGRATION. `alembic check` compares this metadata against the
    # migrated schema and treats any difference as a defect — a model change with no migration, a
    # migration with no model change, or an object created by raw DDL and never declared. These columns
    # were the second kind: the migration created them, the model did not know about them, and the check
    # correctly proposed dropping all five.
    #
    # `compiled_prompt` is NULL for a run made before the compiler existed, or for one driven by free
    # text with no readiness findings behind it. That reads as "not recorded", which is true; an empty
    # string would read as "the model was sent nothing", which is a different and false claim.
    compiled_prompt: str | None = Field(default=None, sa_column=Column("compiled_prompt", Text, nullable=True))
    #: What the compiler estimated, beside the budget it was held to. Both are stored because a prompt
    #: that dropped a section is only explicable next to the limit that forced the drop.
    prompt_token_estimate: int | None = Field(default=None)
    prompt_token_budget: int | None = Field(default=None)
    #: The failing checks this run set out to fix, and the ones the budget could not hold. Kept apart
    #: because a user looking at an unchanged score needs to know a check was never attempted rather
    #: than attempted and rejected — only the first is fixed by narrowing the request.
    addressed_checks: list | None = Field(default=None, sa_column=Column("addressed_checks", JSONB, nullable=True))
    deferred_checks: list | None = Field(default=None, sa_column=Column("deferred_checks", JSONB, nullable=True))
