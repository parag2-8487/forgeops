# SPDX-License-Identifier: FSL-1.1-ALv2
"""Regressions found on 2026-11-01/02: hidden projects, and generation runs stuck in `running` forever.

AGAINST REAL POSTGRES, because both defects were facts about rows: a `tenant_id` an autonomous deployment
stamped onto a tenantless project (so every tenant-scoped list query stopped returning it), and
`generation_runs` rows that never left `running` once their SSE stream was abandoned.
"""

from __future__ import annotations

import importlib.util
import json
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from src.generation.routes import _abandon_run, reap_orphaned_runs
from src.generation.service import GenerationOutcome

pytestmark = pytest.mark.asyncio

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0040_generation_provenance_and_tenant_repair.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("rev_0040", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _project(conn: AsyncConnection, *, tenant: uuid.UUID | None = None, name: str = "disposable") -> uuid.UUID:
    project_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO projects (id, tenant_id, name, path, settings) VALUES (:id, :t, :n, '/tmp/x', '{}'::jsonb)"),
        {"id": project_id, "t": tenant, "n": name},
    )
    return project_id


async def _visible_to_tenantless_user(conn: AsyncConnection) -> set[uuid.UUID]:
    """The project list query's own tenant clause for a principal with no tenant (projects/routes.py)."""
    rows = await conn.execute(text("SELECT id FROM projects WHERE tenant_id IS NULL AND archived_at IS NULL"))
    return {row[0] for row in rows}


async def _run(
    conn: AsyncConnection, project_id: uuid.UUID, *, age_seconds: int = 0, status: str = "running"
) -> uuid.UUID:
    run_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO generation_runs (id, project_id, status, iterations_used, served_from, tier, "
            "prompt_tokens, completion_tokens, created_at) VALUES (:id, :p, :s, 0, 'pending', 'self_hosted', 0, 0, "
            "now() - make_interval(secs => :age))"
        ),
        {"id": run_id, "p": project_id, "s": status, "age": age_seconds},
    )
    return run_id


class TestSyntheticTenantRepair:
    async def test_a_project_hidden_by_the_old_deployer_becomes_visible_again(self, conn: AsyncConnection) -> None:
        module = _migration()
        hidden = await _project(conn, name="hidden-by-deployer")
        synthetic = module.synthetic_project_tenant(hidden)
        await conn.execute(text("UPDATE projects SET tenant_id = :t WHERE id = :p"), {"t": synthetic, "p": hidden})
        await conn.execute(
            text("UPDATE generation_runs SET tenant_id = :t WHERE project_id = :p"), {"t": synthetic, "p": hidden}
        )
        assert hidden not in await _visible_to_tenantless_user(conn), "precondition: the defect hides it"

        repaired = await conn.run_sync(lambda sync: module.repair_synthetic_project_tenants(sync))

        assert hidden in repaired
        assert hidden in await _visible_to_tenantless_user(conn)

    async def test_a_genuine_tenant_is_never_touched(self, conn: AsyncConnection) -> None:
        module = _migration()
        real_tenant = uuid.uuid4()
        tenanted = await _project(conn, tenant=real_tenant, name="really-in-a-tenant")
        await conn.run_sync(lambda sync: module.repair_synthetic_project_tenants(sync))
        tenant = (
            await conn.execute(text("SELECT tenant_id FROM projects WHERE id = :p"), {"p": tenanted})
        ).scalar_one()
        assert tenant == real_tenant

    async def test_the_repair_deletes_nothing(self, conn: AsyncConnection) -> None:
        module = _migration()
        before = (await conn.execute(text("SELECT count(*) FROM projects"))).scalar_one()
        hidden = await _project(conn)
        await conn.execute(
            text("UPDATE projects SET tenant_id = :t WHERE id = :p"),
            {"t": module.synthetic_project_tenant(hidden), "p": hidden},
        )
        await conn.run_sync(lambda sync: module.repair_synthetic_project_tenants(sync))
        assert (await conn.execute(text("SELECT count(*) FROM projects"))).scalar_one() == before + 1


class TestNoRunStaysRunningForever:
    async def test_the_reaper_closes_orphans_and_leaves_live_runs(self, conn: AsyncConnection) -> None:
        project = await _project(conn)
        orphan = await _run(conn, project, age_seconds=3600)
        live = await _run(conn, project, age_seconds=5)
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")

        closed = await reap_orphaned_runs(session, older_than_seconds=900)

        assert closed >= 1
        rows = {
            row[0]: (row[1], row[2])
            for row in await conn.execute(
                text("SELECT id, status, failure_reason FROM generation_runs WHERE id IN (:a, :b)"),
                {"a": orphan, "b": live},
            )
        }
        assert rows[orphan][0] == "failed" and "Orphaned" in rows[orphan][1]
        assert rows[live] == ("running", None), "a run inside its deadline may still be streaming"

    async def test_an_abandoned_stream_is_recorded_as_failed_with_its_evidence(self, conn: AsyncConnection) -> None:
        project = await _project(conn)
        run_id = await _run(conn, project)
        outcome = GenerationOutcome(run_id=run_id)
        outcome.timings["context_seconds"] = 1.5
        outcome.attempts.append({"attempt": 1, "seconds": 61.0, "verdict": "no_content", "reason": "x"})
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")

        await _abandon_run(
            None, session, run_id=run_id, outcome=outcome, reason="browser closed", started=time.monotonic() - 70
        )

        row = (
            await conn.execute(
                text(
                    "SELECT status, failure_reason, timings, attempts, finished_at FROM generation_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).one()
        assert row[0] == "failed" and row[1] == "browser closed"
        assert row[2]["context_seconds"] == 1.5 and row[2]["total_seconds"] >= 70
        assert row[3][0]["seconds"] == 61.0
        assert row[4] is not None

    async def test_abandoning_never_rewrites_a_finished_run(self, conn: AsyncConnection) -> None:
        project = await _project(conn)
        run_id = await _run(conn, project, status="accepted")
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
        await _abandon_run(
            None,
            session,
            run_id=run_id,
            outcome=GenerationOutcome(run_id=run_id),
            reason="late",
            started=time.monotonic(),
        )
        status = (
            await conn.execute(text("SELECT status FROM generation_runs WHERE id = :id"), {"id": run_id})
        ).scalar_one()
        assert status == "accepted"


class TestProjectsPersist:
    async def test_a_failed_generation_run_does_not_remove_its_project(self, conn: AsyncConnection) -> None:
        project = await _project(conn)
        await _run(conn, project, age_seconds=3600)
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
        await reap_orphaned_runs(session, older_than_seconds=900)
        assert project in await _visible_to_tenantless_user(conn)

    async def test_deleting_one_project_removes_only_that_project(self, conn: AsyncConnection) -> None:
        keep = await _project(conn, name="keep")
        drop = await _project(conn, name="drop")
        kept_run = await _run(conn, keep, status="accepted")
        await _run(conn, drop, status="accepted")

        # The delete route's own statement (projects/routes.py), scoped by id.
        await conn.execute(text("DELETE FROM projects WHERE id = :id"), {"id": drop})

        visible = await _visible_to_tenantless_user(conn)
        assert keep in visible and drop not in visible
        still = (
            await conn.execute(text("SELECT count(*) FROM generation_runs WHERE id = :r"), {"r": kept_run})
        ).scalar_one()
        assert still == 1, "the cascade must reach only the deleted project's rows"

    async def test_one_tenants_scoped_delete_cannot_reach_another_tenants_project(self, conn: AsyncConnection) -> None:
        other_tenant = uuid.uuid4()
        theirs = await _project(conn, tenant=other_tenant, name="theirs")
        # `load_visible_project` + the delete both carry the caller's tenant clause; a tenantless caller's
        # clause is `tenant_id IS NULL`, which cannot match a row in another tenant.
        result = await conn.execute(text("DELETE FROM projects WHERE id = :id AND tenant_id IS NULL"), {"id": theirs})
        assert result.rowcount == 0
        assert (
            await conn.execute(text("SELECT count(*) FROM projects WHERE id = :p"), {"p": theirs})
        ).scalar_one() == 1

    async def test_timings_and_provenance_columns_round_trip(self, conn: AsyncConnection) -> None:
        project = await _project(conn)
        run_id = await _run(conn, project)
        await conn.execute(
            text(
                "UPDATE generation_runs SET requested_endpoint_id = 'gemini-3.8-flash', requested_model = 'gemini-3.8-flash', "
                "timings = CAST(:t AS jsonb) WHERE id = :id"
            ),
            {"id": run_id, "t": json.dumps({"model_seconds": 12.5})},
        )
        row = (
            await conn.execute(
                text("SELECT requested_endpoint_id, requested_model, timings FROM generation_runs WHERE id = :id"),
                {"id": run_id},
            )
        ).one()
        assert row[0] == "gemini-3.8-flash" and row[2]["model_seconds"] == 12.5
