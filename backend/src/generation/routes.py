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

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core import index_evidence
from ..core.db import get_session
from ..core.errors import ProblemException, forbidden_problem
from ..core.readiness import ReadinessEngine
from ..core.sse import SSE_MEDIA_TYPE, SSEEventType, format_event
from ..governance.chokepoint import ChangeItemRequest, GovernanceChokepoint, MutationRequest
from .prompt_compiler import CompiledPrompt, compile_prompt, compile_repair_prompts
from .retrieval import retrieve_generation_context
from .service import GenerationOutcome, GenerationService

logger = logging.getLogger(__name__)


#: The two event types that end a stream (§7.4). Anything after one of these is unreachable by a
#: client that closed on it, which is why exactly one may be emitted.
_TERMINAL_EVENTS = frozenset({SSEEventType.COMPLETE.value, SSEEventType.ERROR.value})


def nl_join(skill_file_text: str) -> str:
    """Two blank lines and the skill file, so it reads as its own section rather than running on."""
    return "\n\n" + skill_file_text


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
    #: THE PROMPT CARRIES A BUILD LOG when this run is a repair, so the ceiling is sized for one.
    #:
    #: It was 4000, and that made "Resolve Errors with AI" fail with a 422 the user saw as
    #: "One or more fields failed validation." — a message that names neither the field nor the
    #: reason. Measured: the resolver's own instructions are 3133 characters, and the real failure
    #: a user pasted was 944 more, so a genuine repair request was 4977 and could never be accepted.
    #: The limit was set when the prompt was a sentence a human typed; it stopped being that when
    #: the resolver started attaching the host's error output, and nothing moved with it.
    #:
    #: 16000 is not a licence to send anything: it is roughly 4k tokens, comfortably inside every
    #: model's context, and large enough for a truncated build log (the agent caps what it returns)
    #: plus the instructions. A prompt past it is a bug worth rejecting rather than silently
    #: truncating, because a truncated log is a log with the cause cut out of it.
    prompt: str = Field(min_length=1, max_length=16000)
    #: Where the change is destined. Passed straight through to the chokepoint, and deliberately
    #: not defaulted here: `approval.rego` answers `require_approval` when the member is absent, so
    #: an omitted environment means a human reviews it rather than it auto-approving (finding 68).
    environment: str | None = Field(default=None, max_length=32)
    #: The failing readiness checks this run should address, or `None` for every one that a generated
    #: artifact can satisfy.
    #:
    #: PRD FR-89's shape: a user reads the readiness report, picks the findings they want acted on, and
    #: gets a change set that addresses those and nothing else. Without it a run either fixes everything
    #: it can — a large change set to review for one small complaint — or the user rewrites the whole
    #: prompt by hand and loses the compiled facts.
    #:
    #: Not validated against the check table here. An id that matches no failing check simply selects
    #: nothing, which is the same outcome as asking for a check that already passes; rejecting the
    #: request would make a stale browser tab an error rather than a no-op.
    target_checks: list[str] | None = Field(default=None, max_length=64)
    target_strategy: str | None = Field(default=None, max_length=128)
    #: The one endpoint to call, chosen on the Manual Generation page -- or None for the generation tier's
    #: own chain (the local Ollama models). A pinned run calls that endpoint and NOTHING else: no cascade,
    #: no cache, and no whole-run template fallback, so a failure is reported as the selected provider's.
    endpoint_id: str | None = Field(default=None, max_length=100)


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


