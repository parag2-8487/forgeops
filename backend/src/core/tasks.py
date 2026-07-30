# SPDX-License-Identifier: FSL-1.1-ALv2
"""Engine-neutral task dispatcher seam (design.md §7.9).

Phase 0: InlineDispatcher. Phase 1: ARQ or Dramatiq. Phase 2: exactly one
durable engine (Temporal, or Inngest) — introduced ONCE.
Business logic must never import an engine SDK directly (Research §0, §B6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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
