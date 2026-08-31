# SPDX-License-Identifier: FSL-1.1-ALv2
"""Generation must use the user's own codebase as context (FR-13).

Every piece of the retrieval side existed and none of it reached a model call. `analysis/bm25.py` builds a
sparse index, `ai/rrf.py` implements Reciprocal Rank Fusion, `generation/context.py` has `assemble_prompt`,
and the `embeddings` tables hold dense vectors behind HNSW indexes. A repository-wide search found no
production caller for any of them, and `build_generation_prompt` was handed five scalars from the
`projects` row. So the platform produced the same artifacts for a project with a `pyproject.toml` as for
one with a `Cargo.toml`, because nothing looked.

What is asserted here, in order of what would actually break:

* a scanned project's files reach the retrieval result;
* those files reach the PROMPT the provider is handed -- a retrieval result nobody sends is the defect
  being fixed, not evidence against it;
* the prompt carries something only the repository could have said;
* an unscanned project produces no context section rather than an empty one, and says why;
* the run records what retrieval supplied.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.generation.model_prompt import ProjectFacts, build_generation_prompt
from src.generation.retrieval import (
    MAX_CONTEXT_CHUNKS,
    RetrievalContext,
    RetrievedChunk,
    render_context_section,
    retrieve_generation_context,
)

FACTS = ProjectFacts(
    app_name="billing",
    runtime="python",
    port=8000,
    base_image="python:3.12-slim",
    start_command=("python", "main.py"),
)

#: A dependency no template would guess and no prompt mentions. If it reaches the model prompt, it can
#: only have come from the indexed repository -- which is the whole claim of FR-13.
DISTINCTIVE = "granian"
PYPROJECT = f"""[project]
name = "billing"
dependencies = ["fastapi", "{DISTINCTIVE}==1.6.0", "sqlmodel"]