def _selectable_endpoint_ids(request: Request) -> list[str]:
    raw = str(getattr(request.app.state.settings, "generation_selectable_endpoints", "") or "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _pinned_service(request: Request, body: GenerationRequest, default: GenerationService) -> GenerationService:
    """The service for this request: the default chain, or a port pinned to the operator's choice.

    Every refusal is a 422 naming the reason, raised before any run row exists: a selection that cannot be
    honoured must never quietly become a run on a different provider.
    """
    if body.endpoint_id is None:
        return default
    base = getattr(request.app.state, "artifact_model", None)
    if base is None or not hasattr(base, "pinned_to"):
        raise ProblemException(
            status=422,
            type_suffix="model-unavailable",
            title="Model selection is not available",
            detail="This deployment has no model routing configured, so no model can be selected.",
        )
    allowed = _selectable_endpoint_ids(request)
    choices = {choice["endpoint_id"]: choice for choice in base.endpoint_choices(allowed)}
    choice = choices.get(body.endpoint_id)
    if body.endpoint_id not in allowed or choice is None:
        raise ProblemException(
            status=422,
            type_suffix="model-unavailable",
            title="Unsupported model",
            detail=(
                f"'{body.endpoint_id}' is not a model the Manual Generation page can select. "
                f"Selectable: {', '.join(allowed) or 'none'}, or omit the selection for the default."
            ),
        )
    if not choice["available"]:
        raise ProblemException(
            status=422,
            type_suffix="model-unavailable",
            title="Selected model is not configured",
            detail=(
                f"'{body.endpoint_id}' cannot be called: {choice['reason'] or 'it is unavailable'}. "
                "Configure it under Model Tiers, or select another model."
            ),
        )
    return GenerationService(model=base.pinned_to(body.endpoint_id))


def _progress(run_id: uuid.UUID, state: str, **extra: object) -> bytes:
    """A `progress` frame naming the phase the run is in, so a silent screen is never ambiguous."""
    return format_event(SSEEventType.PROGRESS, {"run_id": str(run_id), "state": state, **extra}).encode("utf-8")


def _phase_description(outcome: GenerationOutcome) -> str:
    """Which phase a run was in when it stopped, from the timings it had recorded so far."""
    timings = outcome.timings
    if "compile_seconds" not in timings:
        return "preparing the project context"
    attempts = sum(1 for key in timings if key.endswith("_model_seconds") and key.startswith("attempt_"))
    if attempts == 0:
        if any(key.endswith("_first_token_seconds") for key in timings):
            return "the model was streaming its first answer"
        return "waiting for the model's first token (model load and prompt evaluation)"
    return f"the model was working on attempt {attempts + 1} after {attempts} completed attempt(s)"


async def _abandon_run(
    sessionmaker: Any,
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    outcome: GenerationOutcome,
    reason: str,
    started: float,
) -> None:
    """Close a run that did not finish as `failed`, with the reason and whatever evidence it gathered.

    Guarded by `status = 'running'` so a run that DID finish is never rewritten, and executed on a fresh
    session when one can be made, because the request's session may be what failed.
    """
    outcome.timings["total_seconds"] = round(time.monotonic() - started, 3)
    params = {
        "id": run_id,
        "reason": reason[:2000],
        "attempts": json.dumps(outcome.attempts),
        "timings": json.dumps(outcome.timings),
        "iterations": min(outcome.iterations_used, 3),
    }
    statement = text(
        "UPDATE generation_runs SET status = 'failed', failure_reason = :reason, "
        "attempts = CAST(:attempts AS jsonb), timings = CAST(:timings AS jsonb), "
        "iterations_used = :iterations, finished_at = now() WHERE id = :id AND status = 'running'"
    )
    try:
        if sessionmaker is not None:
            async with sessionmaker() as fresh:
                await fresh.execute(statement, params)
                await fresh.commit()
        else:
            await session.rollback()
            await session.execute(statement, params)
            await session.commit()
    except Exception:  # noqa: BLE001 - the startup reaper is the backstop for a write that cannot land
        logger.exception("generation: could not record run %s as failed", run_id)


async def reap_orphaned_runs(session: AsyncSession, *, older_than_seconds: float) -> int:
    """Mark runs left `running` by a previous process as `failed`. Returns how many were closed.

    A run streams inside its HTTP request, so no run survives the process that served it; one still
    `running` past the deadline is an orphan, not a slow run. Called once at startup.
    """
    result = await session.execute(
        text(
            "UPDATE generation_runs SET status = 'failed', finished_at = now(), "
            "failure_reason = COALESCE(failure_reason, "
            "'Orphaned: the run never reached a terminal state (the stream was abandoned or the backend "
            "restarted before it finished). Recorded as failed at startup; no artifacts were submitted.') "
            "WHERE status = 'running' AND created_at < now() - make_interval(secs => :age)"
        ),
        {"age": older_than_seconds},
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


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
    if row is None:
        return None
    data = dict(row)
    inv_res = await session.execute(
        text("SELECT inventory FROM analysis_reports WHERE project_id = :p ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id},
    )
    inv = inv_res.scalar_one_or_none() or {}
    if not inv.get("manifests"):
        with contextlib.suppress(Exception):
            ft_res = await session.execute(
                text(
                    "SELECT path FROM file_tree WHERE project_id = :p AND ("
                    "path LIKE '%package.json' OR path LIKE '%package-lock.json' OR "
                    "path LIKE '%requirements.txt' OR path LIKE '%pyproject.toml' OR "
                    "path LIKE '%go.mod' OR path LIKE '%Cargo.toml')"
                ),
                {"p": project_id},
            )
            found_manifests = [str(r[0]).replace("\\", "/") for r in ft_res.fetchall()]
            if found_manifests:
                inv = dict(inv)
                inv["manifests"] = sorted(found_manifests)
    data["inventory"] = inv
    return data


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
    # Resolved BEFORE the stream, so a selection that cannot be honoured is a 422 the page can show next to
    # the selector, rather than a run row that fails a second later.
    service = _pinned_service(request, body, service)
    deadline_seconds = float(getattr(request.app.state.settings, "generation_run_deadline_seconds", 2400.0))
    sessionmaker = getattr(request.app.state, "sessionmaker", None)

    async def stream() -> AsyncGenerator[bytes]:
        started = time.monotonic()
        # The row is inserted BEFORE the first frame, as `running`. A row written only on success
        # would leave a crashed run with no trace at all, which is the opposite of what an
        # evidence table is for.
        await _insert_run(session, run_id=run_id, body=body, principal=principal, service=service)
        finished = False
        inner = _run_stream(started)
        deadline = started + deadline_seconds
        try:
            # `wait_for(anext(...))` and not `asyncio.timeout` around the loop: a timeout scope that spans a
            # `yield` can fire while this generator is suspended in the CLIENT's send, cancelling Starlette
            # rather than the run. Each step of the inner generator runs as its own awaited unit, so the
            # deadline cancels the model call that is actually late.
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    chunk = await asyncio.wait_for(anext(inner), timeout=remaining)
                except StopAsyncIteration:
                    break
                yield chunk
            finished = True
        except TimeoutError:
            # THE WHOLE-RUN DEADLINE. The per-endpoint timeouts multiply across attempts and failover, so
            # without this a CPU-only run could stay `running` for two hours. The run ends as itself --
            # `failed`, with the reason -- and the client gets the one terminal frame §7.4 requires.
            reason = (
                f"The run exceeded its {int(deadline_seconds)}s deadline while "
                f"{_phase_description(outcome)}. On a CPU-only model server one attempt can take several "
                "minutes; select a hosted model, or raise GENERATION_RUN_DEADLINE_SECONDS."
            )
            await _abandon_run(sessionmaker, session, run_id=run_id, outcome=outcome, reason=reason, started=started)
            finished = True
            yield format_event(
                SSEEventType.ERROR,
                {"run_id": str(run_id), "state": "deadline_exceeded", "detail": reason},
            ).encode("utf-8")
        finally:
            # The inner generator owns the in-flight model task; closing it cancels that task rather than
            # leaving an orphaned provider call holding a CPU model server for minutes.
            with contextlib.suppress(Exception):
                await inner.aclose()
            if not finished:
                # THE CLIENT WENT AWAY, OR SOMETHING RAISED, mid-stream. Before this the row stayed
                # `running` forever with no attempts recorded -- four such rows were found on 2026-11-01,
                # indistinguishable from runs still working. Closed out on a fresh session, because the
                # request's session may be the very thing that broke, and shielded so the cancellation
                # that triggered this cannot also cancel the write that records it.
                await asyncio.shield(
                    _abandon_run(
                        sessionmaker,
                        session,
                        run_id=run_id,
                        outcome=outcome,
                        reason=(
                            "The run was abandoned before it finished (the browser closed the stream, or the "
                            f"server raised) while {_phase_description(outcome)}. No artifacts were submitted."
                        ),
                        started=started,
                    )
                )

    async def _run_stream(started: float) -> AsyncGenerator[bytes]:
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
        yield format_event(
            SSEEventType.STATUS,
            {"run_id": str(run_id), "project_id": str(body.project_id), "state": "running"},
        ).encode("utf-8")
        yield _progress(run_id, "preparing_context")
        phase_started = time.monotonic()
        # The project row, so the rendered manifests name the REAL application. Loaded here because
        # this is where the session lives; the service takes facts, not a database handle.
        project_row = await _load_project_for_generation(session, body.project_id)

        # FR-13's retrieval, ONCE per run rather than once per attempt.
        #
        # ONCE, not per attempt, for two reasons. The repair loop re-prompts with the validator's
        # findings, and the codebase has not changed between attempts — so retrieving again would spend
        # a hybrid search to get the same rows. And the cache key is computed over the prompt: context
        # that varied between attempts would make attempt 2 of an identical request miss a cache entry
        # attempt 1 had just written.
        retrieval = await retrieve_generation_context(session, project_id=body.project_id, query=body.prompt)
        # Persisted whether or not it found anything, because "we searched and the index held nothing
        # relevant" and "we never searched" are different facts about a run.
        outcome.retrieval = retrieval.as_record()
        outcome.timings["context_seconds"] = round(time.monotonic() - phase_started, 3)

        # THE COMPILED INSTRUCTION. Built here for the same reason the project row and the retrieval are
        # loaded here: this is where the session lives, and `service.py` takes facts rather than a
        # database handle.
        yield _progress(run_id, "compiling_instruction")
        phase_started = time.monotonic()
        effective_strat = body.target_strategy or (
            (project_row.get("settings") or {}).get("deployment_strategy") if project_row else None
        )
        compiled, existing_contents, repair_prompts = await _compile_generation_plan(
            session,
            project_id=body.project_id,
            selected_check_ids=body.target_checks,
            target_strategy=effective_strat,
            # HOW MUCH OF THE PROJECT'S BUILD FILES TO QUOTE, by what the model can read. A hosted model takes
            # a million tokens; the local CPU/GPU model runs in an 8K window that the plan already half fills.
            build_inputs_chars=3_000 if service.attempted_tier in ("self_hosted", "template") else 40_000,
        )
        # RELOADED AFTER THE COMPILE, because the compile is what indexes a project scanned for the first
        # time. The row loaded above predates that scan, so a freshly imported project reached the template
        # fallback with an empty inventory and was described as Python (2026-11-02).
        project_row = await _load_project_for_generation(session, body.project_id) or project_row
        # 2.13: THIS PROJECT'S LEARNED PREFERENCES, injected into the instruction.
        #
        # After compilation and before recording, deliberately: the recorded prompt must be the text the
        # model actually received, or the run cannot be judged from it.
        memory_port = getattr(request.app.state, "memory_port", None)
        if compiled is not None and memory_port is not None:
            memory = await memory_port.memory_for_prompt(session, project_id=body.project_id)
            if not memory.is_empty:
                skill_file = nl_join(memory.content)
                compiled = replace(compiled, text=compiled.text + skill_file)
                # THE REPAIR PROMPTS GET IT TOO: a repair is a model call like any other, and the retry is
                # the call that has to land.
                repair_prompts = {
                    path: replace(prompt, text=prompt.text + skill_file) for path, prompt in repair_prompts.items()
                }
                await memory_port.record_injection(session, project_id=body.project_id, memory=memory)
        outcome.timings["compile_seconds"] = round(time.monotonic() - phase_started, 3)

        # Persisted AND COMMITTED before the stream. It used to be written into the request's open
        # transaction and committed only by `_finish_run`, after the model returned -- so for the
        # minutes a CPU run takes, "Show the instruction" read NULL and said this run recorded none, and a
        # run abandoned mid-stream rolled the instruction back and lost it for good.
        #
        # THE TEXT IS WHAT ATTEMPT 1 SENDS, byte for byte: the compiled plan PLUS the operator's own
        # request section, redacted -- `service.opening_prompt` is the same function the attempt loop
        # uses. A free-text run with no compiled plan now records its instruction too.
        sent = service.opening_prompt(body.prompt, project=project_row, retrieval=retrieval, compiled=compiled)
        await _record_compiled_prompt(session, run_id=run_id, compiled=compiled, sent_text=sent)
        await session.commit()
        yield _progress(run_id, "instruction_recorded")

        async for frame in service.stream_generation(
            body.project_id,
            body.prompt,
            outcome=outcome,
            project=project_row,
            retrieval=retrieval,
            compiled=compiled,
            repair_prompts=repair_prompts,
            existing=existing_contents,
        ):
            if _is_terminal(frame):
                withheld = frame
                break
            if frame.startswith(f"event: {SSEEventType.STATUS.value}\n"):
                continue
            yield frame.encode("utf-8")

        try:
            outcome.timings["total_seconds"] = round(time.monotonic() - started, 3)
            await _finish_run(session, run_id=run_id, outcome=outcome)

            if not outcome.validation_passed:
                # The service's own terminal frame is the right answer here: it is an `error` naming the
                # validation gate (or the pinned provider's failure), and nothing is submitted because
                # nothing passed.
                if withheld is not None:
                    yield withheld.encode("utf-8")
                return

            # THE PRE-IMAGES, loaded before the submit because that is where the session lives.
            preimages = await _editable_preimages(
                session, project_id=body.project_id, paths=[f.path for f in outcome.files]
            )

            submission = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=body.project_id,
                    items=tuple(_change_item_for(f.path, f.content, preimages) for f in outcome.files),
                    reason=f"generated from prompt: {body.prompt[:180]}",
                    origin="generation",
                    generation_run_id=run_id,
                    environment=body.environment,
                ),
                principal=principal,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as the terminal frame
            # A refusal by the chokepoint or persistence issue is surfaced to the client as the
            # terminal frame, guaranteeing the stream never ends without a terminal event.
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
                "endpoint_id": outcome.endpoint_id,
                "timings": outcome.timings,
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
            " prompt_tokens, completion_tokens, requested_endpoint_id, requested_model) "
            "VALUES (:id, :project_id, :tenant_id, :requested_by, 'running', 0, :served_from, :tier, "
            " 0, 0, :requested_endpoint_id, :requested_model)"
        ),
        {
            "id": run_id,
            "project_id": body.project_id,
            "tenant_id": principal.tenant_id,
            "requested_by": principal.user_id if principal.kind == "user" else None,
            "served_from": "pending",
            "tier": service.attempted_tier,
            # What the operator ASKED FOR, beside what will answer (`endpoint_id`, written at the end).
            "requested_endpoint_id": body.endpoint_id,
            "requested_model": getattr(getattr(service, "_model", None), "model_name", None),
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
            "endpoint_id = :endpoint_id, iterations_used = :iterations_used, "
            # FR-13. This column has existed since revision `0006` and was never written, so a run's
            # grounding was unrecoverable after the request finished — which made "generated using RAG
            # context" a claim with no record behind it.
            "retrieval = CAST(:retrieval AS jsonb), "
            # PER-ATTEMPT EVIDENCE, revision `0037`. A row that says `template_fallback` must be able to
            # name which attempt failed and why -- and, because each record carries its duration, whether
            # it was refused on its merits or cut off by a client budget. That distinction is what three
            # investigations this cycle lacked.
            "attempts = CAST(:attempts AS jsonb), timings = CAST(:timings AS jsonb), "
            "failure_reason = :failure_reason, "
            # THE TEXT ATTEMPT 1 ACTUALLY SENT overwrites the pre-stream record when the two differ, so the
            # stored instruction is what the provider received even if a future change lets them drift.
            "compiled_prompt = COALESCE(:sent_prompt, compiled_prompt), finished_at = now() "
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
            # Serialised explicitly, and left NULL when retrieval was never attempted. Passing a dict
            # to a JSONB parameter through `text()` leaves the driver to guess, and the guess differs
            # between asyncpg and psycopg.
            "retrieval": json.dumps(outcome.retrieval) if outcome.retrieval is not None else None,
            "attempts": json.dumps(outcome.attempts),
            "timings": json.dumps(outcome.timings),
            "failure_reason": outcome.failure_reason,
            "sent_prompt": outcome.sent_prompt,
        },
    )
    await session.commit()


