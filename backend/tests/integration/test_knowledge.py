"""Knowledge Base mode. Phase 2 §2.14.

THE CROSS-TENANT TEST IS WHY THIS FILE EXISTS. Retrieval reaches the codebase index, deployment history and
incidents, so a badly scoped query surfaces another tenant's content -- and unlike most bugs it does so
silently and correctly-looking. So the first class below drives a real second tenant with real content and
asserts none of it is reachable, including when the caller knows the other project's id exactly.

The second concern is fabrication: an answer generated from the model's general knowledge reads as if it were
about this project, and the user cannot tell. So `no_context` refuses BEFORE the model is asked.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.auth.dependencies import require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal
from src.core.db import get_session
from src.core.errors import install_problem_handlers
from src.knowledge.routes import router
from src.knowledge.service import (
    CONTEXT_CHAR_BUDGET,
    TOPICS,
    ContextChunk,
    KnowledgeRefusedError,
    answer,
    authorise_project,
    build_prompt,
    retrieve,
)

TENANT_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TENANT_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

SECRET_OF_B = "TENANT-B-ONLY-CONTENT-DO-NOT-LEAK"


@pytest_asyncio.fixture
async def session(head_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(head_engine, expire_on_commit=False, autoflush=False)
    async with maker() as opened:
        yield opened


@pytest_asyncio.fixture
async def two_tenants(session: AsyncSession) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Two projects owned by two tenants, each with real indexed content."""
    project_a = uuid.uuid4()
    project_b = uuid.uuid4()
    for pid, tenant, body in (
        (project_a, TENANT_A, "FROM python:3.13-slim\nUSER 10001\n"),
        (project_b, TENANT_B, f"FROM python:3.13-slim\n# {SECRET_OF_B}\n"),
    ):
        await session.execute(
            text("INSERT INTO projects (id, name, path, tenant_id) VALUES (:id, :name, :path, :tenant)"),
            {"id": pid, "name": f"kb-{pid.hex[:8]}", "path": f"/tmp/{pid.hex[:8]}", "tenant": tenant},
        )
        file_id = uuid.uuid4()
        await session.execute(
            text(
                # The real columns: `file_tree` has no `kind`, and `content_hash`, `size_bytes` and
                # `last_modified` are all NOT NULL. Read off the schema rather than assumed.
                "INSERT INTO file_tree "
                "(id, project_id, path, content_hash, size_bytes, last_modified, created_at) "
                "VALUES (:id, :p, 'Dockerfile', :hash, :size, now(), now())"
            ),
            {
                "id": file_id,
                "p": pid,
                "hash": f"sha256:{pid.hex}",
                "size": len(body),
            },
        )
        await session.execute(
            text(
                "INSERT INTO file_contents (file_id, content, language, redaction_count, updated_at) "
                "VALUES (:fid, :content, 'dockerfile', 0, now())"
            ),
            {"fid": file_id, "content": body},
        )
    await session.commit()
    try:
        yield project_a, project_b
    finally:
        for pid in (project_a, project_b):
            await session.execute(
                text("DELETE FROM file_contents WHERE file_id IN (SELECT id FROM file_tree WHERE project_id = :p)"),
                {"p": pid},
            )
            await session.execute(text("DELETE FROM file_tree WHERE project_id = :p"), {"p": pid})
            await session.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        await session.commit()


class _Completion:
    def __init__(self, *, ok: bool = True, content: str | None = None) -> None:
        self.ok = ok
        self.content = content
        self.failure_reasons = ("unreachable",)
        self.served_from = "provider"
        self.endpoint_id = "ollama-primary"


class _Model:
    def __init__(self, completion: _Completion) -> None:
        self._completion = completion
        self.prompts: list[str] = []
        self.remembered: list[str] = []

    async def complete(self, *, prompt, on_token=None, may_serve_from_cache=True, store_in_cache=True):
        self.prompts.append(str(prompt))
        return self._completion

    async def remember(self, *, prompt, content: str) -> None:
        self.remembered.append(content)


