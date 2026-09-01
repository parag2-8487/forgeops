# SPDX-License-Identifier: FSL-1.1-ALv2
"""FR-13 on the RUNTIME path, not in the module.

WHY THIS FILE EXISTS, AND WHAT IT WOULD HAVE CAUGHT

`retrieval.py` and its twelve tests landed in one commit. That commit touched three files:
`model_prompt.py`, `retrieval.py` and `test_generation_retrieval.py`. **It did not touch `routes.py` or
`service.py`**, so `retrieve_generation_context` had no production caller — and the twelve tests all passed,
because every one of them called the module directly.

That is precisely the defect the whole PRD P0 pass exists to remove: a capability that is present, tested,
and unreachable. It was found by reading `generation_runs.retrieval` after a real end-to-end run and seeing
NULL on every row.

So the assertions here are about the WIRING rather than the retrieval logic:

* `_finish_run`, the production persistence function, writes the record to the column.
* `routes.py` actually calls the retriever and actually passes the result to the service.

The second is a source-level assertion, which is weaker than an execution test and is used deliberately.
The property being protected is "this function has a caller on the request path", and that is a property of
the call graph rather than of any single run — the same shape as `check-chokepoint.sh` and
`check-gate-reachability.py`, which are source-level for the same reason. A run-level test would also pass
against a route that retrieved and then dropped the result.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.generation import routes as generation_routes
from src.generation.retrieval import RetrievalContext, RetrievedChunk
from src.generation.service import GenerationOutcome

ROUTES_SOURCE = pathlib.Path(inspect.getfile(generation_routes)).read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def session(head_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A real session against the migrated database.

    Not the rolled-back ``conn`` fixture: ``_finish_run`` COMMITS, which is the behaviour under test.
    Local rather than from conftest, for the reason ``test_generation_run_rows.py`` gives for its own copy.
    """
    maker = async_sessionmaker(head_engine, expire_on_commit=False, autoflush=False)
    async with maker() as opened:
        yield opened


@pytest_asyncio.fixture
async def project_id(session: AsyncSession) -> AsyncIterator[uuid.UUID]:
    """A real ``projects`` row, because ``generation_runs.project_id`` is a real foreign key."""
    pid = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, :path)"),
        {"id": pid, "name": f"retrieval-wiring-{pid.hex[:8]}", "path": f"/tmp/{pid.hex[:8]}"},
    )
    await session.commit()
    try:
        yield pid
    finally:
        await session.execute(text("DELETE FROM generation_runs WHERE project_id = :id"), {"id": pid})
        await session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": pid})
        await session.commit()


