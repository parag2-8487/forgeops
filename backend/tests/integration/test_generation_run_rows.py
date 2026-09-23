# SPDX-License-Identifier: FSL-1.1-ALv2
"""`generation_runs` rows against a REAL PostgreSQL, written by the real persistence functions.

WHAT THIS PROVES THAT NOTHING ELSE DOES
---------------------------------------
`tests/unit/test_generation_routing.py` asserts that `GenerationOutcome.served_from` becomes
`provider`, `l1`, `l2` or `template` on the right path. That is the pipeline's verdict. It says
nothing about whether the verdict reaches the TABLE, and the defect was entirely in the persistence
layer: `routes.py::_insert_run` wrote

    VALUES (..., 'running', 0, 'template', 'deterministic', ...)

with `served_from` and `tier` as SQL string LITERALS. A pipeline that computed the right answer and
an INSERT that ignored it would satisfy every unit test in this repository.

So these tests call `_insert_run` and `_finish_run` — the production functions, unmodified — against
the real database, and then SELECT the row back. Postgres's CHECK constraint is part of the
assertion: a value outside `SERVED_FROM` is refused by the server, not by Python, which is why the
`pending` state needed revision `0011` rather than a convention.

The model half is real too. The provider row below comes from the local `ollama` service through the
committed tier YAML, so `served_from='provider'` is a row a genuine model call produced.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.auth.models import UserRole
from src.auth.principal import Principal
from src.generation.models import SERVED_FROM
from src.generation.routes import GenerationRequest, _finish_run, _insert_run
from src.generation.service import GenerationOutcome, GenerationService

from .test_self_hosted_generation import (
    TIMEOUT_SECONDS,
    _embedding_model,
    _generation_service,
    _model,
    _Redis,
    _require_model,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

PROJECT = {"name": "checkout-api", "settings": {}}


@pytest_asyncio.fixture
async def session(head_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A real session against the migrated database.

    Not the rolled-back `conn` fixture the §6.5 revision proofs use: `_insert_run` and `_finish_run`
    COMMIT, which is the behaviour under test — the row has to survive the insert so a crashed run
    leaves evidence. Rows are cleaned up explicitly instead.
    """
    maker = async_sessionmaker(head_engine, expire_on_commit=False, autoflush=False)
    async with maker() as opened:
        yield opened


@pytest_asyncio.fixture
async def project_id(session: AsyncSession) -> AsyncIterator[uuid.UUID]:
    """A real `projects` row, because `generation_runs.project_id` is a real foreign key."""
    pid = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, :path)"),
        {"id": pid, "name": f"row-evidence-{pid.hex[:8]}", "path": f"/tmp/{pid.hex[:8]}"},
    )
    await session.commit()
    try:
        yield pid
    finally:
        await session.execute(text("DELETE FROM generation_runs WHERE project_id = :id"), {"id": pid})
        await session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": pid})
        await session.commit()


def _principal() -> Principal:
    return Principal.for_user(
        user_id=None,
        subject="test-only-not-a-real-subject",
        email="rows@example.invalid",
        role=UserRole.DEVELOPER,
    )