async def _compile_generation_plan(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    selected_check_ids: list[str] | None,
    target_strategy: str | None = None,
    build_inputs_chars: int = 12_000,
) -> tuple[CompiledPrompt | None, Mapping[str, str], dict[str, CompiledPrompt]]:
    """Build the instruction from this project's own index and its failing checks.

    Returns None when the project has never been scanned. That is deliberate rather than a fallback to a
    generic prompt: with no index there are no facts, and a prompt built from no facts is exactly the
    template-with-substitutions this compiler exists to replace. The service then uses the older
    free-text path, which is honest about having nothing to ground itself in.
    """
    evidence = await index_evidence.load_index_evidence(session, project_id=project_id)
    if not evidence.paths:
        try:
            proj_res = await session.execute(
                text("SELECT path, tenant_id FROM projects WHERE id = :p"),
                {"p": project_id},
            )
            proj_row = proj_res.mappings().first()
            if proj_row and proj_row.get("path"):
                from ..analysis.indexer import persist_scan_report
                from ..deployments.autonomous_gates import resolve_workspace_path
                from ..deployments.autonomous_source import scan_workspace_to_report

                ws_path = resolve_workspace_path(proj_row["path"])
                if ws_path.exists() and ws_path.is_dir():
                    report = scan_workspace_to_report(ws_path)
                    if report and getattr(report, "files", None):
                        tenant_id = proj_row.get("tenant_id")
                        await persist_scan_report(
                            session,
                            project_id=project_id,
                            tenant_id=tenant_id,
                            report=report,
                            embedder=None,
                            embedder_absent_reason="Direct workspace scan fallback for generation prompt compiler",
                        )
                        await session.flush()
                        evidence = await index_evidence.load_index_evidence(session, project_id=project_id)
        except Exception as scan_err:
            logger.warning("Direct workspace scan fallback in _compile_generation_plan failed: %s", scan_err)

    if not evidence.paths:
        # No index: no facts to compile from, and no stored text to compare a rewrite against.
        return None, {}, {}

    inventory = (
        await session.execute(
            text("SELECT inventory FROM analysis_reports WHERE project_id = :p ORDER BY created_at DESC LIMIT 1"),
            {"p": project_id},
        )
    ).scalar_one_or_none() or {}

    if not inventory.get("manifests") and evidence.paths:
        manifests_from_paths = [
            p
            for p in evidence.paths
            if any(
                p.endswith(m) or p == m
                for m in (
                    "package.json",
                    "package-lock.json",
                    "pnpm-lock.yaml",
                    "yarn.lock",
                    "requirements.txt",
                    "pyproject.toml",
                    "go.mod",
                    "Cargo.toml",
                    "pnpm-workspace.yaml",
                )
            )
        ]
        if manifests_from_paths:
            inventory = dict(inventory)
            inventory["manifests"] = sorted(manifests_from_paths)
    # The project's own name, which is the fact that fills the one placeholder the findings table
    # carries: a chart directory. Without it `charts/<name>/Chart.yaml` would reach the model verbatim
    # and the model would either invent a name or write the angle brackets into the tree.
    project_name = (
        await session.execute(text("SELECT name FROM projects WHERE id = :p"), {"p": project_id})
    ).scalar_one_or_none() or ""
    report = ReadinessEngine().evaluate(evidence)
    # THE CONTENTS TRAVEL WITH THE PROMPT, because the same index answers both questions: what to
    # tell the model, and what each file it rewrites already satisfied. Loading the index twice would
    # let the two drift apart between the compile and the gate.
    return (
        compile_prompt(
            checks=report.checks,
            paths=evidence.paths,
            contents=evidence.contents,
            inventory=inventory,
            selected_check_ids=selected_check_ids,
            project_name=str(project_name),
            target_strategy=target_strategy,
            build_inputs_chars=build_inputs_chars,
        ),
        evidence.contents,
        # ONE PROMPT PER ARTIFACT, for the repair loop. Built here, from the SAME readiness report that
        # produced the opening ask, so a repair cannot disagree with it about what is failing -- loading
        # the index twice is how the compile and the gate drift apart.
        compile_repair_prompts(
            checks=report.checks,
            paths=evidence.paths,
            contents=evidence.contents,
            inventory=inventory,
            selected_check_ids=selected_check_ids,
            project_name=str(project_name),
            target_strategy=target_strategy,
            build_inputs_chars=build_inputs_chars,
        ),
    )


