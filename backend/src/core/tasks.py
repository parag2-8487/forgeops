# SPDX-License-Identifier: FSL-1.1-ALv2
"""Engine-neutral task dispatcher seam (design.md §7.9).

Phase 0: InlineDispatcher. Phase 1: ARQ or Dramatiq. Phase 2: exactly one
durable engine (Temporal, or Inngest) — introduced ONCE.
Business logic must never import an engine SDK directly (Research §0, §B6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol


@dataclass(frozen=True)
class TaskHandle:
    """Handle returned when a task is enqueued."""

    id: str
    dispatcher: str  # "inline" now; "arq"/"dramatiq" at P1; durable engine at P2


class TaskDispatcher(Protocol):
    """The only way business logic ever enqueues work.

    Phase 0: InlineDispatcher. Phase 1: ARQ or Dramatiq. Phase 2: exactly one
    durable engine (Temporal, or Inngest if self-host DX wins) — introduced ONCE.
    Business logic must never import an engine SDK directly (Research §0, §B6).
    """

    async def enqueue(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> TaskHandle: ...


# Registry of task handlers for InlineDispatcher
_TASK_HANDLERS: dict[str, Any] = {}


def register_task(name: str):
    """Decorator to register a task handler by name."""

    def decorator(func):
        _TASK_HANDLERS[name] = func
        return func

    return decorator


class InlineDispatcher:
    """Executes the handler in-process, immediately. Development and Phase 0 only.

    Not durable, not retried, not a queue. It exists so the seam has a real
    implementation rather than a stub, and so Phase 1 can swap it out with a
    one-line change in the lifespan.
    """

    async def enqueue(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> TaskHandle:
        task_id = idempotency_key or str(uuid.uuid4())
        handler = _TASK_HANDLERS.get(name)
        if handler is not None:
            await handler(payload)
        return TaskHandle(id=task_id, dispatcher="inline")


class ArqDispatcher:
    """Durable-ish dispatch onto ARQ, behind the unchanged seam (§4.6, §7.10, D-32).

    What this deliberately does NOT do
    ---------------------------------
    It leaks no engine concept upward. `enqueue` returns a `TaskHandle` and nothing
    else — no ARQ `Job` object, no workflow id, no signal, no query, no way for a
    caller to poll or cancel through an engine API. That restraint is the point of the
    seam: OQ-16 is still open between Temporal and Inngest for Phase 2, and every
    engine concept that reaches business logic is a rewrite when that decision lands.
    ARQ is chosen for Phase 1 because it is Redis-backed and already-present
    infrastructure, not because it is the final answer.

    Idempotency
    -----------
    `idempotency_key` becomes ARQ's `_job_id`. ARQ refuses to enqueue a second job with
    an id already present, returning None, so a duplicate enqueue is a no-op and the
    caller still receives a handle carrying the same id. That is exactly the contract
    `InlineDispatcher` provides (`task_id = idempotency_key or uuid4()`), so switching
    dispatchers cannot change whether a retry double-executes.

    Without a key, a fresh uuid4 is used rather than letting ARQ mint one: the handle
    must be meaningful to the caller before the enqueue is confirmed, and it keeps both
    dispatchers' id semantics identical.
    """

    def __init__(self, pool: Any, *, queue_name: str = "forgeops") -> None:
        # `pool` is typed `Any` on purpose. Annotating it `ArqRedis` would put an ARQ
        # type in a signature that `scripts/collect_call_sites.py` reads and that the
        # Ruff banned-api rule forbids importing outside this module.
        self._pool = pool
        self._queue_name = queue_name

    async def enqueue(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> TaskHandle:
        task_id = idempotency_key or str(uuid.uuid4())
        await self._pool.enqueue_job(
            name,
            payload,
            _job_id=task_id,
            _queue_name=self._queue_name,
        )
        # A None return means "already queued under this id". That is success for an
        # idempotent enqueue, so it is not distinguished here: doing so would hand the
        # caller an engine detail and invite branching on it.
        return TaskHandle(id=task_id, dispatcher="arq")


# ─── The only place in the codebase that may import `arq` ────────────────────
#
# The Ruff banned-api rule in backend/pyproject.toml forbids `import arq` everywhere
# else, with this module as the single per-file exemption. That is what keeps the
# Phase 2 engine decision (OQ-16) a one-file change rather than a migration.


def _redis_settings(redis_url: str) -> Any:
    """Translate the project's Redis DSN into ARQ's settings object."""
    from arq.connections import RedisSettings

    return RedisSettings.from_dsn(redis_url)


async def create_arq_pool(settings: Any) -> Any:
    """Create the ARQ Redis pool the dispatcher enqueues onto.

    Called from the lifespan. Deliberately does not ping: the Phase 0 lifespan contract
    is that construction validates local configuration and performs no mandatory
    network handshake, so an unreachable Redis changes readiness rather than liveness
    (§4.4, §11.1).
    """
    from arq import create_pool

    return await create_pool(_redis_settings(str(settings.redis_url)))


def build_dispatcher(settings: Any, pool: Any | None = None) -> TaskDispatcher:
    """Select the dispatcher `TASK_DISPATCHER` names.

    One function, so no caller ever branches on the engine. `inline` remains fully
    supported rather than being a dev-only fallback: the `production_app` fixture uses
    it so handlers run in-process without a worker, which is a transport substitution
    and not a collaborator substitution (§0.4.1).
    """
    mode = getattr(settings, "task_dispatcher", "inline")
    if mode == "inngest":
        # 2.2 and 2.4a. The client is built here rather than passed in, because `pool` is ARQ's
        # collaborator and threading a second engine handle through every caller of this function would
        # put an engine concept in the signature the seam exists to keep clean.
        return InngestDispatcher(build_inngest_client(settings))
    if mode == "arq":
        if pool is None:
            raise ValueError("TASK_DISPATCHER=arq requires an ARQ pool; call create_arq_pool first")
        return ArqDispatcher(pool, queue_name=getattr(settings, "arq_queue_name", "forgeops"))
    return InlineDispatcher()


def worker_functions() -> list[Any]:
    """Every registered handler, wrapped for ARQ's calling convention.

    ARQ calls `f(ctx, *args)`; the project's handlers take `(payload)`. Adapting here
    rather than changing `@register_task` keeps one handler signature across both
    dispatchers, which is what makes the "identical results under either dispatcher"
    test meaningful.
    """
    from arq import func

    def _adapt(name: str, handler: Any) -> Any:
        async def _run(_ctx: dict[str, Any], payload: dict[str, Any]) -> Any:
            return await handler(payload)

        return func(_run, name=name)

    return [_adapt(name, handler) for name, handler in sorted(_TASK_HANDLERS.items())]


# --- Phase 2: durable execution -------------------------------------------------------------------
#
# THE ENGINE DECISION (OQ-16) IS RESOLVED TO INNGEST, and the reason is self-hosting rather than
# features. Temporal needs a server, a database of its own and a worker fleet; Inngest's dev server is
# one container that discovers functions by calling an HTTP endpoint this app already serves. For a
# product an operator runs on their own machine, that difference is the whole argument -- Temporal would
# be a second stateful system to operate before the first deployment.
#
# WHAT A WORKFLOW IS HERE: DATA, NOT CODE. A `WorkflowDefinition` names steps and marks which ones wait
# for a human. It carries no engine type, so business logic declares a pipeline without importing an
# SDK, and `inngest_functions()` below is the ONLY place that turns one into engine objects. That is the
# thin-wrapper pattern 2.4a asks for, and it is what makes the engine decision reversible: a Temporal
# translator would read the same descriptions.


@dataclass(frozen=True)
class WorkflowStep:
    """One step of a durable workflow, naming a handler registered with `@register_task`."""

    name: str
    #: When true, the workflow WAITS FOR A HUMAN before running this step.
    #:
    #: The wait is the engine's, not a poll: the run is suspended and costs nothing until the event
    #: arrives. That is the reason a durable engine earns its place here at all -- an approval gate
    #: implemented with a sleep loop would hold a worker for however long the human takes.
    requires_approval: bool = False
    #: How long to wait before giving up. A day by default: an approval nobody answers should expire,
    #: because a pipeline suspended for ever is indistinguishable from one that was forgotten.
    approval_timeout_seconds: int = 86_400


@dataclass(frozen=True)
class WorkflowDefinition:
    """An ordered pipeline of steps, triggered by one event.

    NO ENGINE TYPES IN THIS CLASS. That is the entire constraint it exists to satisfy: `deployments/`
    declares its pipeline by constructing one of these, and nothing in `deployments/` imports an engine.
    """

    name: str
    steps: tuple[WorkflowStep, ...]

    @property
    def trigger_event(self) -> str:
        """The event that starts a run. Derived, so a caller cannot name one that nothing triggers."""
        return f"forgeops/{self.name}"

    def approval_event(self, step_name: str) -> str:
        """The event a gated step waits for. Derived for the same reason."""
        return f"forgeops/{self.name}.{step_name}.approved"


#: Registered workflows, by name. Same shape as `_TASK_HANDLERS` and for the same reason: the engine
#: translator reads a registry rather than being handed a list somebody has to remember to update.
_WORKFLOWS: dict[str, WorkflowDefinition] = {}


def register_workflow(definition: WorkflowDefinition) -> WorkflowDefinition:
    """Register a workflow. Raises on a duplicate name.

    A duplicate is a PROGRAMMING ERROR and not a last-one-wins: two modules declaring the same pipeline
    differently would produce a run whose shape depends on import order.
    """
    if definition.name in _WORKFLOWS:
        raise ValueError(f"workflow {definition.name!r} is already registered")
    for step in definition.steps:
        if step.name not in _TASK_HANDLERS:
            # REFUSED AT REGISTRATION, not at run time. A workflow naming a handler that does not exist
            # would fail midway through a real deployment, after the earlier steps had already run.
            raise ValueError(f"workflow {definition.name!r} names step {step.name!r}, which no @register_task declares")
    _WORKFLOWS[definition.name] = definition
    return definition


def registered_workflows() -> dict[str, WorkflowDefinition]:
    """Every registered workflow. A copy, so a caller cannot mutate the registry."""
    return dict(_WORKFLOWS)


class InngestDispatcher:
    """Durable dispatch onto Inngest, behind the unchanged seam.

    IT LEAKS NO ENGINE CONCEPT UPWARD, exactly as `ArqDispatcher` does not: `enqueue` returns a
    `TaskHandle` and nothing else -- no run id to poll, no cancel, no signal. A caller cannot tell which
    dispatcher it has, which is what makes swapping one a configuration change instead of a rewrite.

    IDEMPOTENCY. The key becomes the event's `id`. Inngest deduplicates events by id within a 24-hour
    window, so a duplicate enqueue is a no-op and the caller still receives a handle carrying the same
    id -- the same contract `InlineDispatcher` and `ArqDispatcher` provide, so switching dispatchers
    cannot change whether a retry double-executes. Without a key a fresh uuid4 is used rather than
    letting Inngest mint one, so the handle is meaningful before the send is confirmed.
    """

    def __init__(self, client: Any) -> None:
        # `Any` on purpose, like ARQ's pool: annotating it would put an engine type in a signature that
        # the banned-api rule forbids importing outside this module.
        self._client = client

    async def enqueue(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> TaskHandle:
        task_id = idempotency_key or str(uuid.uuid4())
        await self._client.send(_event(name=f"forgeops/{name}", event_id=task_id, data=payload))
        return TaskHandle(id=task_id, dispatcher="inngest")


def _event(*, name: str, event_id: str, data: dict[str, Any]) -> Any:
    """Build an engine event. Here so `InngestDispatcher` holds no import of its own."""
    import inngest

    return inngest.Event(name=name, id=event_id, data=data)


def build_inngest_client(settings: Any) -> Any:
    """The Inngest client, or `None` when the engine is not configured.

    `is_production` is driven by the app's own environment rather than left to the SDK's default, because
    the default reads `INNGEST_DEV` and a deployment that set neither would sign requests against a cloud
    it has no key for -- failing at the first enqueue with an authentication error rather than at startup.
    """
    import inngest

    return inngest.Inngest(
        app_id=getattr(settings, "inngest_app_id", "forgeops"),
        event_key=getattr(settings, "inngest_event_key", None),
        is_production=bool(getattr(settings, "inngest_is_production", False)),
        api_base_url=getattr(settings, "inngest_base_url", None) or None,
        event_api_base_url=getattr(settings, "inngest_base_url", None) or None,
    )


def inngest_functions(client: Any) -> list[Any]:
    """Translate every registered task and workflow into Inngest functions.

    THE ONLY PLACE A WORKFLOW BECOMES ENGINE CODE. Two shapes are produced:

    * ONE FUNCTION PER REGISTERED TASK, so `enqueue("some.task", ...)` behaves the same under this
      dispatcher as under ARQ. Without these, switching engines would silently change which names are
      enqueueable.
    * ONE FUNCTION PER REGISTERED WORKFLOW, whose body is the steps in order. Each step is a
      `step.run`, so a run that fails at step three RESUMES AT STEP THREE rather than re-running one and
      two -- which for this pipeline means a retry after a failed apply does not rebuild and re-push an
      image that is already in the registry.

    THE PAYLOAD THREADS THROUGH. Each step receives the accumulated payload and its return value is
    merged into it, which is how the digest a push produces reaches the apply that pins it. A step
    returning something other than a mapping is ignored rather than replacing the payload, because a
    handler that returns `None` must not erase what earlier steps established.
    """
    import inngest

    functions: list[Any] = []

    for task_name, handler in sorted(_TASK_HANDLERS.items()):

        def _make_task(task_name: str = task_name, handler: Any = handler) -> Any:
            @client.create_function(
                fn_id=task_name.replace(".", "-"),
                trigger=inngest.TriggerEvent(event=f"forgeops/{task_name}"),
            )
            async def _run(ctx: inngest.Context, step: inngest.Step) -> Any:
                async def _call() -> Any:
                    return await handler(dict(ctx.event.data))

                return await step.run(task_name, _call)

            return _run

        functions.append(_make_task())

    for definition in sorted(_WORKFLOWS.values(), key=lambda item: item.name):

        def _make_workflow(definition: WorkflowDefinition = definition) -> Any:
            @client.create_function(
                fn_id=definition.name.replace(".", "-"),
                trigger=inngest.TriggerEvent(event=definition.trigger_event),
            )
            async def _run(ctx: inngest.Context, step: inngest.Step) -> Any:
                payload: dict[str, Any] = dict(ctx.event.data)
                for declared in definition.steps:
                    if declared.requires_approval:
                        # THE RUN SUSPENDS HERE and costs nothing until a human answers. A timeout
                        # returning `None` STOPS the pipeline: continuing without the approval would
                        # mean the gate had no effect, which is worse than not having one.
                        granted = await step.wait_for_event(
                            f"{declared.name}-approval",
                            event=definition.approval_event(declared.name),
                            timeout=timedelta(seconds=declared.approval_timeout_seconds),
                        )
                        if granted is None:
                            return {
                                "stopped_at": declared.name,
                                "reason": "the approval expired before anyone answered",
                                **payload,
                            }
                        approval_data = dict(getattr(granted, "data", {}) or {})
                        payload = {**payload, **approval_data}

                    handler = _TASK_HANDLERS[declared.name]
                    frozen = dict(payload)

                    async def _call(handler: Any = handler, frozen: dict[str, Any] = frozen) -> Any:
                        return await handler(frozen)

                    produced = await step.run(declared.name, _call)
                    if isinstance(produced, dict):
                        payload = {**payload, **produced}
                    # A STEP MAY STOP THE PIPELINE by returning `stop: True`. Needed because a build
                    # that fails is a RESULT rather than an exception everywhere else in this codebase,
                    # and a pipeline that pushed an image whose build failed would be worse than one
                    # that raised.
                    if isinstance(produced, dict) and produced.get("stop") is True:
                        return {"stopped_at": declared.name, **payload}
                return payload

            return _run

        functions.append(_make_workflow())

    return functions


async def send_engine_event(client: Any, *, name: str, data: dict[str, Any]) -> None:
    """Send one event on an existing engine client.

    HERE AND NOT IN A ROUTE. A route that released a suspended run would otherwise construct an engine
    event, which means importing the SDK -- and the banned-api rule exists precisely so the engine
    decision stays a one-file change. The route reaches the client the lifespan built and names an
    event; this turns that into engine types.

    NO IDEMPOTENCY KEY. An approval event is deliberately not deduplicated: two humans releasing the
    same gate is harmless, because the WAIT resolves once -- whereas a key would make a second,
    legitimate release of a re-suspended run silently vanish.
    """
    import inngest

    await client.send(inngest.Event(name=name, data=data))