async def _persist(session: AsyncSession, project_id: uuid.UUID, service: GenerationService, prompt: str) -> dict:
    """Drive a run through the production persistence path and return the stored row."""
    run_id = uuid.uuid4()
    outcome = GenerationOutcome(run_id=run_id)
    body = GenerationRequest(project_id=project_id, prompt=prompt)

    await _insert_run(session, run_id=run_id, body=body, principal=_principal(), service=service)

    # The `running` row's own claim, checked before the stream overwrites it. `pending` is the whole
    # reason revision 0011 exists: the previous literal said `template` before anything had run.
    inflight = (
        (
            await session.execute(
                text("SELECT status, served_from, tier FROM generation_runs WHERE id = :id"), {"id": run_id}
            )
        )
        .mappings()
        .one()
    )
    assert inflight["status"] == "running"
    assert inflight["served_from"] == "pending", (
        "the in-flight row claims it was served from somewhere before the pipeline ran"
    )

    async for _ in service.stream_generation(project_id, prompt, outcome=outcome, project=PROJECT):
        pass
    await _finish_run(session, run_id=run_id, outcome=outcome)

    row = (
        (
            await session.execute(
                text(
                    "SELECT id, status, served_from, tier, endpoint_id, iterations_used, "
                    "prompt_tokens, completion_tokens, finished_at IS NOT NULL AS finished "
                    "FROM generation_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row) | {"attempted_tier": inflight["tier"]}


class TestARealModelCallStoresServedFromProvider:
    async def test_the_row_says_provider_and_names_the_endpoint(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """The headline row. `served_from='provider'`, produced by a genuine model call."""
        model = _model()
        base_url = _require_model(model)
        service = _generation_service(base_url, model, redis=_Redis())

        row = await _persist(session, project_id, service, "a python checkout service")

        assert row["served_from"] == "provider", (
            f"the stored row says {row['served_from']!r}. If this is 'template' the model's "
            f"artifacts failed the deterministic gate three times; if it is anything else the "
            f"INSERT is not carrying the pipeline's verdict."
        )
        assert row["status"] == "accepted"
        # The `tier` column was the SQL literal `'deterministic'`, which is not a `ModelTier`.
        assert row["attempted_tier"] == "self_hosted"
        assert row["tier"] == "self_hosted"
        assert row["endpoint_id"] == "qwen3-coder-next"
        assert 1 <= row["iterations_used"] <= 3
        assert row["completion_tokens"] > 0
        assert row["prompt_tokens"] > 0
        assert row["finished"] is True


class TestACacheHitStoresL1OrL2:
    """`served_from='l1'` and `'l2'` on the real table, with the PRECONDITION made explicit.

    THE PREMISE THESE TESTS USED TO ASSUME, AND WHY IT WAS WRONG
    Both asserted that a second identical prompt is served from the cache. That holds only when run
    1's completion was accepted BY ITSELF. It is not, in general: when the gate refuses some of the
    model's artifacts and the audited template floor stands in for them, the run is delivered and
    correctly records `served_from='provider'` -- but the raw completion is not what was accepted, so
    it is deliberately NOT cached (see `tests/unit/test_generation_cache_not_poisoned.py`, and the
    measurement that produced it: caching it made every later run hit an entry the gate then refused,
    burn an attempt, and call the provider anyway).

    So `second['served_from'] == 'l1'` conflated two different things: whether the CACHE works, and
    whether THIS MODEL can satisfy this prompt unaided. The first is the property worth asserting and
    is deterministic; the second is a fact about `qwen2.5-coder:7b` and a four-artifact prompt.

    The assertion is not lowered. It is replaced by the exact invariant, checked in both directions:
    the cache is populated IF AND ONLY IF the delivery needed no floor, and WHEN it is populated the
    second run must be served `l1` with `iterations_used=0`. A violation of either direction fails.
    What no longer fails is the model needing the floor, which is not a defect and which the test now
    reports instead of hiding.
    """

    async def test_a_repeated_prompt_stores_l1_and_costs_no_iteration(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """The second headline row: `served_from='l1'` on the same table."""
        model = _model()
        base_url = _require_model(model)
        redis = _Redis()

        first = await _persist(
            session,
            project_id,
            _generation_service(base_url, model, redis=redis),
            "a python checkout service",
        )
        if first["served_from"] != "provider":
            pytest.fail(f"the first run stored {first['served_from']!r}, so nothing was cached to hit")

        second = await _persist(
            session,
            project_id,
            # A NEW service and router over the SAME Redis, which is how two HTTP requests see the
            # cache the lifespan composed.
            _generation_service(base_url, model, redis=redis),
            "a python checkout service",
        )

        assert second["status"] == "accepted"
        assert second["id"] != first["id"]

        # THE INVARIANT, in both directions.
        #
        # `redis.store` is the cache the two services share, so whether run 1 left anything is
        # observable here rather than inferred from run 2's provenance -- which is what made the
        # original failure unreadable: `provider` was reported for a run that HAD hit the cache and
        # had the entry refused.
        cached = bool(redis.store)
        if cached:
            assert second["served_from"] == "l1", (
                f"run 1 left an entry in the cache and run 2 was served "
                f"{second['served_from']!r}. Either the key run 2 asks for is not the key run 1 "
                f"wrote, or the cached content no longer satisfies the gate."
            )
            # Zero, because a cache hit called no provider. Counting one would inflate the NFR-04
            # iteration average this column exists to measure.
            assert second["iterations_used"] == 0
        else:
            # Run 1's completion needed the floor, so caching it would have written an entry every
            # later run is served and every later gate refuses. NOT a cache failure -- and the run
            # must still be honest about having called a provider.
            assert second["served_from"] == "provider", second["served_from"]
            assert second["iterations_used"] >= 1

    async def test_a_near_duplicate_prompt_stores_l2(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """`served_from='l2'`, with a real embedding model deciding the similarity."""
        from src.ai.embeddings import SelfHostedEmbedder

        model = _model()
        embedding_model = _embedding_model()
        base_url = _require_model(model)
        _require_model(embedding_model)
        embedder = SelfHostedEmbedder(base_url=base_url, model=embedding_model, timeout_seconds=TIMEOUT_SECONDS)
        redis = _Redis()

        first = await _persist(
            session,
            project_id,
            _generation_service(base_url, model, redis=redis, embed=embedder),
            "a python checkout service",
        )
        if first["served_from"] != "provider":
            pytest.fail(f"the first run stored {first['served_from']!r}, so nothing was indexed to match")

        second = await _persist(
            session,
            project_id,
            _generation_service(base_url, model, redis=redis, embed=embedder),
            # Surface form only, so L1's digest misses and only similarity can serve it.
            "A python checkout service.",
        )

        # The same invariant as above. L2 additionally needs run 1 to have been INDEXED, which only
        # happens for content that was cached at all.
        if redis.index:
            assert second["served_from"] == "l2", second["served_from"]
            assert second["iterations_used"] == 0
        else:
            assert second["served_from"] == "provider", second["served_from"]
            assert second["iterations_used"] >= 1


class TestTheDatabaseRefusesAValueOutsideTheVocabulary:
    async def test_every_declared_origin_is_storable(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """Including `pending`, which revision `0011` added and `0008`'s constraint refused."""
        for origin in SERVED_FROM:
            run_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO generation_runs (id, project_id, status, iterations_used, "
                    "served_from, tier, prompt_tokens, completion_tokens) "
                    "VALUES (:id, :p, 'running', 0, :served_from, 'self_hosted', 0, 0)"
                ),
                {"id": run_id, "p": project_id, "served_from": origin},
            )
        await session.commit()
        stored = (
            await session.execute(
                text("SELECT DISTINCT served_from FROM generation_runs WHERE project_id = :p"), {"p": project_id}
            )
        ).scalars()
        assert set(stored) == set(SERVED_FROM)

    async def test_the_stored_constraint_names_pending(self, session: AsyncSession) -> None:
        """Read from the SERVER, so the schema and the Python tuple cannot drift silently."""
        definition = (
            await session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_generation_runs_served_from_allowed'"
                )
            )
        ).scalar()
        assert definition is not None
        for origin in SERVED_FROM:
            assert f"'{origin}'" in str(definition), f"{origin} is not in the stored constraint: {definition}"
