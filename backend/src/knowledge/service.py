# SPDX-License-Identifier: FSL-1.1-ALv2
"""Knowledge Base mode: question answering over this project. Phase 2 §2.14.

WHAT MAKES THIS SAFE, and it is the same shape as the PromQL catalogue's answer for the same reason.

Retrieval reaches the codebase index, deployment history and incidents -- so a badly scoped query could
surface another tenant's content. The scoping is therefore **derived from the verified principal and the path
parameter, never from anything in the request body**: `_authorise_project` reads the project row and refuses
unless the caller's tenant owns it, and every retrieval statement then filters on that project id. There is
no request field naming a tenant, a project filter or a similarity threshold, so a crafted body cannot widen
what is searched.

WHY THE TOPICS ARE ENUMERATED. §2.14 names three -- explain a Dockerfile, explain an error, best practices --
and they are a closed set for the same reason the command intents are: a topic selects WHICH retrieval runs,
and an open topic parameter would mean a caller choosing the retrieval. A question is free text, because a
question has to be; but the question never becomes part of a query, only part of a prompt.

"ALWAYS USES THE CURRENT PROJECT AS EXAMPLE" IS ENFORCED, NOT REQUESTED. The prompt carries this project's
real indexed content and the model is told to answer from it and to say so when it cannot. An answer with no
retrieved context is reported as `no_context` rather than being generated from the model's own general
knowledge -- because a generic answer that looks specific is worse than an admission, and the user cannot tell
the difference from the text.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.model_port import ArtifactModelPort
from ..secrets.redaction import create_redacted_prompt

#: The closed topic set. Each selects a different retrieval; see `retrieve`.
TOPICS: Final[frozenset[str]] = frozenset(
    {
        "explain_artifact",  # "Explain this Dockerfile"
        "explain_error",  # "Explain this error"
        "best_practices",  # "Best practices for..."
        "project_state",  # "What is deployed right now"
    }
)

#: How much retrieved content may reach the prompt. A budget rather than everything, because a prompt stuffed
#: with the whole repository answers worse than one carrying the three relevant files.
CONTEXT_CHAR_BUDGET: Final = 6_000

ANSWER_STATES: Final[tuple[str, ...]] = ("answered", "no_context", "unavailable", "refused")


class KnowledgeRefusedError(Exception):
    """A guard rail refused. Carries the sentence the user reads."""


@dataclass(frozen=True, slots=True)
class ContextChunk:
    """One piece of retrieved evidence, with where it came from.

    `origin` is shown to the user alongside the answer, so a claim can be traced to the file, deployment or
    incident it came from. An answer whose sources are invisible is one nobody can check.
    """

    kind: str
    origin: str
    body: str


@dataclass(frozen=True, slots=True)
class Answer:
    state: str
    text: str = ""
    chunks: list[ContextChunk] = field(default_factory=list)
    model: str = ""
    explanation: str = ""


async def authorise_project(session: AsyncSession, *, project_id: uuid.UUID, tenant_id: uuid.UUID | None) -> None:
    """Refuse unless the caller's tenant owns this project. THE SCOPING BOUNDARY.

    Called before any retrieval, and it reads the project's tenant from the DATABASE rather than trusting
    anything in the request. A caller who knows another tenant's project id gets a refusal rather than that
    project's content -- which is the attack this function exists to stop, and the reason retrieval scoping
    cannot be a filter bolted onto each query.

    An untenanted project is readable by an untenanted principal only. With tenancy deferred that is every
    principal and every project, so the check is a no-op TODAY and load-bearing the moment tenancy lands --
    which is the right time to write it, because retrofitting scoping onto a working retrieval path means
    finding every query.
    """
    row = (
        (await session.execute(text("SELECT tenant_id FROM projects WHERE id = :id"), {"id": project_id}))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise KnowledgeRefusedError("No such project.")
    owner = row["tenant_id"]
    if owner != tenant_id:
        # NON-DISCLOSING, deliberately: the same sentence as a missing project, so this cannot be used to
        # probe which project ids exist in other tenants.
        raise KnowledgeRefusedError("No such project.")


async def retrieve(session: AsyncSession, *, project_id: uuid.UUID, topic: str, question: str) -> list[ContextChunk]:
    """Gather evidence for one topic. Scoped to the project, which `authorise_project` has already checked.

    THE QUESTION NEVER BECOMES PART OF A QUERY. It is used only to pick which indexed paths look relevant, by
    matching against names already in the index -- so the worst a hostile question can do is match nothing.
    Every statement below filters on `:project_id` and takes no other caller-supplied value.
    """
    if topic not in TOPICS:
        raise KnowledgeRefusedError(
            f"{topic!r} is not a topic this system answers. The set is closed because a topic selects which "
            "retrieval runs, and an open topic would let a caller choose that."
        )

    chunks: list[ContextChunk] = []

    if topic in ("explain_artifact", "best_practices"):
        # The project's own files. `question` narrows by matching against paths THE INDEX ALREADY HOLDS -- it
        # is never interpolated into SQL, and a word matching no path simply narrows nothing.
        wanted = _path_hints(question)
        rows = [
            dict(row)
            for row in (
                await session.execute(
                    text(
                        # Joined through `file_tree`, which is where `project_id` lives -- `file_contents`
                        # is keyed by `file_id`. Got this wrong first and the query named a column that
                        # does not exist; the schema is the authority, not the shape one would expect.
                        "SELECT f.path, c.content FROM file_contents c "
                        "JOIN file_tree f ON f.id = c.file_id "
                        "WHERE f.project_id = :project_id "
                        "ORDER BY length(c.content) LIMIT 40"
                    ),
                    {"project_id": project_id},
                )
            ).mappings()
        ]
        for row in rows:
            path = str(row["path"])
            if wanted and not any(hint in path.lower() for hint in wanted):
                continue
            chunks.append(ContextChunk(kind="file", origin=path, body=str(row["content"] or "")[:2000]))

    if topic in ("explain_error", "project_state"):
        incidents = [
            dict(row)
            for row in (
                await session.execute(
                    text(
                        "SELECT i.title, i.source, i.severity, i.detail, "
                        "COALESCE(a.problem, '') AS problem "
                        "FROM incidents i "
                        "LEFT JOIN LATERAL ("
                        "  SELECT problem FROM incident_analyses "
                        "  WHERE incident_id = i.id AND state = 'analysed' "
                        "  ORDER BY created_at DESC LIMIT 1"
                        ") a ON TRUE "
                        "WHERE i.project_id = :project_id "
                        "ORDER BY i.last_seen_at DESC LIMIT 10"
                    ),
                    {"project_id": project_id},
                )
            ).mappings()
        ]
        for row in incidents:
            body = f"{row['severity']}: {row['title']}"
            if row["problem"]:
                body += f"\n  diagnosed cause: {row['problem']}"
            chunks.append(ContextChunk(kind="incident", origin=str(row["source"]), body=body))

    if topic == "project_state":
        deployments = [
            dict(row)
            for row in (
                await session.execute(
                    text(
                        "SELECT d.status, d.healthy, d.stable, e.name AS environment, "
                        "d.created_at::text AS created_at FROM deployments d "
                        "JOIN environments e ON e.id = d.environment_id "
                        "WHERE d.project_id = :project_id ORDER BY d.created_at DESC LIMIT 10"
                    ),
                    {"project_id": project_id},
                )
            ).mappings()
        ]
        for row in deployments:
            # `healthy` is rendered as three states, never two: NULL means nothing checked, which is not the
            # same as unhealthy, and collapsing them here would put a false claim in an answer.
            health = (
                "health not verified" if row["healthy"] is None else ("healthy" if row["healthy"] else "not healthy")
            )
            chunks.append(
                ContextChunk(
                    kind="deployment",
                    origin=f"{row['environment']} at {row['created_at']}",
                    body=f"{row['status']}, {health}",
                )
            )

    return _within_budget(chunks)


def _path_hints(question: str) -> set[str]:
    """Words from the question that could name a file. Used to FILTER retrieved rows, never to query."""
    words = {word for word in re.findall(r"[a-z0-9._/-]{3,}", question.lower())}
    stop = {"the", "this", "that", "what", "why", "how", "does", "explain", "best", "practices", "for"}
    return {word for word in words - stop if len(word) >= 3}


def _within_budget(chunks: list[ContextChunk]) -> list[ContextChunk]:
    """Trim to the budget, keeping the smallest first.

    Smallest first because a 2000-character file crowding out three 200-character ones buys less context, and
    the question is usually answered by several small facts rather than one large file.
    """
    kept: list[ContextChunk] = []
    used = 0
    for chunk in sorted(chunks, key=lambda one: len(one.body)):
        if used + len(chunk.body) > CONTEXT_CHAR_BUDGET:
            continue
        kept.append(chunk)
        used += len(chunk.body)
    return kept


def build_prompt(*, topic: str, question: str, chunks: list[ContextChunk]) -> str:
    """The answering prompt.

    THE MODEL IS TOLD TO ANSWER FROM THE CONTEXT AND TO SAY SO WHEN IT CANNOT. Without that instruction a
    model asked about a Dockerfile it was not shown answers about Dockerfiles in general, which reads as
    specific and is not -- and the user cannot tell from the text.
    """
    lines = [
        "Answer the question about THIS project, using only the context below.",
        "",
        "If the context does not contain the answer, reply with exactly:",
        "### NO CONTEXT",
        "and nothing else. Do not answer from general knowledge: a generic answer that reads as specific to",
        "this project is worse than saying the context does not cover it, because the person asking cannot",
        "tell the difference.",
        "",
        f"TOPIC: {topic}",
        f"QUESTION: {question}",
        "",
        f"CONTEXT FROM THIS PROJECT ({len(chunks)} item(s)):",
    ]
    for chunk in chunks:
        lines.append(f"--- {chunk.kind}: {chunk.origin}")
        lines.append(chunk.body)
    if not chunks:
        lines.append("  (nothing was retrieved)")
    return "\n".join(lines)


async def answer(
    *,
    model: ArtifactModelPort | None,
    topic: str,
    question: str,
    chunks: list[ContextChunk],
) -> Answer:
    """Answer from the retrieved context, or state why there is no answer."""
    if not chunks:
        # REFUSED BEFORE THE MODEL IS ASKED. With no context there is nothing to ground an answer in, and
        # asking anyway would produce exactly the generic-but-specific-looking answer this refuses.
        return Answer(
            state="no_context",
            explanation=(
                "Nothing relevant was found in this project's index, deployments or incidents, so no answer "
                "has been generated. This is a refusal rather than a failure: an answer from general "
                "knowledge would read as if it were about your project, and you could not tell that it was "
                "not. Scanning the project first will usually fix it."
            ),
        )

    if model is None:
        return Answer(
            state="unavailable",
            chunks=chunks,
            explanation=(
                f"No model is configured, so the {len(chunks)} relevant item(s) below were retrieved but not "
                "summarised. They are the evidence an answer would have been built from, and they are "
                "readable directly."
            ),
        )

    prompt = create_redacted_prompt(build_prompt(topic=topic, question=question, chunks=chunks))
    completion = await model.complete(prompt=prompt, store_in_cache=False)
    if not completion.ok or not completion.content:
        return Answer(
            state="unavailable",
            chunks=chunks,
            explanation=(
                "The model could not be reached, so the retrieved context below was not summarised: "
                + "; ".join(completion.failure_reasons[:2])
            ),
        )

    if "### NO CONTEXT" in completion.content:
        return Answer(
            state="no_context",
            chunks=chunks,
            model=completion.endpoint_id or "",
            explanation=(
                "The model read the retrieved context and reported that it does not answer the question. "
                "That is a real answer: the context below is what it had, and you can read it directly."
            ),
        )

    await model.remember(prompt=prompt, content=completion.content)
    return Answer(
        state="answered",
        text=completion.content[:12_000],
        chunks=chunks,
        model=completion.endpoint_id or "",
        explanation=(
            f"Answered from {len(chunks)} item(s) of this project's own content, listed below so any claim "
            "can be traced to what it came from."
        ),
    )
