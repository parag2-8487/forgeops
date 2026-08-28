# SPDX-License-Identifier: FSL-1.1-ALv2
"""`projects.archived_at`, and favourites that belong to a PERSON rather than to a project.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-28

PRD FR-02 (tags), FR-03 (favourites) and FR-05 (archive/delete) are all P0 and none of them had a
route on either side. This revision adds the only two schema changes the three of them need; tags
need none, and that is worth stating rather than leaving to be inferred.

TAGS REUSE `project_tags`, WHICH REVISION 0009 ALREADY CREATED
--------------------------------------------------------------
A join table with `UNIQUE (project_id, tag)` and an index on `project_id`, and nothing has ever
written to it. The alternative — a JSONB array in `projects.settings` — was rejected in one line:
filtering `WHERE 'prod' = ANY(tags)` server-side needs a GIN index and gives no uniqueness, so the
same tag could be added twice and the list screen would show it twice. The table already gives both.

FAVOURITES ARE PER USER, AND `projects.settings.favourite` WAS THE WRONG SHAPE
-----------------------------------------------------------------------------
`PROJECT_SETTINGS_KEYS` has carried a `favourite` boolean since revision 0009. It is per PROJECT, so
in any tenant with two people one person starring a project would reorder the other's list — a
shortcut is one person's, not the installation's. The key is left in place and left unused by the
new surface rather than removed: dropping it would need a data migration over live `settings`
documents for a flag whose only writer was a validation test, and `validate_project_settings` still
accepts it so no existing row becomes invalid. `GET /projects?favourite=true` reads THIS table.

`ON DELETE CASCADE` on both columns is right here in a way it is not for audit rows: a favourite is
a pointer, and a pointer to a deleted project or a deleted user is not a record of anything.

WHAT ARCHIVE IS, AND WHAT IT IS NOT
-----------------------------------
`archived_at` is nullable and reversible. It is deliberately NOT a status enum: an enum would invite
a third value later and force every query to enumerate which values mean "visible", whereas
`archived_at IS NULL` has exactly one reading and the timestamp answers "when" for free.

Archiving does not touch the index, the change sets or the secrets. That is the point of it being
distinct from delete: an archived project is one you have stopped working on, and losing its history
when you stop working on it is the opposite of what an archive is for.

The partial index exists because the list endpoint's default predicate is `archived_at IS NULL`, so
that is the set worth indexing; indexing the archived rows too would be paying for a page nobody
opens.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    # Partial: the default list predicate is `archived_at IS NULL`, and `(created_at, id)` is the
    # keyset the cursor orders by, so this covers the ordinary page end to end.
    op.create_index(
        "ix_projects_active_created_at",
        "projects",
        ["created_at", "id"],
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    op.create_table(
        "project_favourites",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # The pair IS the identity. No surrogate id: a second row for the same user and project
        # would be meaningless, and a composite primary key makes that unrepresentable rather than
        # merely constrained.
        sa.PrimaryKeyConstraint("user_id", "project_id", name="pk_project_favourites"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_project_favourites_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_favourites_project_id_projects",
            ondelete="CASCADE",
        ),
    )
    # The primary key already indexes `(user_id, project_id)`, which serves "this user's
    # favourites". The reverse direction is what the DELETE cascade walks when a project is removed.
    op.create_index("ix_project_favourites_project_id", "project_favourites", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_favourites_project_id", table_name="project_favourites")
    op.drop_table("project_favourites")
    op.drop_index("ix_projects_active_created_at", table_name="projects")
    op.drop_column("projects", "archived_at")
