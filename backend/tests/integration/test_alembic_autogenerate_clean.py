# SPDX-License-Identifier: FSL-1.1-ALv2
"""`alembic check` reports no difference between the models and the migrated schema.

design.md §6.4, §6.5; tasks.md leaf 5.9; Appendix E criterion 12.

This is one assertion that catches three separate classes of mistake, which is why
§6.5 asks for it rather than for a per-table comparison:

* a **model/migration divergence** — a column added to a SQLModel class and forgotten
  in the revision, or the reverse;
* a **naming-convention slip** — an index or constraint whose name differs between the
  metadata and the database, which Alembic reports as a paired drop-and-create;
* a **raw-DDL omission** — an object created with `op.execute(...)` and never declared
  in the model. That is not hypothetical: writing this test found three, the two
  partial unique indexes on `policy_bundles` and the trigram index on `file_tree`,
  each of which `alembic check` reported as a pending removal on every run.

The `render_item` hook in `alembic/env.py` is what stops the pgvector `Vector` type
producing a spurious drop-and-create, and it is exercised here by construction: the
schema at head has two vector columns of different dimensions, so a hook that
rendered the dimension wrongly could not pass.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .migration_support import BACKEND_DIR, run_alembic

pytestmark = pytest.mark.mandatory


def _ddl(url: str, statement: str) -> None:
    """Run one DDL statement over a throwaway async engine.

    These tests are synchronous — `alembic check` is a subprocess, not a coroutine —
    so the one place that needs a connection drives its own loop rather than making
    the whole module async for a single statement.
    """

    async def _run() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(_run())


class TestAutogenerateIsClean:
    def test_alembic_check_reports_no_pending_operations(self, schema_at_head: str) -> None:
        result = run_alembic(schema_at_head, "check")
        assert result.returncode == 0, (
            "alembic check found a difference between SQLModel.metadata and the "
            "migrated schema. Every such difference is a real defect: either a model "
            "change with no migration, a migration with no model change, or an object "
            "created by raw DDL and never declared.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "No new upgrade operations detected" in (result.stdout + result.stderr), result.stdout + result.stderr

    def test_the_check_would_notice_a_divergence(self, schema_at_head: str) -> None:
        """The negative control. `alembic check` exiting 0 is only evidence if it can
        exit non-zero, so an unrelated table is created behind the models' back and the
        check must report it — then it is removed and the check must be clean again.

        A standalone table is used rather than a column on a real one because nothing
        references it, so even a failed cleanup cannot affect another test.
        """
        control = "forgeops_autogen_control"
        _ddl(schema_at_head, f"CREATE TABLE {control} (id integer PRIMARY KEY)")
        try:
            dirty = run_alembic(schema_at_head, "check")
            assert dirty.returncode != 0, (
                "alembic check passed with an undeclared table present, so a clean run "
                "proves nothing:\n" + dirty.stdout + dirty.stderr
            )
            assert control in (dirty.stdout + dirty.stderr), dirty.stdout + dirty.stderr
        finally:
            _ddl(schema_at_head, f"DROP TABLE IF EXISTS {control}")

        clean = run_alembic(schema_at_head, "check")
        assert clean.returncode == 0, clean.stdout + clean.stderr


class TestTheRenderItemHookCoversBothVectorDimensions:
    def test_env_py_renders_the_dimension_from_the_type(self) -> None:
        """`return f"Vector({obj.dim})"` is what makes the hook cover 1024-d by
        construction rather than by a second branch someone has to remember to add."""
        source = (BACKEND_DIR / "alembic" / "env.py").read_text(encoding="utf-8")
        assert "Vector({obj.dim})" in source, (
            "env.py no longer renders the vector dimension from the type; a hard-coded "
            "1536 would silently mis-render embeddings_local (D-48)"
        )

    def test_env_py_imports_every_model_module(self) -> None:
        """A model not imported here is invisible to autogenerate, which then proposes
        dropping its table — and `alembic check` is what turns that into a failure."""
        source = (BACKEND_DIR / "alembic" / "env.py").read_text(encoding="utf-8")
        for module in (
            "src.analysis.models",
            "src.audit.models",
            "src.auth.device_models",
            "src.auth.models",
            "src.generation.models",
            "src.governance.models",
            "src.policies.models",
            "src.projects.models",
            "src.secrets.models",
        ):
            assert module in source, f"env.py does not import {module}"
