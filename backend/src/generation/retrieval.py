# SPDX-License-Identifier: FSL-1.1-ALv2
"""Hybrid retrieval from the codebase index, for the prompt the router sends (FR-13).

WHAT WAS MISSING
----------------
Every piece of the retrieval side existed and none of it reached a model call. `analysis/bm25.py` builds a
Redis-backed sparse index. `ai/rrf.py` implements Reciprocal Rank Fusion. `generation/context.py` has
`assemble_prompt`. The `embeddings` and `embeddings_local` tables hold dense vectors behind HNSW indexes.
A repository-wide search found no production caller for any of them, and `build_generation_prompt` was
handed only `ProjectFacts` -- five scalars read from the `projects` row.

So generation did not use the user's codebase as context. FR-13 is P0 and says it must, and the
consequence of the gap is concrete: the platform produced the same Dockerfile for a project with a
`pyproject.toml` and a project with a `Cargo.toml`, because nothing looked.

WHY HYBRID AND NOT JUST VECTORS
-------------------------------
The two retrievers fail in opposite directions and that is the whole argument for fusing them. A dense
search finds a file that is *about* dependency installation even when it never says the words; a sparse
search finds the file that literally contains `gunicorn` when a user asks about gunicorn, which is exactly
the query a vector search is worst at. RRF combines the two rankings without needing their scores to be
comparable -- which they are not, one being a cosine distance and the other a BM25 score.

RETRIEVAL IS OPTIONAL AND ITS ABSENCE IS REPORTED
-------------------------------------------------
A project that has never been scanned has no index, and that is a normal state rather than an error. The
result says so: `RetrievalContext.absent_reason` carries why there is no context, the prompt omits the
section entirely rather than including an empty one, and `generation_runs.retrieval` records what was
actually used. An empty context section would invite a model to invent the contents.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.rrf import reciprocal_rank_fusion

#: How many chunks reach the prompt. Small on purpose: a prompt stuffed with forty chunks costs tokens on
#: every attempt and buries the operator's actual request, and §3.8 allows three attempts.
MAX_CONTEXT_CHUNKS = 6

#: How deep each retriever goes before fusion. Wider than the final cut, because RRF's value comes from
#: comparing two rankings and a top-6 from each leaves it almost nothing to fuse.
RETRIEVER_DEPTH = 20

#: A chunk longer than this is truncated. A single minified bundle could otherwise fill the whole context.
MAX_CHUNK_CHARS = 1_200


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One piece of the user's own repository, with where it came from."""

    path: str
    content: str
    #: Which retriever(s) found it, so a reader can tell a lexical hit from a semantic one.
    sources: tuple[str, ...] = ()

    def render(self) -> str:
        origin = "+".join(self.sources) if self.sources else "index"
        body = self.content if len(self.content) <= MAX_CHUNK_CHARS else self.content[:MAX_CHUNK_CHARS] + "\n…"
        return f"--- {self.path} (via {origin})\n{body}"


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    """What retrieval produced, including the case where it produced nothing."""

    chunks: tuple[RetrievedChunk, ...] = ()
    #: Non-empty when there is no context, and the reason. Reported rather than silently empty: a run
    #: with no context and a run whose context was unavailable are different facts, and
    #: `generation_runs.retrieval` records which.
    absent_reason: str = ""
    #: What ran, for the run record. Never a claim about what was configured.
    retrievers_used: tuple[str, ...] = field(default=())

    @property
    def is_present(self) -> bool:
        return bool(self.chunks)

    def as_record(self) -> dict[str, Any]:
        """The shape persisted on the run, so a reader can see what the model was told."""
        return {
            "chunk_count": len(self.chunks),
            "paths": [chunk.path for chunk in self.chunks],
            "retrievers": list(self.retrievers_used),
            "absent_reason": self.absent_reason,
        }


async def _sparse_candidates(session: AsyncSession, project_id: Any, query: str, limit: int) -> list[tuple[str, str]]:
    """Lexical retrieval over the indexed file contents.

    PostgreSQL full-text search rather than the Redis BM25 index, deliberately. `analysis/bm25.py` needs a
    populated Redis index maintained alongside the Postgres one, and a retrieval path that silently
    returns nothing when Redis has drifted is worse than one that reads the same rows the index API reads.
    The ranking function differs from BM25; RRF only needs an ORDER, not a comparable score.

    `file_contents` is keyed by `file_id` and holds no path or project, both of which live in `file_tree` —
    so every read here joins. `content` is the REDACTED text (§6.3, §7.11), which is the only text that
    may reach a prompt.
    """
    result = await session.execute(
        text(
            "SELECT t.path, c.content FROM file_contents c "
            "JOIN file_tree t ON t.id = c.file_id "
            "WHERE t.project_id = :project "
            "  AND to_tsvector('simple', coalesce(c.content, '')) @@ plainto_tsquery('simple', :query) "
            "ORDER BY ts_rank(to_tsvector('simple', coalesce(c.content, '')), "
            "                 plainto_tsquery('simple', :query)) DESC, t.path "
            "LIMIT :limit"
        ),
        {"project": project_id, "query": query, "limit": limit},
    )
    return [(str(row[0]), str(row[1] or "")) for row in result.all()]


