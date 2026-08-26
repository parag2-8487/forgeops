# SPDX-License-Identifier: FSL-1.1-ALv2
"""Admit `pending` to `generation_runs.served_from`, for a run that is still in flight.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-26

Design: §6.2, §6.5, §11.5, §13.7.

WHY AN ELEVENTH REVISION
------------------------
`generation/routes.py::_insert_run` wrote the row for a new run as

    ... status, iterations_used, served_from, tier ...
    VALUES (..., 'running', 0, 'template', 'deterministic', ...)

with `served_from` and `tier` as SQL STRING LITERALS rather than bound parameters. Two consequences
followed from that single line. `served_from` could never record anything but `template`, so the
four other values `0008` put in the CHECK constraint — `l1`, `l2`, `l3`, `provider` — were
unreachable by construction; and every row claimed a template had served it before the pipeline had
run at all.

Binding the parameter needs a value for the state in between. The column is NOT NULL, and a row
INSERTed as `running` has genuinely not been served from anywhere yet. The alternative considered
was to state the path the service was ABOUT to attempt, which is a claim about an outcome that has
not happened — and is simply wrong for a run that crashes mid-stream, which is the case the
insert-before-streaming order exists to leave evidence of.

`l3` is left in place untouched. It has no writer either, but it names a designed cache tier
(§13.4) rather than a hole this revision opens; removing it is a separate decision.

WHY THIS IS SAFE TO RUN FORWARDS
--------------------------------
This is a WIDENING: every value the old constraint admitted is still admitted, so no stored row can
be stranded and Postgres's validation of the new CHECK against existing rows cannot fail. That is
the opposite of `0010`'s situation, which narrowed a vocabulary and therefore had to inspect the
table first. Nothing is inspected here because there is nothing a widening can break.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from src.generation.models import SERVED_FROM, in_list

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_generation_runs_served_from_allowed"

#: `0008`'s vocabulary, written out as a literal **because it no longer exists in code**.
#:
#: `0008` generates its constraint from `src.generation.models.SERVED_FROM`, which this commit
#: widens — so on a database built from scratch, `0008` already creates the wider constraint and
#: this revision replaces it with an identical one. That is the same arrangement `0010` has with
#: `0004` and it is deliberate: the generated form keeps the application and the schema unable to
#: drift, and the cost is one redundant swap on a fresh migrate. The only honest place for a
#: superseded list is the revision that superseded it, because `downgrade` has to restore it.
PREVIOUS_SERVED_FROM: tuple[str, ...] = ("l1", "l2", "l3", "provider", "template")


def upgrade() -> None:
    """Widen the constraint to include `pending`."""
    op.drop_constraint(CONSTRAINT, "generation_runs", type_="check")
    op.create_check_constraint(CONSTRAINT, "generation_runs", in_list("served_from", SERVED_FROM))


def downgrade() -> None:
    """Restore `0008`'s narrower vocabulary as `NOT VALID`, grandfathering rows in flight.

    The asymmetry with `upgrade` follows `0010`'s reasoning exactly. A downgrade that refused
    whenever a row carried `pending` would be a downgrade nobody can run, and the first thing it
    breaks is `alembic downgrade base` — which every §6.5 revision proof runs before it migrates up.
    `NOT VALID` constrains every new write while leaving an interrupted run's row readable, and a
    downgrade must never delete evidence to satisfy a constraint.
    """
    stranded = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM generation_runs WHERE served_from NOT IN "
                "(" + ", ".join(f"'{value}'" for value in PREVIOUS_SERVED_FROM) + ")"
            )
        )
        .scalar()
    )
    if stranded:
        print(  # noqa: T201 - a migration's only channel to the operator running it
            f"alembic 0011 downgrade: {CONSTRAINT} restored as NOT VALID because {stranded} "
            "generation_runs row(s) carry a served_from revision 0008's vocabulary does not "
            "permit. Those rows are left intact and readable; new writes are constrained."
        )
    op.drop_constraint(CONSTRAINT, "generation_runs", type_="check")
    op.execute(
        f"ALTER TABLE generation_runs ADD CONSTRAINT {CONSTRAINT} "
        f"CHECK ({in_list('served_from', PREVIOUS_SERVED_FROM)}) NOT VALID"
    )
