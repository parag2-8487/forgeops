# SPDX-License-Identifier: FSL-1.1-ALv2
"""Per-attempt evidence on a generation run.

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-24

WHY THIS COLUMN EXISTS
----------------------
A run that ends `template_fallback` could not say WHICH attempt failed or WHY. Three separate
investigations this cycle needed exactly that and had to reconstruct it from a live SSE stream, which is
gone the moment the request ends -- so the answer was unavailable for every run that had already happened.

The cause it finally exposed was a TIMEOUT: `model_http_timeout_seconds` defaulted to 300s while a real
`qwen2.5-coder:7b` attempt on CPU measured 155-817s, so attempts were aborted mid-flight and recorded as
refusals. A truncated attempt and a refused artifact are indistinguishable in the row as it was, and that
is precisely the confusion D-104 was about: the model was working correctly and would have answered.

With this column the question is answerable from the database: each attempt records when it started, how
long it ran, what became of it, and the reason for any non-delivery. A run that served templates now says
whether its attempts were refused on their merits or cut off.

`jsonb` rather than a child table: this is evidence read with the run and never queried across runs, and a
`generation_run_attempts` table would add a join to every read for no query anybody makes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_runs",
        sa.Column(
            "attempts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("generation_runs", "attempts")
