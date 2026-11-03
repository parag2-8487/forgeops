# SPDX-License-Identifier: FSL-1.1-ALv2
"""Per-project learning: feedback, memory and skill files. Phase 2 §2.13, revision 0035.

FOUR TABLES, AND THE TWO-TIER SPLIT IS THE DESIGN.

`learning_feedback` is the RAW RECORD -- one row per accepted or rejected artifact. It is append-only and is
never rewritten, because it is the evidence everything else is derived from: a preference that turns out to
be wrong has to be traceable to the events that produced it, or correcting it is guesswork.

`learning_sessions` is the SHORT-TERM tier: conversation turns within one session. Bounded and disposable.
It exists because a follow-up question ("no, use 8080") is meaningless without the turn before it, and
nothing about that belongs in long-term memory.

`learning_preferences` is the LONG-TERM tier: statements the Reflector synthesised from feedback. **Every
one is editable and deletable by the user, and carries the feedback count it was derived from.** That is the
whole reason this section is dangerous otherwise: a preference store shapes every future prompt, so one the
user cannot see is one they cannot disagree with, and a system that cannot be corrected gets quietly worse
with every wrong inference.

`learning_skill_files` is the COMPILED form -- what actually gets injected into a prompt. Separate from
preferences because injection has a token budget and preferences do not: a skill file is a selected, ordered,
truncated view, and keeping it separate means the selection is inspectable rather than happening invisibly at
prompt-assembly time.

WHAT `source` ON A PREFERENCE IS FOR. `reflected` means the Reflector inferred it; `stated` means a human
wrote it directly. A stated preference OUTRANKS a reflected one and is never overwritten by reflection --
otherwise the user corrects the system and the system reverts, which is worse than not offering the
correction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Column, DateTime, ForeignKey, Index, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

#: What a human did with something the system produced.
#:
#: `edited` is deliberately distinct from `accepted` and `rejected`, and it is the most informative of the
#: three: an artifact the user kept but changed says exactly what was wrong with it, which neither of the
#: others does. Collapsing it into `accepted` would throw away the signal this table exists to capture.
FEEDBACK_VERDICTS: tuple[str, ...] = ("accepted", "rejected", "edited")

#: Where a preference came from. See the module docstring for why `stated` outranks `reflected`.
PREFERENCE_SOURCES: tuple[str, ...] = ("reflected", "stated")

#: What a preference is about, so injection can select rather than dumping everything.
PREFERENCE_SCOPES: tuple[str, ...] = (
    "dockerfile",
    "kubernetes",
    "ci",
    "iac",
    "style",
    "general",
)


class LearningFeedback(SQLModel, table=True):
    """One accepted, rejected or edited artifact. APPEND-ONLY: the evidence, not a conclusion."""

    __tablename__ = "learning_feedback"
    __table_args__ = (
        Index("ix_learning_feedback_project_created", "project_id", "created_at"),
        Index("ix_learning_feedback_run", "generation_run_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column("project_id", Uuid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    )
    #: The run that produced the artifact. Nullable because feedback can be given on something a human
    #: wrote, and refusing that would make the store a record of the model's work only.
    generation_run_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column("generation_run_id", Uuid(), ForeignKey("generation_runs.id"), nullable=True),
    )
    artifact_path: str = Field(max_length=1024)
    verdict: str = Field(max_length=16)
    #: Why, in the user's own words. Empty is permitted -- demanding a reason would make people click the
    #: verdict that needs no explanation, which biases the whole store.
    comment: str = Field(default="", sa_column=Column("comment", Text, nullable=False))
    #: For `edited`: what the artifact became. This is the highest-value field in the table, because the
    #: DIFFERENCE between what was produced and what was kept is a preference stated by demonstration.
    final_content: str = Field(default="", sa_column=Column("final_content", Text, nullable=False))
    created_by: uuid.UUID | None = Field(default=None)
    created_at: datetime = Field(
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )


class LearningSession(SQLModel, table=True):
    """Short-term memory: turns within one conversation.

    Bounded by `MAX_SESSION_TURNS` at the write site rather than by a sweep, so the bound cannot be exceeded
    between sweeps. Nothing here is promoted to long-term memory automatically -- reflection reads feedback,
    not chatter, because a user thinking aloud is not stating a preference.
    """

    __tablename__ = "learning_sessions"
    __table_args__ = (Index("ix_learning_sessions_session_turn", "session_key", "turn"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column("project_id", Uuid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    )
    #: Opaque, supplied by the caller. NOT the auth session id: a conversation can outlive a login and a
    #: login can hold several conversations, and conflating them would leak one user's context into another
    #: window or lose it on a token refresh.
    session_key: str = Field(max_length=128)
    turn: int
    role: str = Field(max_length=16)
    content: str = Field(sa_column=Column("content", Text, nullable=False))
    created_at: datetime = Field(
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )


class LearningPreference(SQLModel, table=True):
    """Long-term memory: one synthesised or stated preference. INSPECTABLE AND EDITABLE."""

    __tablename__ = "learning_preferences"
    __table_args__ = (
        Index("ix_learning_preferences_project_scope", "project_id", "scope"),
        # One statement per (project, scope, statement): reflection running twice must strengthen the
        # existing row rather than add a duplicate, or the skill file fills with the same sentence.
        Index(
            "ix_learning_preferences_unique_statement",
            "project_id",
            "scope",
            "statement_digest",
            unique=True,
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column("project_id", Uuid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    )
    scope: str = Field(max_length=32)
    #: The preference itself, as a sentence a human can read and disagree with.
    statement: str = Field(sa_column=Column("statement", Text, nullable=False))
    #: SHA-256 of the normalised statement. Stored because the uniqueness constraint needs a bounded key and
    #: `statement` is unbounded text -- an index on it would be refused past the page limit.
    statement_digest: str = Field(max_length=64)
    source: str = Field(max_length=16)
    #: How many feedback rows support this. Shown to the user, because "derived from one edit" and "derived
    #: from forty" deserve different confidence and the store cannot express that otherwise.
    evidence_count: int = Field(default=0)
    #: False hides it from injection WITHOUT deleting it, so a user can disable a preference and still see
    #: that it was inferred. A hard delete would lose the record that the system once believed it.
    active: bool = Field(default=True)
    created_at: datetime = Field(
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )
    updated_at: datetime = Field(
        sa_column=Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )


class LearningSkillFile(SQLModel, table=True):
    """The compiled, token-bounded view of active preferences that gets injected into a prompt."""

    __tablename__ = "learning_skill_files"
    __table_args__ = (Index("ix_learning_skill_files_project_created", "project_id", "created_at"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column("project_id", Uuid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    )
    #: The exact text injected, so a run can be judged rather than guessed at -- the same argument as
    #: recording the compiled prompt.
    content: str = Field(sa_column=Column("content", Text, nullable=False))
    #: Which preference ids were selected, and which were dropped for budget. The second half is what makes
    #: the selection auditable: a preference that never reaches a prompt looks identical to one that does
    #: not exist, from the output alone.
    included_preference_ids: list[Any] = Field(
        default_factory=list, sa_column=Column("included_preference_ids", JSONB, nullable=False)
    )
    excluded_preference_ids: list[Any] = Field(
        default_factory=list, sa_column=Column("excluded_preference_ids", JSONB, nullable=False)
    )
    created_at: datetime = Field(
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )
