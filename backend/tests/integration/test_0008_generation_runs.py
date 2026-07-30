# SPDX-License-Identifier: FSL-1.1-ALv2
"""Revision `0008_generation_runs` against a REAL PostgreSQL.

design.md §6.1, §6.2, §6.5, §11.5, §3.8; Q-08; tasks.md leaf 5.7.

The bound is stated once, in `src.generation.models.MAX_GENERATION_ITERATIONS`, and
this module imports it. The whole argument for a check constraint here is that the
3-iteration limit is expressed three independent times — in the type
(`generation_max_iterations` is `Literal[3]`, so no environment variable can raise
it), in Q-08's termination property, and in the schema. A test that hard-coded `3`
would collapse two of those three into one.

`change_sets.generation_run_id` gets its foreign key here rather than in `0004`,
because `generation_runs` does not exist until this revision. That is proven
behaviourally as well as declaratively: an unenforced column that merely looks like a
reference is how PRD D5's dangling `environment_id` came about in the first place.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from src.generation.models import GENERATION_STATUSES, MAX_GENERATION_ITERATIONS, SERVED_FROM

from .migration_support import column_type, fk_delete_action, make_project, make_user, scalar

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

INSERT_RUN = text(
    "INSERT INTO generation_runs "
    "(id, project_id, requested_by, status, iterations_used, served_from, tier, "
    " endpoint_id, rubric, retrieval, prompt_tokens, completion_tokens) "
    "VALUES (:id, :project_id, :requested_by, :status, :iterations, :served_from, :tier, "
    "        :endpoint_id, CAST(:rubric AS jsonb), CAST(:retrieval AS jsonb), :pt, :ct)"
)


async def _run(
    conn,
    project_id: uuid.UUID,
    *,
    requested_by: uuid.UUID | None = None,
    status: str = "accepted",
    iterations: int = 1,
    served_from: str = "provider",
    rubric: dict | None = None,
    retrieval: dict | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    await conn.execute(
        INSERT_RUN,
        {
            "id": run_id,
            "project_id": project_id,
            "requested_by": requested_by,
            "status": status,
            "iterations": iterations,
            "served_from": served_from,
            "tier": "high_coding",
            "endpoint_id": "openai/gpt-5-codex",
            "rubric": json.dumps(rubric) if rubric is not None else None,
            "retrieval": json.dumps(retrieval) if retrieval is not None else None,
            "pt": 1200,
            "ct": 340,
        },
    )
    return run_id


class TestTheIterationBoundIsInTheSchema:
    @pytest.mark.parametrize("iterations", range(MAX_GENERATION_ITERATIONS + 1))
    async def test_every_permitted_count_is_accepted(self, conn, iterations: int) -> None:
        project_id = await make_project(conn, "gen-ok")
        await _run(conn, project_id, iterations=iterations)

    async def test_one_more_than_the_bound_is_rejected(self, conn) -> None:
        """The leaf's named assertion: `iterations_used = 4` must not be storable."""
        project_id = await make_project(conn, "gen-over")
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _run(conn, project_id, iterations=MAX_GENERATION_ITERATIONS + 1)

    async def test_a_negative_count_is_rejected(self, conn) -> None:
        project_id = await make_project(conn, "gen-negative")
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _run(conn, project_id, iterations=-1)

    async def test_the_constraint_text_names_the_same_bound(self, conn) -> None:
        """Reads the constraint the server actually stored, so the schema and the
        Python constant cannot drift apart silently."""
        definition = await scalar(
            conn,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_generation_runs_iterations_bounded'",
        )
        assert definition is not None
        assert str(MAX_GENERATION_ITERATIONS) in str(definition), definition


class TestTheStatusAndServedFromConstraints:
    @pytest.mark.parametrize("status", GENERATION_STATUSES)
    async def test_every_declared_status_is_accepted(self, conn, status: str) -> None:
        project_id = await make_project(conn, "gen-status")
        await _run(conn, project_id, status=status)

    async def test_an_unknown_status_is_rejected(self, conn) -> None:
        project_id = await make_project(conn, "gen-status-bad")
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _run(conn, project_id, status="pending")

    @pytest.mark.parametrize("served_from", SERVED_FROM)
    async def test_every_declared_cache_origin_is_accepted(self, conn, served_from: str) -> None:
        project_id = await make_project(conn, "gen-served")
        await _run(conn, project_id, served_from=served_from)

    async def test_an_unknown_cache_origin_is_rejected(self, conn) -> None:
        project_id = await make_project(conn, "gen-served-bad")
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _run(conn, project_id, served_from="memory")


class TestTheNfr04EvidenceColumns:
    async def test_the_jsonb_payloads_round_trip(self, conn) -> None:
        project_id = await make_project(conn, "gen-evidence")
        rubric = {"correctness": 4, "idiomatic": 5, "advisory": True}
        retrieval = {
            "chunk_ids": ["c1", "c2"],
            "rerank_scores": [0.81, 0.44],
            "fusion": "rrf",
            "degraded": False,
        }
        run_id = await _run(conn, project_id, rubric=rubric, retrieval=retrieval)
        # asyncpg decodes JSONB to a Python object already, so there is nothing to
        # parse — asserting equality directly is the round trip.
        stored_rubric = await scalar(conn, "SELECT rubric FROM generation_runs WHERE id = :id", id=run_id)
        stored_retrieval = await scalar(conn, "SELECT retrieval FROM generation_runs WHERE id = :id", id=run_id)
        assert stored_rubric == rubric
        assert stored_retrieval == retrieval

    async def test_rubric_and_retrieval_are_jsonb_and_nullable(self, conn) -> None:
        """The rubric is advisory (§11.5.5): the deterministic gate blocks, the rubric
        informs. Nullable is therefore correct — a run with no rubric is not a broken
        run."""
        assert await column_type(conn, "generation_runs", "rubric") == "jsonb"
        assert await column_type(conn, "generation_runs", "retrieval") == "jsonb"
        for column in ("rubric", "retrieval", "endpoint_id"):
            notnull = await scalar(
                conn,
                "SELECT a.attnotnull FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "WHERE c.relname = 'generation_runs' AND a.attname = :column",
                column=column,
            )
            assert notnull is False, column

    async def test_token_counts_are_recorded(self, conn) -> None:
        project_id = await make_project(conn, "gen-tokens")
        run_id = await _run(conn, project_id)
        prompt_tokens = await scalar(conn, "SELECT prompt_tokens FROM generation_runs WHERE id = :id", id=run_id)
        assert prompt_tokens == 1200


class TestTheDeferredForeignKey:
    async def test_the_declared_action_is_set_null(self, conn) -> None:
        assert await fk_delete_action(conn, "fk_change_sets_generation_run_id_generation_runs") == "n"

    async def test_a_change_set_survives_deletion_of_its_generation_run(self, conn) -> None:
        """SET NULL, not CASCADE: the proposal is the durable artifact and the run that
        produced it is provenance. Losing the provenance must not lose the proposal."""
        project_id = await make_project(conn, "gen-fk")
        run_id = await _run(conn, project_id)
        change_set_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO change_sets "
                "(id, project_id, status, origin, blast_radius_score, blast_radius_verdict, "
                " policy_bundle_digest, generation_run_id) "
                "VALUES (:id, :p, 'draft', 'generation', 0, 'low', :digest, :run)"
            ),
            {
                "id": change_set_id,
                "p": project_id,
                "digest": "sha256:" + "0" * 64,
                "run": run_id,
            },
        )
        await conn.execute(text("DELETE FROM generation_runs WHERE id = :id"), {"id": run_id})
        surviving = await scalar(conn, "SELECT count(*) FROM change_sets WHERE id = :id", id=change_set_id)
        assert surviving == 1
        dangling = await scalar(conn, "SELECT generation_run_id FROM change_sets WHERE id = :id", id=change_set_id)
        assert dangling is None

    async def test_an_unknown_generation_run_is_refused(self, conn) -> None:
        """The point of adding the key at all: before `0008` the column accepted any
        UUID, which is a reference in name only."""
        project_id = await make_project(conn, "gen-fk-bad")
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "INSERT INTO change_sets "
                        "(id, project_id, status, origin, blast_radius_score, "
                        " blast_radius_verdict, policy_bundle_digest, generation_run_id) "
                        "VALUES (:id, :p, 'draft', 'generation', 0, 'low', :digest, :run)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "p": project_id,
                        "digest": "sha256:" + "0" * 64,
                        "run": uuid.uuid4(),
                    },
                )

    async def test_requested_by_is_set_null_on_user_deletion(self, conn) -> None:
        assert await fk_delete_action(conn, "fk_generation_runs_requested_by_users") == "n"
        project_id = await make_project(conn, "gen-user")
        user_id = await make_user(conn)
        run_id = await _run(conn, project_id, requested_by=user_id)
        await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        requester = await scalar(conn, "SELECT requested_by FROM generation_runs WHERE id = :id", id=run_id)
        assert requester is None
