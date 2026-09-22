"""Per-project learning: feedback, sessions, preferences and skill files.

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-23

Phase 2 §2.13. See `src/learning/models.py` for why this is four tables rather than one.

THE CONSTRAINTS ENCODE THE PROPERTIES THE FEATURE DEPENDS ON:

  * `ck_learning_preferences_statement_not_empty` -- a preference with no text is one a user cannot read or
    disagree with, which defeats the entire inspectability argument.
  * `ck_learning_feedback_edited_has_content` -- an `edited` verdict with no final content records that
    something changed while discarding WHAT changed, which is the only part worth keeping.
  * `ck_learning_preferences_evidence_non_negative` -- the count is shown to a user as confidence; a
    negative one would render as a number nobody could interpret.
  * A partial unique index on `(project_id, scope, statement_digest)` so reflection run twice strengthens a
    row rather than duplicating it -- without it the skill file fills with the same sentence.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "learning_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        # NOT cascade-deleted with the run: feedback outlives the run it was about, and losing it would
        # erase the evidence a preference was derived from.
        sa.Column("generation_run_id", sa.Uuid(), sa.ForeignKey("generation_runs.id"), nullable=True),
        sa.Column("artifact_path", sa.String(length=1024), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("final_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("verdict IN ('accepted', 'rejected', 'edited')", name="ck_learning_feedback_verdict"),
        # An `edited` row without the result records that something changed and discards what -- which is
        # the only part a preference can be derived from.
        sa.CheckConstraint(
            "verdict <> 'edited' OR length(final_content) > 0",
            name="ck_learning_feedback_edited_has_content",
        ),
    )
    op.create_index("ix_learning_feedback_project_created", "learning_feedback", ["project_id", "created_at"])
    op.create_index("ix_learning_feedback_run", "learning_feedback", ["generation_run_id"])

    op.create_table(
        "learning_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_key", sa.String(length=128), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_learning_sessions_role"),
        sa.CheckConstraint("turn >= 0", name="ck_learning_sessions_turn_non_negative"),
    )
    op.create_index("ix_learning_sessions_session_turn", "learning_sessions", ["session_key", "turn"])

    op.create_table(
        "learning_preferences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("statement_digest", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        # TRUE by default: a preference written by reflection is active, because one that had to be enabled
        # would mean the system learned and then did nothing with it. Deactivation is the user's move.
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "scope IN ('dockerfile', 'kubernetes', 'ci', 'iac', 'style', 'general')",
            name="ck_learning_preferences_scope",
        ),
        sa.CheckConstraint("source IN ('reflected', 'stated')", name="ck_learning_preferences_source"),
        # A preference with no text is one a user cannot read or disagree with.
        sa.CheckConstraint("length(statement) > 0", name="ck_learning_preferences_statement_not_empty"),
        sa.CheckConstraint("evidence_count >= 0", name="ck_learning_preferences_evidence_non_negative"),
    )
    op.create_index("ix_learning_preferences_project_scope", "learning_preferences", ["project_id", "scope"])
    # UNIQUE so reflection run twice strengthens rather than duplicates. Not partial: a deactivated
    # preference still occupies its statement, so re-reflecting it must find and strengthen the existing
    # row rather than inserting a second active one beside the one the user switched off.
    op.create_index(
        "ix_learning_preferences_unique_statement",
        "learning_preferences",
        ["project_id", "scope", "statement_digest"],
        unique=True,
    )

    op.create_table(
        "learning_skill_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("included_preference_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("excluded_preference_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_learning_skill_files_project_created", "learning_skill_files", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_table("learning_skill_files")
    op.drop_table("learning_preferences")
    op.drop_table("learning_sessions")
    op.drop_table("learning_feedback")
