# SPDX-License-Identifier: FSL-1.1-ALv2
"""Change sets, items, validations, approvals and rollback handles.

design.md §6.2, §6.3, §6.5 revision `0004`, §3.6 (the lifecycle state machine).

Two columns carry more weight than their type suggests.

`change_sets.version` is optimistic concurrency. Two reviewers approving the same
change set concurrently must not both win, and the apply path reads the version it
approved; a mismatch is a `409`, not a silent overwrite.

`change_items.old_hash` is what makes a *stale* apply detectable at the agent. The
agent recomputes the hash of the file it is about to change and refuses if it does
not match what the change set was built against, which is the difference between
"apply this diff" and "apply this diff to the world I inspected".

**Constraint names are written out in full**, including the `ck_`/`uq_` prefix that
`core/db.py`'s `NAMING_CONVENTION` would otherwise supply. SQLModel keeps its own
`MetaData`, which does not carry that convention, and a convention applied by
mutating `SQLModel.metadata` would depend on import order — the constraint name
is fixed when the `Table` is constructed, so a model imported before the mutation
would silently get a different name. Spelling the names out is import-order
independent and is what `test_alembic_autogenerate_clean.py` compares against.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

#: §3.6's state machine, as data. The check constraints below are generated from
#: these tuples so the schema and the code cannot drift apart.
#:
#: **Corrected by D-63, revision `0010`.** This tuple originally read `draft`,
#: `validating`, `validated`, `awaiting_approval`, `approved`, `rejected`, `applying`,
#: `applied`, `failed`, `rolled_back` — six of §3.6's thirteen states were missing and
#: three states §3.6 does not define had been invented. The database therefore could
#: not store `blocked`, `pending_approval` or `reverted`, which are three of the six
#: outcomes Appendix A.3's chokepoint transit produces, and Q-22 ("only edges in the
#: §3.6 state machine are accepted") was unprovable against it. The list below is
#: §3.6's, in lifecycle order, and nothing else.
CHANGE_SET_STATUSES: tuple[str, ...] = (
    "draft",
    "validating",
    "rejected_by_policy",
    "blocked",
    "pending_approval",
    "approved",
    "rejected",
    "expired",
    "applying",
    "applied",
    "rolled_back",
    "conflicted",
    "reverted",
)

#: The states §3.6 marks terminal. Held separately because "terminal states are
#: absorbing" is a rule about transitions, not about the vocabulary, and Q-22 asserts
#: it: a transition **out of** any of these is illegal, including to itself.
TERMINAL_CHANGE_SET_STATUSES: tuple[str, ...] = (
    "rejected_by_policy",
    "blocked",
    "rejected",
    "expired",
    "applied",
    "reverted",
    "rolled_back",
    "conflicted",
)

#: §3.6's edges, as data — the single source Q-22 quantifies over.
#:
#: `applied → reverted` is the only edge leaving a success state, and it is labelled
#: "rollback handle used": the original set becomes `reverted` when the reverse change
#: set compiled by `GovernanceChokepoint.revert` has been applied and its handle
#: consumed (D-66), not when the revert is requested.
CHANGE_SET_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("draft", "validating"),
    ("validating", "rejected_by_policy"),
    ("validating", "blocked"),
    ("validating", "pending_approval"),
    ("validating", "approved"),
    ("pending_approval", "approved"),
    ("pending_approval", "rejected"),
    ("pending_approval", "expired"),
    ("approved", "applying"),
    ("applying", "applied"),
    ("applying", "rolled_back"),
    ("applying", "conflicted"),
    ("applied", "reverted"),
)

CHANGE_ITEM_ACTIONS: tuple[str, ...] = ("create", "update", "delete")

CHANGE_SET_ORIGINS: tuple[str, ...] = ("generation", "manual", "policy")

APPROVAL_STATUSES: tuple[str, ...] = ("approved", "rejected")


def in_list(column: str, values: tuple[str, ...]) -> str:
    """Render an `IN (...)` predicate from a Python tuple.

    Used by both the models and the `0004` migration, so the constraint the
    database enforces is generated from the same tuple the application validates
    against. A new state cannot be added to one without the other.
    """
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({rendered})"


class ChangeSet(SQLModel, table=True):
    """PRD D3 `change_sets`."""

    __tablename__ = "change_sets"
    __table_args__ = (
        CheckConstraint(in_list("status", CHANGE_SET_STATUSES), name="ck_change_sets_status_allowed"),
        CheckConstraint(in_list("origin", CHANGE_SET_ORIGINS), name="ck_change_sets_origin_allowed"),
        Index("ix_change_sets_project_status", "project_id", "status"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    status: str = Field(max_length=32)
    # Nullable: a change set may originate from the system rather than a person.
    created_by: uuid.UUID | None = Field(default=None, foreign_key="users.id", ondelete="SET NULL")
    origin: str = Field(max_length=16)
    # The foreign key is created in `0008`, not `0004`: `generation_runs` does not
    # exist until then. Declared here so the model and the database agree.
    generation_run_id: uuid.UUID | None = Field(
        default=None, foreign_key="generation_runs.id", index=True, ondelete="SET NULL"
    )
    blast_radius_score: int = Field(default=0)
    blast_radius_verdict: str = Field(max_length=32)
    policy_bundle_digest: str = Field(max_length=71)  # "sha256:" + 64
    version: int = Field(
        default=1,
        sa_column=Column("version", Integer, nullable=False, server_default=text("1")),
    )
    applied_at: datetime | None = Field(
        default=None, sa_column=Column("applied_at", DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))


class ChangeItem(SQLModel, table=True):
    """PRD D3 `change_items`. One file, one action, one ordinal."""

    __tablename__ = "change_items"
    __table_args__ = (
        UniqueConstraint("change_set_id", "ordinal", name="uq_change_items_change_set_id_ordinal"),
        CheckConstraint(in_list("action", CHANGE_ITEM_ACTIONS), name="ck_change_items_action_allowed"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    change_set_id: uuid.UUID = Field(foreign_key="change_sets.id", index=True, ondelete="CASCADE")
    file_path: str = Field(max_length=1024)
    action: str = Field(max_length=16)
    old_content: str | None = Field(default=None, sa_column=Column("old_content", Text, nullable=True))
    new_content: str | None = Field(default=None, sa_column=Column("new_content", Text, nullable=True))
    old_hash: str | None = Field(default=None, max_length=64)
    new_hash: str | None = Field(default=None, max_length=64)
    ordinal: int


class Validation(SQLModel, table=True):
    """PRD D3 `validations`. `output` is redacted before it is stored (Q-24)."""

    __tablename__ = "validations"
    __table_args__ = (Index("ix_validations_change_item_id_iteration", "change_item_id", "iteration"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    change_item_id: uuid.UUID = Field(foreign_key="change_items.id", index=True, ondelete="CASCADE")
    validator: str = Field(max_length=64)
    passed: bool
    blocking: bool
    output: str = Field(sa_column=Column("output", Text, nullable=False))
    iteration: int = Field(default=0)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))


class Approval(SQLModel, table=True):
    """PRD D3 `approvals`. One row per decision; decisions are never edited."""

    __tablename__ = "approvals"
    __table_args__ = (CheckConstraint(in_list("status", APPROVAL_STATUSES), name="ck_approvals_status_allowed"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    change_set_id: uuid.UUID = Field(foreign_key="change_sets.id", index=True, ondelete="CASCADE")
    # RESTRICT, not CASCADE: deleting a user must not erase the record of what they
    # approved. The same reasoning as the audit log's missing foreign keys (§6.3).
    approver_id: uuid.UUID = Field(foreign_key="users.id", index=True, ondelete="RESTRICT")
    status: str = Field(max_length=16)
    comment: str | None = Field(default=None, sa_column=Column("comment", Text, nullable=True))
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))


class RollbackHandle(SQLModel, table=True):
    """PRD D3 `rollback_handles`. At most one handle per change set (Q-02)."""

    __tablename__ = "rollback_handles"
    __table_args__ = (UniqueConstraint("change_set_id", name="uq_rollback_handles_change_set_id"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    change_set_id: uuid.UUID = Field(foreign_key="change_sets.id", ondelete="CASCADE")
    backup_manifest: dict = Field(sa_column=Column("backup_manifest", JSONB, nullable=False))
    agent_device_id: str = Field(max_length=64)
    consumed: bool = Field(default=False)
    expires_at: datetime = Field(sa_column=Column("expires_at", DateTime(timezone=True), nullable=False))
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
