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
from collections.abc import AsyncGenerator, Mapping
from typing import Annotated, Any

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

#: The two event types that end a stream (§7.4). Anything after one of these is unreachable by a
#: client that closed on it, which is why exactly one may be emitted.
_TERMINAL_EVENTS = frozenset({SSEEventType.COMPLETE.value, SSEEventType.ERROR.value})


def _is_terminal(frame: str) -> bool:
    """True when an already-encoded SSE frame carries a terminal event.

    Reads the wire form rather than tracking intent separately, so this cannot disagree with what
    was actually sent — the mismatch between intent and wire is the class of bug that let three
    invented event names ship.
    """
    head, _, _ = frame.partition("\n")
    return head.removeprefix("event: ").strip() in _TERMINAL_EVENTS


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


def _service(request: Request) -> GenerationService:
    """The generation service, wired to the model port the lifespan already composed.

    The port is READ from `app.state` rather than constructed here, and that is the point of this
    function existing at all. It used to be `return GenerationService()` — a service with no
    provider path — so `backend/src/ai/routing/` was a complete six-tier cascade with a cache, per
    endpoint breakers and a key resolver that generation never called. The only consumer was
    `POST /api/v1/ai/complete`, which no product surface uses.

    `app.state.artifact_model` and not `app.state.model_router`, because `src/generation/` may not
    import `src.ai` (§2.2.1) and that ban is re-asserted by parsing rather than by a lint — the
    first version of this function named `ai.routing.tiers.ModelTier` and the parse check refused
    it. `ai/generation_port.py` builds the adapter; `core/model_port.py` is the only seam this
    module knows.

    Sharing the composed object rather than building a second one is load-bearing for a second
    reason: the router behind it holds per-endpoint breaker state, and a private set would let
    generation keep hammering an endpoint that `/ai/complete` had already opened the circuit on.

    A MISSING PORT IS A CONFIGURATION, NOT AN ERROR
    `_chokepoint` below raises when its collaborator is absent, because a generation run that
    cannot be submitted for approval is not a run worth streaming. This one does not: a deployment
    with no reachable endpoint still generates artifacts from the template path and records
    `served_from='template'`, which is a true row. Refusing to serve would turn a degraded
    configuration into an outage.
    """
    return GenerationService(model=getattr(request.app.state, "artifact_model", None))


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


async def _load_project_for_generation(session: AsyncSession, project_id: uuid.UUID) -> Mapping[str, Any] | None:
    """The project's name and settings, for naming the generated artifacts.

    Returns None rather than raising when the row is absent: the run itself is already scoped by the
    caller's authorisation, and `_render` documents what it falls back to. Failing the stream here
    would turn a naming detail into a failed generation.
    """
    result = await session.execute(
        text("SELECT name, path, repo_url, settings FROM projects WHERE id = :id"),
        {"id": project_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


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
        await _insert_run(session, run_id=run_id, body=body, principal=principal, service=service)

        # THE SERVICE'S TERMINAL FRAME IS WITHHELD, and this route emits the single terminal frame
        # itself. §7.4 permits exactly one, and it must be last.
        #
        # The earlier version forwarded everything the service produced and then appended its own
        # frame describing what governance did — so a successful run emitted `complete` followed by
        # `status`, and a refused submission emitted `complete` followed by `error`. The second is a
        # second TERMINAL event, which Q-26 forbids: a client that closes on the first never learns
        # the submission was refused, and one that reads to the end sees a run reported both accepted
        # and failed.
        #
        # Only this function knows the real outcome, because the outcome includes whether the
        # chokepoint accepted the artifacts. So the terminal frame belongs here.
        withheld: str | None = None
        # The project row, so the rendered manifests name the REAL application. Loaded here because
        # this is where the session lives; the service takes facts, not a database handle.
        project_row = await _load_project_for_generation(session, body.project_id)
        async for frame in service.stream_generation(
            body.project_id, body.prompt, outcome=outcome, project=project_row
        ):
            if _is_terminal(frame):
                withheld = frame
                break
            yield frame.encode("utf-8")

        await _finish_run(session, run_id=run_id, outcome=outcome)

        if not outcome.validation_passed:
            # The service's own terminal frame is the right answer here: it is an `error` naming the
            # validation gate, and nothing is submitted because nothing passed.
            if withheld is not None:
                yield withheld.encode("utf-8")
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
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as the terminal frame
            # A refusal by the chokepoint is a legitimate outcome, not a server fault: a policy
            # deny, a blocked blast radius or a stale policy bundle all land here. This is THE
            # terminal frame, not one appended after another — the artifacts were generated and
            # then refused, so the run did not succeed.
            yield format_event(
                SSEEventType.ERROR,
                {
                    "run_id": str(run_id),
                    "detail": str(exc),
                    "state": "submission_refused",
                    "generated": [f.path for f in outcome.files],
                },
            ).encode("utf-8")
            return

        # THE single terminal frame, carrying what governance did with the run. The change-set id is
        # what §12.6 step 8 opens.
        yield format_event(
            SSEEventType.COMPLETE,
            {
                "run_id": str(run_id),
                "state": "accepted",
                "files": [f.path for f in outcome.files],
                "completion_tokens": outcome.completion_tokens,
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
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    body: GenerationRequest,
    principal: Principal,
    service: GenerationService,
) -> None:
    """Record the attempt as `running`.

    `served_from` AND `tier` ARE NOW BOUND PARAMETERS, WHICH IS THE POINT
    They were SQL string literals: `'template'` and `'deterministic'`, written directly into the
    INSERT text. So `generation_runs.served_from` could not record a provider call, a cache hit or
    anything else regardless of what the pipeline did — and `SERVED_FROM` already contained
    `('l1', 'l2', 'l3', 'provider', 'template')`, four of which were unreachable by construction.
    `'deterministic'` is not a `ModelTier` at all.

    `served_from='pending'` because this row is written BEFORE the first frame, and at that moment
    the run has not been served from anywhere. `pending` was added to `SERVED_FROM` by revision
    `0011` for exactly this state; the alternative was to state the path the service was about to
    ATTEMPT, which is a claim about an outcome that has not happened and is wrong for every run
    that crashes mid-stream. `_finish_run` overwrites it with what actually served the run.

    The row is still inserted first, for the reason it always was: a row written only on success
    would leave a crashed run with no trace, which is the opposite of what an evidence table is for.
    """
    await session.execute(
        text(
            "INSERT INTO generation_runs "
            "(id, project_id, tenant_id, requested_by, status, iterations_used, served_from, tier, "
            " prompt_tokens, completion_tokens) "
            "VALUES (:id, :project_id, :tenant_id, :requested_by, 'running', 0, :served_from, :tier, "
            " 0, 0)"
        ),
        {
            "id": run_id,
            "project_id": body.project_id,
            "tenant_id": principal.tenant_id,
            "requested_by": principal.user_id if principal.kind == "user" else None,
            "served_from": "pending",
            "tier": service.attempted_tier,
        },
    )
    await session.commit()


async def _finish_run(session: AsyncSession, *, run_id: uuid.UUID, outcome: GenerationOutcome) -> None:
    """Close the row out with the real counts, origin and terminal status.

    `served_from`, `tier`, `endpoint_id` and `iterations_used` are all written from the outcome the
    stream filled in, so the row reports what happened rather than what the schema was seeded with.
    A run served from L1 says `l1` and consumed zero iterations; a genuine model call says
    `provider` and names the endpoint that answered; a template fallback says `template` and
    carries status `template_fallback`, which distinguishes it from a deployment that never had a
    provider path at all.
    """
    await session.execute(
        text(
            "UPDATE generation_runs SET status = :status, prompt_tokens = :prompt_tokens, "
            "completion_tokens = :completion_tokens, served_from = :served_from, tier = :tier, "
            "endpoint_id = :endpoint_id, iterations_used = :iterations_used, finished_at = now() "
            "WHERE id = :id"
        ),
        {
            "id": run_id,
            "status": outcome.status,
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
            "served_from": outcome.served_from,
            "tier": outcome.tier,
            "endpoint_id": outcome.endpoint_id,
            "iterations_used": outcome.iterations_used,
        },
    )
    await session.commit()