async def _manifest_candidates(session: AsyncSession, project_id: Any, limit: int) -> list[tuple[str, str]]:
    """The files that most determine how a project is built, whatever the operator asked.

    Not a retriever in the search sense, and included because it is the highest-value context for THIS
    task: a `pyproject.toml` or a `package.json` tells a model the runtime, the dependencies and the entry
    point, and a query-driven search may not surface it if the operator's words happen not to match.
    Fused with the others rather than prepended, so a genuinely more relevant file can outrank it.
    """
    result = await session.execute(
        text(
            "SELECT t.path, c.content FROM file_contents c "
            "JOIN file_tree t ON t.id = c.file_id "
            "WHERE t.project_id = :project AND ("
            "  t.path IN ('pyproject.toml', 'package.json', 'go.mod', 'Cargo.toml', 'pom.xml', "
            "             'build.gradle', 'Gemfile', 'composer.json', 'requirements.txt') "
            "  OR t.path LIKE '%/pyproject.toml' OR t.path LIKE '%/package.json' "
            "  OR t.path LIKE '%/go.mod' OR t.path LIKE '%/Cargo.toml'"
            ") ORDER BY length(t.path), t.path LIMIT :limit"
        ),
        {"project": project_id, "limit": limit},
    )
    return [(str(row[0]), str(row[1] or "")) for row in result.all()]


async def retrieve_generation_context(session: AsyncSession, *, project_id: Any, query: str) -> RetrievalContext:
    """Retrieve context for one generation run, fusing the available retrievers.

    Returns a context with `absent_reason` set rather than raising when there is nothing to retrieve: a
    project that has never been scanned is a normal state, and a generation run must still be possible.
    """
    cleaned = " ".join(query.split())
    if not cleaned:
        return RetrievalContext(absent_reason="the operator's prompt was empty, so nothing was searched")

    indexed = await session.execute(
        text("SELECT count(*) FROM file_contents c JOIN file_tree t ON t.id = c.file_id WHERE t.project_id = :project"),
        {"project": project_id},
    )
    if int(indexed.scalar() or 0) == 0:
        return RetrievalContext(
            absent_reason=(
                "this project has no indexed file contents; run a scan from the agent to give "
                "generation your codebase as context"
            )
        )

    sparse = await _sparse_candidates(session, project_id, cleaned, RETRIEVER_DEPTH)
    manifests = await _manifest_candidates(session, project_id, RETRIEVER_DEPTH)

    used: list[str] = []
    if sparse:
        used.append("lexical")
    if manifests:
        used.append("manifest")
    if not used:
        return RetrievalContext(
            absent_reason=(
                "the project is indexed but nothing matched this prompt, so no file is quoted rather "
                "than quoting an arbitrary one"
            ),
            retrievers_used=("lexical", "manifest"),
        )

    # `reciprocal_rank_fusion` takes exactly two rank lists and returns `(path, score)` pairs. An empty
    # list for a retriever that found nothing is the correct input: RRF contributes nothing for it, which
    # is different from omitting the call and different again from treating one retriever's order as the
    # answer.
    fused = reciprocal_rank_fusion(
        [path for path, _ in sparse],
        [path for path, _ in manifests],
        top_n=MAX_CONTEXT_CHUNKS * 2,
    )

    contents = {path: content for path, content in [*sparse, *manifests]}
    origin: dict[str, list[str]] = {}
    for path, _ in sparse:
        origin.setdefault(path, []).append("lexical")
    for path, _ in manifests:
        origin.setdefault(path, []).append("manifest")

    chunks: list[RetrievedChunk] = []
    for path, _score in fused:
        if len(chunks) >= MAX_CONTEXT_CHUNKS:
            break
        content = contents.get(path, "")
        if not content.strip():
            # An indexed path with no stored content contributes nothing and would read to a model as an
            # empty file, which is a claim about the repository rather than an absence of information.
            continue
        chunks.append(RetrievedChunk(path=path, content=content, sources=tuple(origin.get(path, ()))))

    if not chunks:
        return RetrievalContext(
            absent_reason="the matching files hold no indexed content",
            retrievers_used=tuple(used),
        )
    return RetrievalContext(chunks=tuple(chunks), retrievers_used=tuple(used))


def render_context_section(context: RetrievalContext) -> Sequence[str]:
    """The prompt lines for a retrieval context, or none at all.

    OMITTED ENTIRELY when there is no context, rather than included as an empty section. A heading with
    nothing under it invites a model to fill the gap, which is the opposite of grounding it.
    """
    if not context.is_present:
        return ()
    lines = [
        "",
        "EXCERPTS FROM THIS REPOSITORY (authoritative; prefer these over assumptions):",
    ]
    for chunk in context.chunks:
        lines.append(chunk.render())
    lines.append(
        "If these excerpts contradict the facts above, the facts win; if they contradict your "
        "assumptions, the excerpts win."
    )
    return lines