[project.scripts]
serve = "billing.__main__:main"
"""


async def _index_file(session: AsyncSession, project_id: uuid.UUID, path: str, content: str) -> None:
    """Write the two rows a scan writes: `file_tree` for the path, `file_contents` for the text."""
    file_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_tree (id, project_id, path, content_hash, size_bytes, last_modified, "
            "created_at) VALUES (:id, :project, :path, :hash, :size, now(), now())"
        ),
        {
            "id": file_id,
            "project": project_id,
            "path": path,
            "hash": f"{abs(hash(content)):064x}"[:64],
            "size": len(content),
        },
    )
    await session.execute(
        text(
            "INSERT INTO file_contents (file_id, content, language, redaction_count, updated_at) "
            "VALUES (:file_id, :content, 'toml', 0, now())"
        ),
        {"file_id": file_id, "content": content},
    )


async def _new_project(session: AsyncSession) -> uuid.UUID:
    project_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, '/tmp/rag')"),
        {"id": project_id, "name": f"rag-{project_id.hex[:8]}"},
    )
    return project_id


# ── the render, as a pure function ───────────────────────────────────────────────────────────────


def test_an_absent_context_renders_no_section_at_all() -> None:
    """Not an empty heading. A heading with nothing under it invites a model to fill the gap."""
    assert render_context_section(RetrievalContext(absent_reason="never scanned")) == ()


def test_a_present_context_renders_the_paths_and_the_content() -> None:
    context = RetrievalContext(
        chunks=(RetrievedChunk(path="pyproject.toml", content=PYPROJECT, sources=("manifest",)),)
    )
    rendered = "\n".join(render_context_section(context))
    assert "pyproject.toml" in rendered
    assert DISTINCTIVE in rendered
    # The origin travels, so a reader can tell a lexical hit from a manifest one.
    assert "manifest" in rendered


def test_the_context_reaches_the_prompt_the_provider_is_handed() -> None:
    """A retrieval result nobody sends is the defect being fixed, not evidence against it."""
    context = RetrievalContext(
        chunks=(RetrievedChunk(path="pyproject.toml", content=PYPROJECT, sources=("manifest",)),)
    )
    prompt = build_generation_prompt(
        operator_prompt="make this deployable",
        facts=FACTS,
        context_lines=render_context_section(context),
    )
    assert DISTINCTIVE in prompt, "the excerpt never reached the prompt"
    assert "pyproject.toml" in prompt
    # And the facts still win over the excerpts, which the section states explicitly.
    assert "the facts win" in prompt


def test_a_prompt_without_context_is_unchanged_in_shape() -> None:
    """An unscanned project must still produce a usable prompt."""
    prompt = build_generation_prompt(operator_prompt="make this deployable", facts=FACTS)
    assert "EXCERPTS FROM THIS REPOSITORY" not in prompt
    assert "APPLICATION FACTS" in prompt


def test_a_long_chunk_is_truncated_rather_than_filling_the_context() -> None:
    """One minified bundle must not be able to consume the whole prompt."""
    chunk = RetrievedChunk(path="bundle.js", content="x" * 50_000)
    rendered = chunk.render()
    assert len(rendered) < 5_000, len(rendered)
    assert rendered.endswith("…")


def test_the_record_shape_says_what_was_used() -> None:
    context = RetrievalContext(
        chunks=(RetrievedChunk(path="pyproject.toml", content=PYPROJECT),),
        retrievers_used=("lexical", "manifest"),
    )
    record = context.as_record()
    assert record["chunk_count"] == 1
    assert record["paths"] == ["pyproject.toml"]
    assert record["retrievers"] == ["lexical", "manifest"]
    assert record["absent_reason"] == ""
    # The record must be JSON-serialisable, because it is stored in a jsonb column.
    json.dumps(record)


# ── retrieval, against a real database ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unscanned_project_gets_no_context_and_a_reason(sessions: Any) -> None:
    """A normal state, not an error. The reason has to be actionable."""
    async with sessions() as session:
        project_id = await _new_project(session)
        await session.flush()
        context = await retrieve_generation_context(session, project_id=project_id, query="make this deployable")
        assert not context.is_present
        assert "scan" in context.absent_reason, context.absent_reason
        assert render_context_section(context) == ()
        await session.rollback()


@pytest.mark.asyncio
async def test_a_scanned_projects_manifest_is_retrieved(sessions: Any) -> None:
    """The core of FR-13: the model is told something only this repository could say."""
    async with sessions() as session:
        project_id = await _new_project(session)
        await _index_file(session, project_id, "pyproject.toml", PYPROJECT)
        await _index_file(session, project_id, "src/billing/__main__.py", "def main():\n    pass\n")
        await session.flush()

        context = await retrieve_generation_context(
            session, project_id=project_id, query="containerise this python service"
        )
        assert context.is_present, context.absent_reason
        assert "pyproject.toml" in [chunk.path for chunk in context.chunks]

        prompt = build_generation_prompt(
            operator_prompt="containerise this python service",
            facts=FACTS,
            context_lines=render_context_section(context),
        )
        # The assertion FR-13 is about. `granian` appears in no template, no fact and no prompt -- only in
        # the indexed repository.
        assert DISTINCTIVE in prompt, "the prompt reflects nothing that came from the repository"
        await session.rollback()


@pytest.mark.asyncio
async def test_another_projects_files_are_never_retrieved(sessions: Any) -> None:
    """Scoping, in the direction that matters."""
    async with sessions() as session:
        mine = await _new_project(session)
        theirs = await _new_project(session)
        await _index_file(session, theirs, "pyproject.toml", PYPROJECT)
        await session.flush()

        context = await retrieve_generation_context(session, project_id=mine, query="python service")
        assert not context.is_present, "another project's files were retrieved"
        await session.rollback()


@pytest.mark.asyncio
async def test_the_context_is_bounded(sessions: Any) -> None:
    """A prompt stuffed with forty chunks costs tokens on every attempt and buries the request."""
    async with sessions() as session:
        project_id = await _new_project(session)
        for index in range(MAX_CONTEXT_CHUNKS + 8):
            await _index_file(session, project_id, f"pkg{index}/package.json", '{"name": "thing", "deps": {}}')
        await session.flush()

        context = await retrieve_generation_context(session, project_id=project_id, query="package")
        assert context.is_present
        assert len(context.chunks) <= MAX_CONTEXT_CHUNKS, len(context.chunks)
        await session.rollback()


@pytest.mark.asyncio
async def test_an_empty_prompt_searches_nothing(sessions: Any) -> None:
    async with sessions() as session:
        project_id = await _new_project(session)
        await _index_file(session, project_id, "pyproject.toml", PYPROJECT)
        await session.flush()
        context = await retrieve_generation_context(session, project_id=project_id, query="   ")
        assert not context.is_present
        assert "empty" in context.absent_reason
        await session.rollback()


@pytest.mark.asyncio
async def test_a_file_with_no_stored_content_is_not_quoted_as_empty(sessions: Any) -> None:
    """An indexed path with no content would read to a model as an empty file, which is a claim."""
    async with sessions() as session:
        project_id = await _new_project(session)
        await _index_file(session, project_id, "package.json", "   ")
        await session.flush()
        context = await retrieve_generation_context(session, project_id=project_id, query="package")
        assert all(chunk.content.strip() for chunk in context.chunks)
        await session.rollback()
