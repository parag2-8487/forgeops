# SPDX-License-Identifier: FSL-1.1-ALv2
"""Knowledge Base routes. Phase 2 2.14.

ONE ROUTE, AND THE SCOPING IS ITS FIRST ACTION. `authorise_project` runs before any retrieval and reads the
project's owner from the database rather than trusting the request -- so a caller who knows another tenant's
project id gets the same non-disclosing refusal as one naming a project that does not exist.

`topic` is a closed set and `question` is free text that never becomes part of a query. That asymmetry is
deliberate: a question has to be free text, and a topic decides which retrieval runs, so only one of them can
safely be open.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import problem
from .service import (
    TOPICS,
    KnowledgeRefusedError,
    answer,
    authorise_project,
    retrieve,
)

router = APIRouter(tags=["knowledge"])


class AskRequest(BaseModel):
    """A question about one project.

    NOTE WHAT IS ABSENT: no tenant, no project filter, no similarity threshold, no retrieval limit. Scoping
    comes from the path parameter and the verified principal, so no field here can widen what is searched.
    """

    model_config = {"extra": "forbid"}

    topic: str
    question: str = Field(min_length=3, max_length=2000)


@router.get("/knowledge/topics")
async def list_topics(
    principal: Annotated[Principal, Depends(require_principal)],
) -> dict[str, Any]:
    """The closed topic set, so what can be asked is inspectable."""
    return {
        "topics": sorted(TOPICS),
        "explanation": (
            "Each topic selects which of this project's own content is searched. The set is closed because "
            "an open topic would let a caller choose the retrieval; the question itself is free text and "
            "never becomes part of a query."
        ),
    }


@router.post("/projects/{project_id}/knowledge/ask")
async def ask(
    project_id: uuid.UUID,
    body: AskRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Answer from this project's own content, or say why it cannot."""
    try:
        # FIRST, before any retrieval. Scoping is not a filter bolted onto each query.
        await authorise_project(session, project_id=project_id, tenant_id=principal.tenant_id)
        chunks = await retrieve(session, project_id=project_id, topic=body.topic, question=body.question)
    except KnowledgeRefusedError as exc:
        raise problem("knowledge-refused", detail=str(exc)) from exc

    result = await answer(
        model=getattr(request.app.state, "artifact_model", None),
        topic=body.topic,
        question=body.question,
        chunks=chunks,
    )
    return {
        "state": result.state,
        "answer": result.text,
        "model": result.model,
        # The sources, so any claim can be traced to the file, deployment or incident it came from. An answer
        # whose sources are invisible is one nobody can check.
        "sources": [
            {"kind": chunk.kind, "origin": chunk.origin, "excerpt": chunk.body[:400]} for chunk in result.chunks
        ],
        "explanation": result.explanation,
    }
