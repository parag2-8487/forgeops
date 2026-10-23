"""Incidents, the evidence consulted, the conclusions drawn, and the fixes proposed.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-23

Phase 2 §2.11. Four tables rather than one, and the reason is in `src/incidents/models.py`: what happened,
what was looked at, what was concluded, and what was proposed are separate facts with separate lifetimes.
Folding them together would make "not analysed" indistinguishable from "analysed and found nothing".

THE CHECK CONSTRAINTS ARE THE POINT OF DOING THIS IN A MIGRATION rather than leaving it to the model. A
`source` or `state` outside its vocabulary is a row nothing can interpret, and the database is the only
place that refuses it regardless of which code path wrote it -- the same argument as
`ck_change_sets_operation`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        # Starts at one, not zero: a row exists because something was seen once.
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("origin_kind", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("origin_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('deployment_failure', 'build_failure', 'kubernetes_event', "
            "'container_exit', 'log_pattern', 'circuit_breaker')",
            name="ck_incidents_source",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_incidents_severity",
        ),
        # An occurrence count below one would mean a row recording that nothing happened.
        sa.CheckConstraint("occurrences >= 1", name="ck_incidents_occurrences_positive"),
    )
    op.create_index("ix_incidents_project_detected_at", "incidents", ["project_id", "detected_at"])
    # PARTIALLY UNIQUE, over unresolved incidents only, and this index is what makes deduplication
    # correct rather than merely likely. `ingest` is one INSERT .. ON CONFLICT statement instead of a
    # select-then-insert, because two agents reporting the same crash-looping pod in the same second
    # would both find no row and both insert -- and that window is exactly when the source is firing
    # fastest. ON CONFLICT needs a unique index to arbitrate against, so this is it.
    #
    # Scoped to `resolved_at IS NULL` deliberately: a problem that recurs after somebody resolved it
    # SHOULD file a new incident. Folding it back into the closed one would hide a regression inside a
    # row an operator has already dismissed.
    op.create_index(
        "ix_incidents_fingerprint",
        "incidents",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    op.create_table(
        "incident_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        # DEFAULTS FALSE. A row written by a path that forgot to set it reads as "did not answer", which is
        # the safe direction: it understates what was known rather than overstating it.
        sa.Column("reachable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_incident_evidence_incident", "incident_evidence", ["incident_id", "collected_at"])

    op.create_table(
        "incident_analyses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("problem", sa.Text(), nullable=False, server_default=""),
        sa.Column("location", sa.Text(), nullable=False, server_default=""),
        sa.Column("fix", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence_reachable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_consulted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("served_from", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "state IN ('not_analysed', 'unavailable', 'insufficient_evidence', 'analysed', 'inconclusive')",
            name="ck_incident_analyses_state",
        ),
        # THE CONSTRAINT THAT MATTERS: a row claiming state `analysed` must carry a problem and a location.
        # An analysis with a verdict and no content is the fabrication this section invites, and the
        # database refuses it regardless of which code path wrote the row.
        sa.CheckConstraint(
            "state <> 'analysed' OR (length(problem) > 0 AND length(location) > 0)",
            name="ck_incident_analyses_analysed_has_content",
        ),
        # And the converse: content without the state would be a conclusion nothing stands behind.
        sa.CheckConstraint(
            "state = 'analysed' OR length(problem) = 0",
            name="ck_incident_analyses_unanalysed_is_empty",
        ),
        sa.CheckConstraint(
            "evidence_reachable <= evidence_consulted",
            name="ck_incident_analyses_evidence_counts",
        ),
    )
    op.create_index("ix_incident_analyses_incident", "incident_analyses", ["incident_id", "created_at"])

    op.create_table(
        "incident_fix_suggestions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), sa.ForeignKey("incident_analyses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("proposed_content", sa.Text(), nullable=False),
        sa.Column("observed_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        # NOT cascade-deleted with the change set: a suggestion outlives the change set made from it, and
        # losing the link would erase the record that somebody acted on this analysis.
        sa.Column("change_set_id", sa.Uuid(), sa.ForeignKey("change_sets.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # A suggestion proposing nothing is not a suggestion.
        sa.CheckConstraint("length(proposed_content) > 0", name="ck_incident_fix_suggestions_has_content"),
    )
    op.create_index("ix_incident_fix_suggestions_analysis_id", "incident_fix_suggestions", ["analysis_id"])
    op.create_index("ix_incident_fix_suggestions_change_set_id", "incident_fix_suggestions", ["change_set_id"])
    op.create_index("ix_incident_fix_suggestions_incident", "incident_fix_suggestions", ["incident_id", "created_at"])


def downgrade() -> None:
    op.drop_table("incident_fix_suggestions")
    op.drop_table("incident_analyses")
    op.drop_table("incident_evidence")
    op.drop_table("incidents")
