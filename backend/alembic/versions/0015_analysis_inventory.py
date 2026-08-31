"""Persist the inventory a scan produced, not only its hash.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-01

WHAT WAS MISSING, AND WHY FR-11 COULD NOT WORK WITHOUT IT
---------------------------------------------------------
The agent's scan report has always carried an inventory: `languages`, `manifests`, `config_files`,
`entry_points`, a file count and a total size. `ScanInventoryIn` accepted all six. `analysis_reports`
stored **one** of them — `inventory_hash` — and dropped the rest on the floor.

So FR-11 ("record the project's structure: entry points, configuration files, manifests") was satisfied
in transit and nowhere else. The agent computed the answer, the backend validated it, and then the only
surviving trace was a sha256 that proves two scans agreed without saying what they agreed about. Nothing
could show an operator the entry points, and the generation prompt could not use them, because after the
request finished they did not exist.

The hash is not a substitute and was never meant to be one: its own docstring says it is *determinism
evidence*, which is a different job from being the record.

JSONB rather than four array columns and two integers, for the reason 0014 gives: the inventory's shape
is the agent's report schema, that schema is versioned (`ScanReportSchemaVersion`), and a field added
there should not need a migration here. This revision also lands `frameworks` and `package_managers`
(FR-10), which is exactly the kind of addition that argument anticipates — they are new in this pass and
need no column of their own.

`server_default='{}'` so existing rows are valid immediately. An empty object reads as "this report
predates the column", which is true of every row written before now and is not the same claim as "this
project has no entry points". A reader that needs the distinction has `report_version` to check.

NOT NULL for 0014's reason: a null and an empty object would mean the same thing to every reader, and
two spellings of one state is how a query starts needing `COALESCE`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analysis_reports",
        sa.Column(
            "inventory",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("analysis_reports", "inventory")