def _stream_call() -> ast.Call:
    """The `service.stream_generation(...)` call in `routes.py`, found in the AST rather than by regex."""
    tree = ast.parse(ROUTES_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "stream_generation":
                return node
    raise AssertionError("routes.py no longer calls service.stream_generation")


class TestTheRouteActuallyRetrieves:
    """The call-graph property that was violated for a whole commit."""

    def test_the_route_calls_the_retriever(self) -> None:
        tree = ast.parse(ROUTES_SOURCE)
        called = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "retrieve_generation_context" in called, (
            "routes.py does not call retrieve_generation_context, so FR-13's retrieval cannot reach a "
            "prompt. This is exactly the state the module shipped in: present, tested, and unreachable."
        )

    def test_the_retrieved_context_is_passed_to_the_service(self) -> None:
        """Retrieving and then dropping the result would satisfy the test above and change nothing."""
        keywords = {kw.arg for kw in _stream_call().keywords}
        assert "retrieval" in keywords, (
            "stream_generation is called without `retrieval=`, so the prompt is built from the project "
            f"row alone. Keywords passed: {sorted(k for k in keywords if k)}"
        )

    def test_the_service_accepts_and_forwards_it(self) -> None:
        """The parameter must exist and reach the prompt builder, not merely be accepted and ignored."""
        from src.generation import service as generation_service

        signature = inspect.signature(generation_service.GenerationService.stream_generation)
        assert "retrieval" in signature.parameters

        source = pathlib.Path(inspect.getfile(generation_service)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "build_generation_prompt"
            ):
                assert "context_lines" in {kw.arg for kw in node.keywords}, (
                    "build_generation_prompt is called without `context_lines`, so the retrieved "
                    "context never reaches the model."
                )
                return
        raise AssertionError("service.py no longer calls build_generation_prompt")

    def test_the_run_row_persists_it(self) -> None:
        """`_finish_run` must write the column, which had never been written since revision 0006."""
        source = inspect.getsource(generation_routes._finish_run)
        assert "retrieval" in source, "_finish_run does not write generation_runs.retrieval"


class TestTheColumnRoundTrips:
    """The persistence half, against the real database."""

    @pytest.mark.asyncio
    async def test_finish_run_stores_the_record_and_it_reads_back(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        run_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO generation_runs (id, project_id, status, iterations_used, "
                "served_from, tier, prompt_tokens, completion_tokens) "
                "VALUES (:id, :project_id, 'running', 0, 'pending', 'template', 0, 0)"
            ),
            {"id": run_id, "project_id": project_id},
        )

        context = RetrievalContext(
            chunks=(
                RetrievedChunk(path="pyproject.toml", content="[project]\nname = 'demo'\n", sources=("manifest",)),
            ),
            retrievers_used=("lexical", "manifest"),
        )
        outcome = GenerationOutcome(run_id=run_id)
        outcome.retrieval = context.as_record()
        outcome.status = "accepted"
        outcome.served_from = "provider"
        outcome.tier = "self_hosted"

        await generation_routes._finish_run(session, run_id=run_id, outcome=outcome)

        stored = (
            await session.execute(text("SELECT retrieval FROM generation_runs WHERE id = :id"), {"id": run_id})
        ).scalar_one()
        assert stored is not None, "the column is still NULL after a run that retrieved"
        assert stored["chunk_count"] == 1
        assert stored["paths"] == ["pyproject.toml"]
        # The record carries paths and counts, not bodies: a prompt's worth of source in a row that is
        # read for provenance would put redacted file content in a second place.
        assert "content" not in str(stored)

    @pytest.mark.asyncio
    async def test_a_run_with_no_retrieval_leaves_the_column_null(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """NULL means retrieval was never attempted, which is not the same as finding nothing."""
        run_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO generation_runs (id, project_id, status, iterations_used, "
                "served_from, tier, prompt_tokens, completion_tokens) "
                "VALUES (:id, :project_id, 'running', 0, 'pending', 'template', 0, 0)"
            ),
            {"id": run_id, "project_id": project_id},
        )
        outcome = GenerationOutcome(run_id=run_id)
        outcome.status = "accepted"
        outcome.served_from = "template"

        await generation_routes._finish_run(session, run_id=run_id, outcome=outcome)

        stored = (
            await session.execute(text("SELECT retrieval FROM generation_runs WHERE id = :id"), {"id": run_id})
        ).scalar_one()
        assert stored is None

    @pytest.mark.asyncio
    async def test_an_empty_retrieval_is_recorded_rather_than_left_null(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """ "We searched and the index held nothing" is a fact worth keeping, and it is not NULL."""
        run_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO generation_runs (id, project_id, status, iterations_used, "
                "served_from, tier, prompt_tokens, completion_tokens) "
                "VALUES (:id, :project_id, 'running', 0, 'pending', 'template', 0, 0)"
            ),
            {"id": run_id, "project_id": project_id},
        )
        outcome = GenerationOutcome(run_id=run_id)
        outcome.retrieval = RetrievalContext(absent_reason="the project has not been scanned").as_record()
        outcome.status = "accepted"
        outcome.served_from = "template"

        await generation_routes._finish_run(session, run_id=run_id, outcome=outcome)

        stored = (
            await session.execute(text("SELECT retrieval FROM generation_runs WHERE id = :id"), {"id": run_id})
        ).scalar_one()
        assert stored is not None
        assert stored["chunk_count"] == 0
        assert stored["absent_reason"] == "the project has not been scanned"
