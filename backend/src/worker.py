# SPDX-License-Identifier: FSL-1.1-ALv2
"""ARQ worker entry point (design.md §4.6, §7.10, D-32).

Run with `make worker`, which is `arq src.worker.WorkerSettings`.

Why this is a separate module rather than part of main.py
--------------------------------------------------------
A worker is not a web process. Sharing `create_app()` would give every worker an HTTP
stack, a router and the middleware chain it never uses, and would make an import cycle
between the app factory and the task registry. What it DOES share is the handler
registry and the settings object, which is the part that has to agree.

`WorkerSettings` is the only ARQ concept in this file, and it is assembled entirely
from helpers in `core/tasks.py` — this module imports no ARQ symbol at all. So the whole
engine surface sits behind ONE file, which is a stronger position than the design
assumed and is asserted by `tests/unit/test_tasks_arq.py`. The Ruff banned-api rule
keeps it that way, so the Phase 2 engine decision (OQ-16) stays a single-file change.
"""

from __future__ import annotations

from typing import Any

from .core.config import get_settings
from .core.logging import configure_logging
from .core.tasks import _redis_settings, worker_functions


def _import_task_modules() -> None:
    """Import every module that registers a task handler.

    `@register_task` populates the registry as a side effect of import, so a worker that
    imported nothing would start successfully and silently handle no jobs — a failure
    that looks like an idle queue rather than a broken deployment.

    Phase 1 grows this list as the index, embedding, policy-bundle and generation tasks
    land (tasks 9.3, 11.8, 11.9, 11.10, 13.8). The assertion in
    `tests/unit/test_tasks_arq.py` requires the worker's function set to equal the
    registry, so a handler added without being imported here fails the build.
    """
    from . import mcp  # noqa: F401 - registers the Phase 0 MCP tasks
    from .policies import tasks  # noqa: F401 - registers policy.bundle.publish


async def _startup(ctx: dict[str, Any]) -> None:
    """Configure the worker process before it takes its first job.

    ASYNC because ARQ awaits it: `await self.on_startup(self.ctx)` in `arq/worker.py`. Declared as a
    plain function this raised

        TypeError: object NoneType can't be used in 'await' expression

    during `main()`, so the worker exited before consuming anything -- and `make worker` had therefore
    never started successfully. The symptom elsewhere was an approved change set that never applied,
    because the apply job sat in Redis with no consumer.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    ctx["settings"] = settings


class WorkerSettings:
    """ARQ worker configuration, read from the same Settings the API uses."""

    _settings = get_settings()

    _import_task_modules()
    functions = worker_functions()

    redis_settings = _redis_settings(str(_settings.redis_url))
    queue_name = _settings.arq_queue_name
    max_jobs = _settings.arq_max_jobs
    job_timeout = _settings.arq_job_timeout_seconds

    # Keep results briefly: long enough for an operator to inspect a failure, short
    # enough that Redis is not a results database. Phase 2's durable engine owns
    # history; ARQ must not become a de facto one.
    keep_result = 3600

    on_startup = staticmethod(_startup)
