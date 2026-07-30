# SPDX-License-Identifier: FSL-1.1-ALv2
"""Project SQLModel table (design.md §6.2).

Exactly one table in this domain for Phase 0. The rest of D1 (users, teams,
sessions, agent_devices) belongs to the identity/auth domain excluded with
authentication.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Project(SQLModel, table=True):
    """Minimal project record for Phase 0.

    The only D1 table; identity/auth tables are Phase 1.
    """

    __tablename__ = "projects"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Seam for Phase 1 PostgreSQL RLS. Nullable now; NOT NULL + policies arrive
    # in the Phase 1 migration so there is no backfill of live rows later.
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    name: str = Field(max_length=200, index=True)
    path: str = Field(max_length=1024)
    repo_url: str | None = Field(default=None, max_length=1024)
    settings: dict = Field(
        default_factory=dict,
        sa_column=Column("settings", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        )
    )


#: The only keys `projects.settings` may carry (design.md §6.5 `0009`, §11.3).
#:
#: `settings` is JSONB, so the database cannot reject an unknown key without a
#: check constraint per key — which would make every new setting a migration. The
#: validation therefore lives here, in one place, and is asserted by
#: `test_0009_projects.py`. An unknown key is refused rather than ignored: a typo
#: in `embedding_backend` that silently kept the default is the failure mode this
#: exists to prevent, and it would only surface as a project whose vectors are in
#: the wrong table.
PROJECT_SETTINGS_KEYS: frozenset[str] = frozenset(
    {
        "embedding_backend",
        "llm_budget_usd_month",
        "favourite",
        "auto_approve_readme_only",
        "max_file_size_bytes",
        "ignore_globs",
    }
)

#: D-48: a project reads exactly one vector table, chosen here. Changing this once
#: embeddings exist would mean two vector spaces for one project, which is why
#: §11.4 returns `409 project-embedding-backend-locked` instead.
EMBEDDING_BACKENDS: frozenset[str] = frozenset({"voyage", "bge_m3"})


class ProjectSettingsError(ValueError):
    """Raised when `projects.settings` carries an unknown key or an invalid value."""


def validate_project_settings(settings: dict) -> dict:
    """Validate `projects.settings` and return it unchanged.

    Raises:
        ProjectSettingsError: on an unknown key or an invalid value, naming the
            offending key. Returning a *cleaned* copy was rejected: silently
            dropping a key the caller believed they set is how a project ends up
            with settings nobody can account for.
    """
    if not isinstance(settings, dict):
        raise ProjectSettingsError(f"settings must be a mapping, got {type(settings).__name__}")

    unknown = sorted(set(settings) - PROJECT_SETTINGS_KEYS)
    if unknown:
        raise ProjectSettingsError(
            f"unknown project settings key(s) {unknown}; allowed: {sorted(PROJECT_SETTINGS_KEYS)}"
        )

    backend = settings.get("embedding_backend")
    if backend is not None and backend not in EMBEDDING_BACKENDS:
        raise ProjectSettingsError(f"embedding_backend must be one of {sorted(EMBEDDING_BACKENDS)}, got {backend!r}")

    budget = settings.get("llm_budget_usd_month")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int | float)):
        raise ProjectSettingsError("llm_budget_usd_month must be a number")
    if isinstance(budget, int | float) and not isinstance(budget, bool) and budget < 0:
        raise ProjectSettingsError("llm_budget_usd_month must not be negative")

    for flag in ("favourite", "auto_approve_readme_only"):
        value = settings.get(flag)
        if value is not None and not isinstance(value, bool):
            raise ProjectSettingsError(f"{flag} must be a boolean")

    max_size = settings.get("max_file_size_bytes")
    if max_size is not None and (isinstance(max_size, bool) or not isinstance(max_size, int)):
        raise ProjectSettingsError("max_file_size_bytes must be an integer")
    if isinstance(max_size, int) and not isinstance(max_size, bool) and max_size <= 0:
        raise ProjectSettingsError("max_file_size_bytes must be positive")

    globs = settings.get("ignore_globs")
    if globs is not None:
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise ProjectSettingsError("ignore_globs must be a list of strings")

    return settings


class ProjectTag(SQLModel, table=True):
    """§11.3's per-project tag. Unique per project, so a tag cannot be added twice.

    Constraint names are spelled out in full for the reason given in
    `governance/models.py`: SQLModel keeps its own `MetaData` without
    `core/db.py`'s naming convention, and mutating it would make the name depend
    on import order.
    """

    __tablename__ = "project_tags"
    __table_args__ = (UniqueConstraint("project_id", "tag", name="uq_project_tags_project_id_tag"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    tag: str = Field(max_length=64)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