async def _record_compiled_prompt(
    session: AsyncSession, *, run_id: uuid.UUID, compiled: CompiledPrompt | None, sent_text: str | None = None
) -> None:
    """Store the exact text the model was sent, so a run can be judged rather than guessed at.

    A run used to be judged only by its output. When one produced a file in the wrong place there was no
    way to tell whether the model had disobeyed a correct instruction or obeyed a bad one, and those two
    faults have opposite fixes.

    Nothing is written when no plan was compiled: `NULL` reads as "not recorded", which is true, and an
    empty string would read as "the model was sent nothing", which is not.
    """
    if compiled is None:
        if sent_text:
            # A free-text run has no compiled plan, but it still sent an instruction, and "nothing was
            # recorded" was false for it. The budget and check columns stay NULL: no plan, no budget.
            await session.execute(
                text("UPDATE generation_runs SET compiled_prompt = :text WHERE id = :id"),
                {"id": run_id, "text": sent_text},
            )
        return
    await session.execute(
        text(
            "UPDATE generation_runs SET compiled_prompt = :text, "
            "prompt_token_estimate = :estimate, prompt_token_budget = :budget, "
            "addressed_checks = CAST(:addressed AS jsonb), "
            "deferred_checks = CAST(:deferred AS jsonb) WHERE id = :id"
        ),
        {
            "id": run_id,
            "text": sent_text if sent_text is not None else compiled.text,
            "estimate": compiled.token_estimate,
            "budget": compiled.token_budget,
            "addressed": json.dumps(list(compiled.addressed_checks)),
            "deferred": json.dumps(list(compiled.deferred_checks)),
        },
    )


async def _editable_preimages(session: AsyncSession, *, project_id: uuid.UUID, paths: Sequence[str]) -> dict[str, str]:
    """The indexed content of each path whose stored text is byte-identical to the file on disk.

    WHY THIS RETURNS FEWER PATHS THAN EXIST, AND WHY THAT IS THE POINT

    An `update` carries `old_content`, whose SHA-256 becomes `change_items.old_hash`, and the agent
    recomputes the hash of the file it is about to write and aborts the whole set if it disagrees. So a
    wrong pre-image does not corrupt anything — it makes the apply impossible. Getting it right is
    therefore not a nicety; it is the difference between an edit that can land and one that always
    refuses.

    `file_contents` stores REDACTED text (§6.3, §7.11), and `file_tree.content_hash` is the hash OF THE
    REDACTED TEXT — the agent redacts before it hashes. So the server holds no hash of the real file and
    cannot manufacture one. That is the correct design: unredacted content must not leave the machine.

    The consequence is precise rather than fatal. When a file had ZERO redactions the redactor returned
    its input unchanged, so the stored text and the bytes on disk are the same bytes, and hashing the
    stored text yields exactly what the agent will compute. When a file had one or more redactions the
    stored text differs from disk by construction, the true bytes are unknown here, and no `update` may
    be offered for it — the change set says so instead, and the file is created-or-refused rather than
    silently mangled.

    In practice the excluded set is small and its exclusion is honest: a Dockerfile or a manifest with a
    credential in it is a file whose readiness report already has something worse to say.
    """
    if not paths:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT f.path, c.content FROM file_contents c JOIN file_tree f ON f.id = c.file_id "
                "WHERE f.project_id = :p AND c.redaction_count = 0"
            ),
            {"p": project_id},
        )
    ).all()
    wanted = {path.replace("\\", "/").lower() for path in paths}
    return {
        str(row[0]).replace("\\", "/"): str(row[1]) for row in rows if str(row[0]).replace("\\", "/").lower() in wanted
    }


