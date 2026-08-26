# SPDX-License-Identifier: FSL-1.1-ALv2
"""Give `embeddings_local` the cAST metadata `embeddings` has carried since revision 0003.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-26

WHY A TWELFTH REVISION

Revision `0003` added seven nullable cAST columns — `symbol`, `parent_symbol`, `signature`, `kind`,
`start_line`, `end_line`, `token_count` — to `embeddings`, and its own comment says they are what
turn a chunk from "1200 characters of something" into "the body of Repo.Save, lines 40-78". D-48
later added `embeddings_local` for the 1024-d self-hosted vectors a `vector(1536)` column cannot
hold, and that table was created WITHOUT those columns.

The asymmetry was invisible until something wrote to the local table. `_persist_embeddings` chooses
its target from `embedder.table` and writes one INSERT for either, so the first real scan under
`EMBEDDING_BACKEND=bge_m3` failed with `column "symbol" of relation "embeddings_local" does not
exist` — after having computed every vector. A scan would take the full embedding cost and then
persist nothing.

THE SHAPE MATTERS MORE THAN THE CRASH. `GET /analysis/codebase/{id}/symbols` reads the metadata off
`embeddings`. Had the INSERT been made conditional on the target table instead, a project on the
self-hosted backend would have had a complete-looking vector index whose symbols were permanently
invisible, and the endpoint would have answered with an empty list that was indistinguishable from
"this project has no functions". Two vector tables that hold different facts about the same chunk is
the defect; one INSERT that works for both is the fix.

Additive and nullable, so there is no backfill and no rewrite: existing rows keep NULL, which is
what they honestly are — those chunks were stored before the columns existed and their declarations
were never recorded. A rescan fills them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "embeddings_local"

#: Mirrors revision `0003`'s columns on `embeddings` exactly, including the widths. A `symbol`
#: capped at a different length on one table than the other would truncate the same declaration
#: differently depending on which embedding backend a project used, and the two indexes would
#: disagree about a name.
COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("symbol", sa.String(length=512), nullable=True),
    sa.Column("parent_symbol", sa.String(length=512), nullable=True),
    sa.Column("signature", sa.Text(), nullable=True),
    sa.Column("kind", sa.String(length=32), nullable=True),
    sa.Column("start_line", sa.Integer(), nullable=True),
    sa.Column("end_line", sa.Integer(), nullable=True),
    sa.Column("token_count", sa.Integer(), nullable=True),
    sa.Column("chunk_metadata", sa.dialects.postgresql.JSONB(), nullable=True),
)


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column(TABLE, column.copy())


def downgrade() -> None:
    # Reverse order, so the drop mirrors the add. Dropping these loses the declarations, which is
    # recoverable by rescanning — the vectors themselves are untouched.
    for column in reversed(COLUMNS):
        op.drop_column(TABLE, column.name)
