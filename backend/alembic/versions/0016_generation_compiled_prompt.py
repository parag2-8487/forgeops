"""Store the exact instruction a generation run was given.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-05

WHY THE PROMPT HAS TO BE ON THE ROW
-----------------------------------
A generation run could be judged only by its output. `generation_runs` recorded the tier, the endpoint,
the token counts, the retrieval record and the outcome — everything except what the model was actually
asked to do. So when a run produced a file in the wrong place or invented a framework, there was no way
to tell whether the model had disobeyed a correct instruction or obeyed a bad one, and those two faults
have opposite fixes.

`compiled_prompt` closes that. It is the exact text sent, not a summary and not a template id: the
compiler derives every line from the index, so two runs of the same project at different scan states
produce different instructions and the difference is the interesting part.

WHY IT IS NULLABLE
------------------
Every existing row predates the compiler and there is nothing truthful to backfill. `NULL` reads as "not
recorded", which is the honest description of a run made before this column existed; a zero-length string
would read as "the model was sent nothing", which is a different and false claim. The same reasoning
`0015` applied to an absent inventory.

WHY TEXT AND NOT JSONB
----------------------
The value IS the text the provider received. Storing a structured decomposition would invite a reader to
reconstruct the prompt from parts, and a reconstruction is not the artifact — the whole purpose of the
column is that what is read back is byte-for-byte what was sent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_runs",
        sa.Column("compiled_prompt", sa.Text(), nullable=True),
    )
    # The token estimate the compiler worked to, and the budget it was given. Recorded beside the text
    # because "this prompt was 9k tokens" is only meaningful next to the budget it was measured against,
    # and a reader diagnosing a deferred artifact needs both numbers.
    op.add_column(
        "generation_runs",
        sa.Column("prompt_token_estimate", sa.Integer(), nullable=True),
    )
    op.add_column(
        "generation_runs",
        sa.Column("prompt_token_budget", sa.Integer(), nullable=True),
    )
    # Which failing checks the run set out to fix, and which it could not fit. Stored as JSONB rather
    # than a joined string so a UI can list them without parsing prose, and so an empty list is
    # distinguishable from an absent one.
    op.add_column(
        "generation_runs",
        sa.Column(
            "addressed_checks",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "generation_runs",
        sa.Column(
            "deferred_checks",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("generation_runs", "deferred_checks")
    op.drop_column("generation_runs", "addressed_checks")
    op.drop_column("generation_runs", "prompt_token_budget")
    op.drop_column("generation_runs", "prompt_token_estimate")
    op.drop_column("generation_runs", "compiled_prompt")