def _change_item_for(path: str, content: str, preimages: Mapping[str, str]) -> ChangeItemRequest:
    """An update when the pre-image is known, a create otherwise.

    THE CASE THIS FIXES. Every generated artifact used to be a `create`, and
    `agent/internal/executor/internal/mutate/apply.go` refuses a create whose target already exists —
    correctly, since a create asserts absence. So a project with a half-correct Dockerfile could be
    given a better one and could never apply it: the change set reached the agent, the agent refused the
    whole set with a conflict, and the change set ended `rolled_back`. That is the common case and the
    one where help is most valuable.

    An update carries the pre-image the agent verifies against, so the same change set now lands — and
    still refuses if the file moved on disk after compilation, which is the protection `old_hash` exists
    for and which a create could never express.
    """
    normalised = path.replace("\\", "/")
    existing = preimages.get(normalised)
    if existing is None:
        # Either the file does not exist, or its stored text is not the bytes on disk. Both mean no
        # honest pre-image is available, and a create is the only thing that can be claimed.
        return ChangeItemRequest(file_path=path, action="create", new_content=content)
    return ChangeItemRequest(
        file_path=path,
        action="update",
        old_content=existing,
        new_content=content,
    )


class GenerationRunResponse(BaseModel):
    """One generation run, including the exact instruction the model was given.

    WHY THE PROMPT BELONGS ON THE RECORD AND NOT ONLY IN A LOG. The row recorded the tier, the endpoint,
    the token counts, the retrieval record and the outcome — everything except what the model was asked
    to do. So when a run wrote a file to the wrong path there was no way to tell whether the model had
    disobeyed a correct instruction or obeyed a bad one, and those two faults have opposite fixes. The
    column was added with the compiler and nothing read it, which is the dead-column shape this codebase
    keeps producing; this route is the reader that makes it evidence rather than storage.

    `compiled_prompt` is None for a run made before the compiler existed, or for a request with no
    readiness findings behind it. That reads as "not recorded", which is true. An empty string would read
    as "the model was sent nothing", which is a different and false claim.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    tier: str | None = None
    endpoint_id: str | None = None
    served_from: str | None = None
    iterations_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    #: The exact text sent to the provider.
    compiled_prompt: str | None = None
    #: What the compiler estimated, beside the budget it was held to. Both are carried because a prompt
    #: that dropped a section is only explicable next to the limit that forced the drop.
    prompt_token_estimate: int | None = None
    prompt_token_budget: int | None = None
    #: The failing checks this run set out to fix, and the ones the budget could not hold. A user looking
    #: at an unchanged score needs to know a check was deferred rather than attempted and failed.
    addressed_checks: list[str] = Field(default_factory=list)
    deferred_checks: list[str] = Field(default_factory=list)
    #: Revision 0040: what was asked for, where the time went, and why a run failed.
    requested_endpoint_id: str | None = None
    requested_model: str | None = None
    timings: dict[str, float] = Field(default_factory=dict)
    failure_reason: str | None = None
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None = None
    finished_at: str | None = None


@router.get("/runs/{run_id}", summary="Read one generation run and the prompt it was given")
async def read_generation_run(
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GenerationRunResponse:
    """Return one run, including the instruction the model received.

    A MISSING RUN AND ANOTHER TENANT'S RUN ANSWER IDENTICALLY, with the same non-disclosing 403 the other
    read routes use. Distinguishing them would turn this into an enumeration oracle for run ids, exactly
    as it would for project ids.
    """
    row = (
        (
            await session.execute(
                text(
                    "SELECT id, project_id, tenant_id, status, tier, endpoint_id, served_from, "
                    "iterations_used, prompt_tokens, completion_tokens, compiled_prompt, "
                    "prompt_token_estimate, prompt_token_budget, addressed_checks, deferred_checks, "
                    "requested_endpoint_id, requested_model, timings, failure_reason, attempts, "
                    "created_at, finished_at "
                    "FROM generation_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None or row["tenant_id"] != principal.tenant_id:
        raise forbidden_problem()

    return GenerationRunResponse(
        id=row["id"],
        project_id=row["project_id"],
        status=str(row["status"]),
        tier=row["tier"],
        endpoint_id=row["endpoint_id"],
        served_from=row["served_from"],
        iterations_used=int(row["iterations_used"] or 0),
        prompt_tokens=int(row["prompt_tokens"] or 0),
        completion_tokens=int(row["completion_tokens"] or 0),
        compiled_prompt=row["compiled_prompt"],
        prompt_token_estimate=row["prompt_token_estimate"],
        prompt_token_budget=row["prompt_token_budget"],
        # `or []` because a run with no addressed checks and a run made before the column existed are
        # both empty here, and neither is evidence about the other.
        addressed_checks=list(row["addressed_checks"] or []),
        deferred_checks=list(row["deferred_checks"] or []),
        requested_endpoint_id=row["requested_endpoint_id"],
        requested_model=row["requested_model"],
        timings=dict(row["timings"] or {}),
        failure_reason=row["failure_reason"],
        # Each attempt's verdict and duration, without any prompt text: the instruction is served once,
        # above, and nothing here can carry a header or credential.
        attempts=list(row["attempts"] or []),
        created_at=row["created_at"].isoformat() if row["created_at"] else None,
        finished_at=row["finished_at"].isoformat() if row["finished_at"] else None,
    )


class GenerationModelChoice(BaseModel):
    """One model the Manual Generation page may offer. No credential or base URL is ever included."""

    endpoint_id: str | None
    provider: str
    model: str
    protocol: str | None = None
    available: bool
    reason: str | None = None
    default: bool = False


@router.get("/models", summary="The models a manual generation run may be pinned to")
async def list_generation_models(request: Request) -> list[GenerationModelChoice]:
    """The default chain (local Ollama) plus each selectable endpoint, with whether it can be called now.

    An unconfigured provider is LISTED as unavailable with its reason rather than omitted, so the page can
    tell the operator what to fix instead of leaving them wondering why Gemini is missing.
    """
    base = getattr(request.app.state, "artifact_model", None)
    if base is None or not hasattr(base, "endpoint_choices"):
        return []
    return [GenerationModelChoice(**choice) for choice in base.endpoint_choices(_selectable_endpoint_ids(request))]
