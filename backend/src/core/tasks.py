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
