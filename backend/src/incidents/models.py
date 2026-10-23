# SPDX-License-Identifier: FSL-1.1-ALv2
"""Incidents and root-cause analysis. Phase 2 §2.11, revision 0033.

THREE TABLES, AND THE SPLIT IS THE DESIGN.

`incidents` is what HAPPENED -- observed, ingested from a source that already existed. It holds no
conclusion.

`incident_evidence` is what was LOOKED AT, one row per source consulted, each carrying whether that source
was reachable. This is the table that keeps the analysis honest, and it exists because of the failure mode
this whole section invites: an RCA that read three of five sources and presents a confident cause, with no
way for a human to see the two it could not reach. A conclusion drawn from a partial view is not wrong, but
it is differently trustworthy, and the difference has to be visible.

`incident_analyses` is what was CONCLUDED, and it is deliberately separate from the incident so that an
incident can exist with NO analysis. That is the normal state when no model is configured. A schema that
folded the conclusion into the incident row would make "not analysed" indistinguishable from "analysed and
found nothing", which are opposite operational facts.

`incident_fix_suggestions` is what was PROPOSED. A suggestion is never applied here: applying it is a
mutation and goes through the chokepoint like any other. The `change_set_id` column records the governed
change set a human eventually created FROM the suggestion, and is null until then -- so the table also
answers "which suggestions did anyone act on", which is the only honest measure of whether the analysis is
useful.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Column, DateTime, ForeignKey, Index, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Where an incident came from. Every value is a source that ALREADY EXISTS in this system and already
# fails -- nothing here needs a new producer, which is why ingestion could be built without inventing a
# feed.
INCIDENT_SOURCES: tuple[str, ...] = (
    "deployment_failure",  # the settler's failure branch: the agent ran the command and it failed
    "build_failure",  # a validation gate rejected a generated artifact set
    "kubernetes_event",  # a Warning event read through the agent's read operations
    "container_exit",  # a container that exited non-zero, from the Docker probe
    "log_pattern",  # a matched pattern in the log store
    "circuit_breaker",  # the deployment breaker opened
)

# How far the analysis got. `not_analysed` is a FIRST-CLASS state and the default, because a deployment
# with no model configured is a supported deployment -- and a row that defaulted to `analysed` with an
# empty cause would read as "nothing to find".
ANALYSIS_STATES: tuple[str, ...] = (
    "not_analysed",  # no analysis has been attempted
    "unavailable",  # attempted, and no model could be reached
    "insufficient_evidence",  # attempted, and too few sources were reachable to conclude anything
    "analysed",  # a cause was identified
    "inconclusive",  # attempted with adequate evidence, and no cause could be identified
)

SEVERITIES: tuple[str, ...] = ("info", "warning", "critical")


class Incident(SQLModel, table=True):
    """One observed failure. Holds no conclusion -- see the module docstring."""

    __tablename__ = "incidents"
    __table_args__ = (
        Index("ix_incidents_project_detected_at", "project_id", "detected_at"),
        # Partially unique over unresolved incidents; see the migration for why deduplication depends
        # on it. `postgresql_where` must match the migration exactly or `alembic check` reports drift.
        Index(
            "ix_incidents_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # `ondelete` is declared HERE as well as in the migration. It is not decoration: `alembic check`
    # compares the model's view of the schema with the migration's, and a Field(foreign_key=...) carries
    # no ondelete -- so the two disagreed and autogenerate proposed dropping and recreating every
    # constraint. Caught by running `alembic check` rather than by review.
    project_id: uuid.UUID = Field(
        sa_column=Column("project_id", Uuid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    )
    source: str = Field(max_length=32)
    severity: str = Field(max_length=16)
    # A short, human sentence. NOT a template with a blank in it: an incident title an operator cannot
    # read at a glance is one they will not read at all.
    title: str = Field(max_length=512)
    # THE DEDUPLICATION KEY, and the reason it is stored rather than computed on read: the inputs that
    # produce it (a container name, an exit code, a pod's reason) may be gone by the time anyone looks,
    # so a fingerprint recomputed later could differ from the one that grouped the incident.
    # NO `index=True`: that would declare a SECOND index of the same name, plain and non-unique, which
    # shadows the partial unique index in `__table_args__` that deduplication depends on. `alembic check`
    # caught it as drift -- proposing to drop the unique index and add a plain one.
    fingerprint: str = Field(max_length=64)
    # How many times this fingerprint has been seen. Incremented rather than inserting a new row, because
    # an operator facing a crash-loop needs one incident saying "47 times" and not 47 incidents.
    occurrences: int = Field(default=1)
    # The originating record, when there is one, so an incident can be traced back to the deployment or
    # change set that produced it. Deliberately NOT a foreign key: the sources are heterogeneous and a
    # constraint per source would mean six nullable columns.
    origin_kind: str = Field(default="", max_length=32)
    origin_id: str = Field(default="", max_length=128)
    detail: dict[str, Any] = Field(sa_column=Column("detail", JSONB, nullable=False))
    detected_at: datetime = Field(
        sa_column=Column("detected_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )
    last_seen_at: datetime = Field(
        sa_column=Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column("resolved_at", DateTime(timezone=True), nullable=True)
    )


class IncidentEvidence(SQLModel, table=True):
    """One source consulted, and whether it answered.

    A row is written for a source that FAILED as readily as for one that answered. That is the point: the
    absence of a row would be indistinguishable from a source nobody thought to check.
    """

    __tablename__ = "incident_evidence"
    __table_args__ = (Index("ix_incident_evidence_incident", "incident_id", "collected_at"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    incident_id: uuid.UUID = Field(
        sa_column=Column("incident_id", Uuid(), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    )
    # `metrics`, `logs`, `kubernetes`, `deployment_history`, `change_set`.
    kind: str = Field(max_length=32)
    # FALSE when the source could not be read. The analysis counts these before concluding anything.
    reachable: bool = Field(default=False)
    # Plain prose describing what was found OR why nothing was. Rendered to the operator verbatim, so it
    # must read as a sentence rather than as a status code.
    summary: str = Field(sa_column=Column("summary", Text, nullable=False))
    detail: dict[str, Any] = Field(sa_column=Column("detail", JSONB, nullable=False))
    collected_at: datetime = Field(
        sa_column=Column("collected_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )


class IncidentAnalysis(SQLModel, table=True):
    """What was concluded, if anything. Separate from the incident so that "none" is expressible."""

    __tablename__ = "incident_analyses"
    __table_args__ = (Index("ix_incident_analyses_incident", "incident_id", "created_at"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    incident_id: uuid.UUID = Field(
        sa_column=Column("incident_id", Uuid(), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    )
    state: str = Field(max_length=32)
    # The three parts §2.11's display box asks for. Empty strings where the state is not `analysed`, and
    # the frontend renders the STATE rather than the blanks -- three empty fields would read as an
    # analysis that found nothing.
    problem: str = Field(default="", sa_column=Column("problem", Text, nullable=False))
    location: str = Field(default="", sa_column=Column("location", Text, nullable=False))
    fix: str = Field(default="", sa_column=Column("fix", Text, nullable=False))
    # How many evidence sources were reachable, out of how many were consulted. Stored rather than counted
    # from `incident_evidence` at read time, because it is a property OF THIS ANALYSIS: a later analysis
    # of the same incident may have reached more.
    evidence_reachable: int = Field(default=0)
    evidence_consulted: int = Field(default=0)
    # Which model answered, and from where. Null model with state `unavailable` is the honest record of a
    # deployment with no model -- and `served_from` lets a reader tell a fresh analysis from a cached one.
    model: str = Field(default="", max_length=128)
    served_from: str = Field(default="", max_length=16)
    created_at: datetime = Field(
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )


class IncidentFixSuggestion(SQLModel, table=True):
    """A proposed remedy. Never applied from here -- applying is a governed mutation."""

    __tablename__ = "incident_fix_suggestions"
    __table_args__ = (
        Index("ix_incident_fix_suggestions_incident", "incident_id", "created_at"),
        Index("ix_incident_fix_suggestions_analysis_id", "analysis_id"),
        Index("ix_incident_fix_suggestions_change_set_id", "change_set_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    incident_id: uuid.UUID = Field(
        sa_column=Column("incident_id", Uuid(), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    )
    analysis_id: uuid.UUID = Field(
        sa_column=Column("analysis_id", Uuid(), ForeignKey("incident_analyses.id", ondelete="CASCADE"), nullable=False)
    )
    # The file the suggestion touches, and the proposed content. A diff is RENDERED from these rather than
    # stored: a stored diff can disagree with the file it claims to patch once the file moves on, and a
    # diff preview that lies about the current state is worse than none.
    path: str = Field(max_length=1024)
    proposed_content: str = Field(sa_column=Column("proposed_content", Text, nullable=False))
    # What the file held when the suggestion was made, so the preview can say whether it still holds that.
    observed_content: str = Field(default="", sa_column=Column("observed_content", Text, nullable=False))
    rationale: str = Field(default="", sa_column=Column("rationale", Text, nullable=False))
    # Set when a human turned this into a governed change set. Null means nobody acted on it -- which is
    # the only honest measure of whether the analysis was useful.
    # NO cascade, deliberately: a suggestion outlives the change set made from it, and losing the link
    # would erase the record that somebody acted on this analysis.
    change_set_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column("change_set_id", Uuid(), ForeignKey("change_sets.id"), nullable=True),
    )
    created_at: datetime = Field(
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )


class HealingAction(SQLModel, table=True):
    """One self-healing attempt, INCLUDING the ones a guard rail refused.

    A refused action is a row and not a silence. "The system did nothing" and "the system decided not to"
    are different facts, and an operator asking why a container was never restarted needs the second.

    The `auto = false OR remedy IN (safe set)` CHECK in revision 0034 is the two-tier split expressed
    where no future caller can bypass it -- see the migration for why that duplication is deliberate.
    """

    __tablename__ = "healing_actions"
    __table_args__ = (
        Index("ix_healing_actions_incident", "incident_id", "created_at"),
        Index("ix_healing_actions_created_at", "created_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    incident_id: uuid.UUID = Field(
        sa_column=Column("incident_id", Uuid(), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    )
    remedy: str = Field(max_length=64)
    operation: str = Field(max_length=64)
    arguments: dict[str, Any] = Field(sa_column=Column("arguments", JSONB, nullable=False))
    auto: bool = Field(default=False)
    state: str = Field(max_length=16)
    change_set_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column("change_set_id", Uuid(), ForeignKey("change_sets.id"), nullable=True),
    )
    note: str = Field(default="", sa_column=Column("note", Text, nullable=False))
    created_at: datetime = Field(
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column("completed_at", DateTime(timezone=True), nullable=True)
    )


class IncidentPostmortem(SQLModel, table=True):
    """The summary and recommendations written after an incident.

    Separate from the analysis because they answer different questions at different times: an analysis says
    what broke, a postmortem says what happened and what to change so it does not recur.
    """

    __tablename__ = "incident_postmortems"
    __table_args__ = (Index("ix_incident_postmortems_incident", "incident_id", "created_at"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    incident_id: uuid.UUID = Field(
        sa_column=Column("incident_id", Uuid(), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    )
    state: str = Field(max_length=16)
    summary: str = Field(default="", sa_column=Column("summary", Text, nullable=False))
    recommendations: list[Any] = Field(default_factory=list, sa_column=Column("recommendations", JSONB, nullable=False))
    model: str = Field(default="", max_length=128)
    actions_considered: int = Field(default=0)
    created_at: datetime = Field(
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )
