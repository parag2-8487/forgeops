# SPDX-License-Identifier: FSL-1.1-ALv2
"""The generation HTTP surface (design.md §1.5, §7.4, §11.5; criterion 10 steps 6-7).

`backend/src/generation/` had twelve modules and no `routes.py`, so the whole pipeline — templates,
renderers, the dry run, the rubric, the feedback loop, the `generation_runs` table from revision
`0008` — was unreachable from a browser. That is what made §12.6 steps 6 and 7 unimplementable and
what the `/generation` panel named as its reason for being blank.

One endpoint, because `GenerationService` exposes one public method. Streaming, because §7.4's whole
event vocabulary exists for this path and because the alternative — a request that blocks for the
length of a generation and returns a blob — is what SSE was specified to avoid.

**It does two things beyond streaming, and both are the point.**

It persists a `GenerationRun` row, so a run leaves evidence with its token counts and `served_from`
rather than existing only as bytes that went down a socket.

And on success it **submits the artifacts to the governance chokepoint** as a change set with
`origin='generation'` and `generation_run_id` set. That is the seam the schema was already built
for: `CHANGE_SET_ORIGINS` contains `generation` and `change_sets.generation_run_id` is a real
column, both unused because nothing generated anything over HTTP. Without it, generation would
produce files that no approval surface could ever show — steps 6 and 7 would pass and step 8 would
have nothing to render.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.sse import SSE_MEDIA_TYPE, SSEEventType, format_event
from ..governance.chokepoint import ChangeItemRequest, GovernanceChokepoint, MutationRequest
from .service import GenerationOutcome, GenerationService

router = APIRouter(
    prefix="/api/v1/generation",
    tags=["generation"],
    dependencies=[Depends(require_principal)],
)


class GenerationRequest(BaseModel):
    """What a caller asks to have generated."""

    project_id: uuid.UUID
    prompt: str = Field(min_length=1, max_length=4000)
    #: Where the change is destined. Passed straight through to the chokepoint, and deliberately
    #: not defaulted here: `approval.rego` answers `require_approval` when the member is absent, so
    #: an omitted environment means a human reviews it rather than it auto-approving (finding 68).
    environment: str | None = Field(default=None, max_length=32)


def _service() -> GenerationService:
    return GenerationService()


def _chokepoint(request: Request) -> GovernanceChokepoint:
    chokepoint = getattr(request.app.state, "governance_chokepoint", None)
    if chokepoint is None:
        # A composition error, not a fact about the caller — the same reasoning
        # `require_principal` applies to a missing verifier.
        raise RuntimeError(
            "app.state.governance_chokepoint is not composed; generation submits its artifacts "
            "through it (design §11.6). create_app() must build it in the lifespan."
        )
    return chokepoint


class GenerationEventEnvelope(BaseModel):
    """One SSE frame this endpoint emits, published so the vocabulary is machine-readable.

    Declared as a response model even though the response is a `StreamingResponse` and no single
    frame is "the" body. The reason is not documentation for its own sake: without a model
    referencing `SSEEventType`, the enum appears nowhere in `openapi.json`, and a client has no
    checkable source for the event names — which is precisely how a producer came to emit
    `run_start`, `token_chunk` and `run_complete` for a whole phase without anything noticing.

    `frontend/__tests__/sse-vocabulary.test.ts` reads this enum out of the generated schema and
    asserts the browser's list is exactly equal to it. That comparison is what makes the two ends
    unable to drift apart silently; a client-side test checking its own list against a copy of
    itself would have been as blind as the backend property was.
    """

    event: SSEEventType = Field(description="One of §7.4's six event types; no others are emitted.")
    data: dict[str, object] = Field(
        description="The JSON payload, encoded on a single line because newlines separate frames."
    )


@router.post(
    "/runs",
    summary="Generate deployment artifacts, streaming progress as SSE",
    response_class=StreamingResponse,
    responses={
        200: {
            "model": GenerationEventEnvelope,
            "content": {SSE_MEDIA_TYPE: {}},
            "description": (
                "An SSE stream of §7.4 events. The schema describes ONE frame, not the whole "
                "body: frames arrive in the order status → token* → validation → complete, or "
                "terminate early with a single error."
            ),
        }
    },
)
async def create_generation_run(
    body: GenerationRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[GenerationService, Depends(_service)],
) -> StreamingResponse:
    """Stream one generation run, then persist it and submit its artifacts for approval.

    The frames are §12.6 step 7's sequence: `status`, one `token` per chunk, `validation`, then
    `complete` — or `error` in place of `complete`, never neither.
    """
    run_id = uuid.uuid4()
    outcome = GenerationOutcome(run_id=run_id)
    chokepoint = _chokepoint(request)

    async def stream() -> AsyncGenerator[bytes]:
        # The row is inserted BEFORE the first frame, as `running`. A row written only on success
        # would leave a crashed run with no trace at all, which is the opposite of what an
        # evidence table is for.
        await _insert_run(session, run_id=run_id, body=body, principal=principal)

        async for frame in service.stream_generation(body.project_id, body.prompt, outcome=outcome):
            yield frame.encode("utf-8")

        await _finish_run(session, run_id=run_id, outcome=outcome)

        if not outcome.validation_passed:
            # The service already emitted a terminal `error`; nothing is submitted for approval
            # because there is nothing that passed the gate.
            return

        try:
            submission = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=body.project_id,
                    items=tuple(
                        # Every artifact is a `create`. `update` would require the pre-image the
                        # agent verifies against, and this endpoint has not read the working tree —
                        # claiming an `old_content` it never saw is precisely the stale-apply hazard
                        # `change_items.old_hash` exists to catch.
                        ChangeItemRequest(file_path=f.path, action="create", new_content=f.content)
                        for f in outcome.files
                    ),
                    reason=f"generated from prompt: {body.prompt[:180]}",
                    origin="generation",
                    generation_run_id=run_id,
                    environment=body.environment,
                ),
                principal=principal,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as a terminal frame
            # A refusal by the chokepoint is a legitimate outcome, not a server fault: a policy
            # deny, a blocked blast radius or a disconnected agent all land here. Reported as an
            # `error` frame so the client learns the reason instead of seeing the stream stop after
            # `complete`.
            yield format_event(
                SSEEventType.ERROR,
                {"run_id": str(run_id), "detail": str(exc), "state": "submission_refused"},
            ).encode("utf-8")
            return

        # A second `status`, not a second `complete`: the run completed above, and this reports what
        # governance did with it. The change-set id is what step 8 opens.
        yield format_event(
            SSEEventType.STATUS,
            {
                "run_id": str(run_id),
                "state": "submitted",
                "change_set_id": str(submission.change_set_id) if submission.change_set_id else None,
                "change_set_status": submission.status,
                "outcome": submission.outcome,
            },
        ).encode("utf-8")

    return StreamingResponse(
        stream(),
        media_type=SSE_MEDIA_TYPE,
        headers={
            # Proxy buffering is what turns a working SSE endpoint into one that delivers
            # everything at once on close, so it is disabled explicitly rather than assumed.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _insert_run(
    session: AsyncSession, *, run_id: uuid.UUID, body: GenerationRequest, principal: Principal
) -> None:
    """Record the attempt as `running`.

    `served_from='template'` is stated rather than inferred: Phase 1's pipeline renders templates,
    and `GENERATION_STATUSES`/`SERVED_FROM` both carry CHECK constraints, so a value outside either
    vocabulary is refused by the database rather than stored and misread later.
    """
    await session.execute(
        text(
            "INSERT INTO generation_runs "
            "(id, project_id, tenant_id, requested_by, status, iterations_used, served_from, tier, "
            " prompt_tokens, completion_tokens) "
            "VALUES (:id, :project_id, :tenant_id, :requested_by, 'running', 0, 'template', "
            " 'deterministic', 0, 0)"
        ),
        {
            "id": run_id,
            "project_id": body.project_id,
            "tenant_id": principal.tenant_id,
            "requested_by": principal.user_id if principal.kind == "user" else None,
        },
    )
    await session.commit()


async def _finish_run(session: AsyncSession, *, run_id: uuid.UUID, outcome: GenerationOutcome) -> None:
    """Close the row out with the real counts and the terminal status."""
    await session.execute(
        text(
            "UPDATE generation_runs SET status = :status, prompt_tokens = :prompt_tokens, "
            "completion_tokens = :completion_tokens, finished_at = now() WHERE id = :id"
        ),
        {
            "id": run_id,
            "status": outcome.status,
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
        },
    )
    await session.commit()