def _app(session: AsyncSession, *, tenant: uuid.UUID | None, model: object | None = None) -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(router)
    app.state.artifact_model = model
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_principal] = lambda: Principal.for_user(
        user_id=uuid.uuid4(),
        subject="s",
        email="o@e.test",
        role=UserRole.ADMIN,
        tenant_id=tenant,
    )
    return app


class TestRetrievalCannotEscapeItsTenant:
    """The reason this file exists. A leak here is silent and looks correct."""

    @pytest.mark.asyncio
    async def test_a_caller_knowing_the_other_projects_id_is_refused(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        _, project_b = two_tenants
        app = _app(session, tenant=TENANT_A)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/projects/{project_b}/knowledge/ask",
                json={"topic": "explain_artifact", "question": "explain this dockerfile"},
            )
        assert response.status_code == 404
        # The other tenant's content appears nowhere, and neither does a hint that the project exists.
        assert SECRET_OF_B not in response.text
        assert "No such project" in response.text

    @pytest.mark.asyncio
    async def test_the_refusal_is_the_same_sentence_as_a_missing_project(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A distinct message would let a caller probe which ids exist in other tenants."""
        _, project_b = two_tenants
        app = _app(session, tenant=TENANT_A)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            other_tenant = await client.post(
                f"/projects/{project_b}/knowledge/ask",
                json={"topic": "explain_artifact", "question": "explain this dockerfile"},
            )
            nonexistent = await client.post(
                f"/projects/{uuid.uuid4()}/knowledge/ask",
                json={"topic": "explain_artifact", "question": "explain this dockerfile"},
            )
        assert other_tenant.status_code == nonexistent.status_code
        assert other_tenant.json()["detail"] == nonexistent.json()["detail"]

    @pytest.mark.asyncio
    async def test_the_owner_can_read_its_own_project(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The negative control: without this, the isolation assertions could pass on a route that refuses
        everyone."""
        _, project_b = two_tenants
        app = _app(session, tenant=TENANT_B)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/projects/{project_b}/knowledge/ask",
                json={"topic": "explain_artifact", "question": "explain this dockerfile"},
            )
        assert response.status_code == 200
        assert SECRET_OF_B in response.text

    @pytest.mark.asyncio
    async def test_retrieval_returns_only_the_named_projects_files(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """At the retrieval layer, below the route, so the scoping is not only in the authorisation check."""
        project_a, _ = two_tenants
        chunks = await retrieve(session, project_id=project_a, topic="explain_artifact", question="dockerfile")
        assert chunks
        for chunk in chunks:
            assert SECRET_OF_B not in chunk.body

    @pytest.mark.asyncio
    async def test_authorise_refuses_a_mismatched_tenant_directly(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        _, project_b = two_tenants
        with pytest.raises(KnowledgeRefusedError, match="No such project"):
            await authorise_project(session, project_id=project_b, tenant_id=TENANT_A)
        # And the owner passes.
        await authorise_project(session, project_id=project_b, tenant_id=TENANT_B)

    @pytest.mark.asyncio
    async def test_there_is_no_request_field_that_could_widen_the_search(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """`extra: forbid` plus no tenant/filter/threshold field: widening is unexpressible."""
        project_a, _ = two_tenants
        app = _app(session, tenant=TENANT_A)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            for extra in ({"tenant_id": str(TENANT_B)}, {"project_filter": "*"}, {"limit": 9999}):
                response = await client.post(
                    f"/projects/{project_a}/knowledge/ask",
                    json={"topic": "explain_artifact", "question": "explain this", **extra},
                )
                assert response.status_code == 422, extra


class TestTheTopicSetIsClosed:
    @pytest.mark.asyncio
    async def test_an_unknown_topic_is_refused(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        project_a, _ = two_tenants
        with pytest.raises(KnowledgeRefusedError, match="not a topic"):
            await retrieve(session, project_id=project_a, topic="anything", question="q")

    @pytest.mark.asyncio
    async def test_the_topics_route_publishes_the_set(self, session: AsyncSession) -> None:
        app = _app(session, tenant=TENANT_A)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/knowledge/topics")
        body = response.json()
        assert set(body["topics"]) == TOPICS
        assert "never becomes part of a query" in body["explanation"]

    @pytest.mark.asyncio
    async def test_the_named_topics_from_the_box_all_exist(self, session: AsyncSession) -> None:
        """§2.14 names three: explain a Dockerfile, explain an error, best practices."""
        assert "explain_artifact" in TOPICS
        assert "explain_error" in TOPICS
        assert "best_practices" in TOPICS


class TestItAlwaysUsesThisProjectAndNeverInventsOne:
    @pytest.mark.asyncio
    async def test_with_no_context_the_model_is_not_even_asked(self) -> None:
        """A generic answer that reads as specific is worse than an admission, and indistinguishable."""
        model = _Model(_Completion(content="Dockerfiles generally should use a small base image."))
        result = await answer(model=model, topic="explain_artifact", question="q", chunks=[])
        assert result.state == "no_context"
        assert result.text == ""
        assert model.prompts == []
        assert "could not tell that it was not" in result.explanation

    @pytest.mark.asyncio
    async def test_the_prompt_forbids_answering_from_general_knowledge(self) -> None:
        prompt = build_prompt(
            topic="explain_artifact",
            question="explain this dockerfile",
            chunks=[ContextChunk(kind="file", origin="Dockerfile", body="FROM python:3.13-slim")],
        )
        assert "### NO CONTEXT" in prompt
        assert "Do not answer from general knowledge" in prompt
        assert "FROM python:3.13-slim" in prompt

    @pytest.mark.asyncio
    async def test_a_model_saying_the_context_does_not_cover_it_is_a_real_answer(self) -> None:
        model = _Model(_Completion(content="### NO CONTEXT"))
        result = await answer(
            model=model,
            topic="explain_error",
            question="q",
            chunks=[ContextChunk(kind="incident", origin="x", body="y")],
        )
        assert result.state == "no_context"
        assert "you can read it directly" in result.explanation
        assert model.remembered == []

    @pytest.mark.asyncio
    async def test_with_no_model_the_retrieved_context_is_still_returned(self) -> None:
        chunks = [ContextChunk(kind="file", origin="Dockerfile", body="FROM x")]
        result = await answer(model=None, topic="explain_artifact", question="q", chunks=chunks)
        assert result.state == "unavailable"
        assert result.chunks == chunks
        assert "readable directly" in result.explanation

    @pytest.mark.asyncio
    async def test_an_answer_carries_its_sources(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """An answer whose sources are invisible is one nobody can check."""
        project_a, _ = two_tenants
        app = _app(
            session,
            tenant=TENANT_A,
            model=_Model(_Completion(content="It pins its base image and runs as uid 10001.")),
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/projects/{project_a}/knowledge/ask",
                json={"topic": "explain_artifact", "question": "explain this dockerfile"},
            )
        body = response.json()
        assert body["state"] == "answered"
        assert body["sources"]
        assert body["sources"][0]["origin"] == "Dockerfile"
        assert "can be traced to what it came from" in body["explanation"]


class TestRetrievalIsBounded:
    @pytest.mark.asyncio
    async def test_the_context_budget_is_respected(self) -> None:
        from src.knowledge.service import _within_budget

        chunks = [ContextChunk(kind="file", origin=f"f{index}", body="x" * 2000) for index in range(10)]
        kept = _within_budget(chunks)
        assert sum(len(chunk.body) for chunk in kept) <= CONTEXT_CHAR_BUDGET
        assert len(kept) < len(chunks)

    @pytest.mark.asyncio
    async def test_a_hostile_question_matches_nothing_rather_than_everything(
        self, session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The question filters retrieved rows; it is never interpolated into SQL."""
        project_a, _ = two_tenants
        chunks = await retrieve(
            session,
            project_id=project_a,
            topic="explain_artifact",
            question="'; DROP TABLE projects; --",
        )
        # Either nothing matched, or only this project's files did. Nothing raised, and the table survives.
        for chunk in chunks:
            assert SECRET_OF_B not in chunk.body
        survived = (
            await session.execute(text("SELECT count(*) FROM projects WHERE id = :p"), {"p": project_a})
        ).scalar_one()
        assert survived == 1
