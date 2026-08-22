# SPDX-License-Identifier: FSL-1.1-ALv2
"""The task dispatcher's Redis pool is composed, and reached, by the route that needs it.

WHY THIS EXISTS

`.env.example` ships `TASK_DISPATCHER=arq`, and `build_dispatcher` refuses to invent a pool:

    ValueError: TASK_DISPATCHER=arq requires an ARQ pool; call create_arq_pool first

Nothing called `create_arq_pool` and nothing set `app.state.arq_pool`, so every route that enqueues
work answered 500 on the committed default configuration. `POST /api/v1/policies/publish` is one of
them, and the consequences ran a long way from the cause: no policy bundle could be published, so no
device could be pinned to one, so `GovernanceChokepoint` refused every generation submission with
"policy bundle stale: device pinned <none>". The control was right; the fact it checked had never
been established.

WHY IT IS COMPOSED LAZILY, WHICH IS WHAT THESE TESTS PIN

Two earlier placements were wrong, and CI caught both rather than review:

  * created in the lifespan, it made an unreachable Redis a slow start rather than a readiness
    failure. `arq.create_pool` connects and retries despite `create_arq_pool`'s docstring, so the
    `ci / secrets` job -- whose Redis is not at the compose hostname -- logged
    "redis connection error redis:6379" once a second until startup exceeded its timeout and the job
    failed with `TimeoutError`, no test having failed;

  * bounded to three seconds, that job passed and `ci / backend` stopped producing output for thirty
    minutes, because every one of the suite's app constructions then paid for a connection attempt.

So the pool is built by the first request that needs one, once, and cached. `app.state.arq_pool`
starts as `None`, which is why this file carries the `@wires` declaration: `test_wiring_coverage.py`
requires every attribute the composition places on `app.state` to be named by some wiring test, so a
newly composed collaborator cannot arrive untested (D-23).
"""

from __future__ import annotations

import os

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from src.core.tasks import ArqDispatcher, InlineDispatcher, build_dispatcher

from .capability import require_capability
from .production_app import apply_committed_baseline_env
from .wiring import wires

pytestmark = pytest.mark.mandatory


@wires("arq_pool", "arq_pool_lock")
class TestTheTaskPoolIsComposedWhereItIsNeeded:
    """`arq_pool` and its lock, declared here and driven through the real graph below."""

    @pytest.mark.asyncio
    async def test_the_composition_declares_the_attribute_before_any_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`app.state.arq_pool` exists from startup, and is None until something needs it.

        Both halves matter. Existing is what lets `get_bundle_service` read it without a
        `hasattr` dance; being None is what proves the lifespan is not connecting to Redis,
        which is the regression that timed the `secrets` job out.
        """
        apply_committed_baseline_env(monkeypatch)
        from src.main import create_app

        app = create_app()
        async with LifespanManager(app):
            assert hasattr(app.state, "arq_pool"), (
                "the composition must declare arq_pool; get_bundle_service reads it directly"
            )
            assert app.state.arq_pool is None, (
                "startup must not build the pool: arq.create_pool connects, and an unreachable "
                "Redis has to be a readiness failure rather than a slow start"
            )

    def test_the_dispatcher_refuses_arq_without_a_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The refusal this whole arrangement exists to satisfy, asserted directly.

        If this ever stops raising, `build_dispatcher` has started inventing a pool and the 500
        that led here would become a silent no-op instead.
        """
        apply_committed_baseline_env(monkeypatch)
        monkeypatch.setenv("TASK_DISPATCHER", "arq")
        from src.core.config import get_settings

        with pytest.raises(ValueError, match="requires an ARQ pool"):
            build_dispatcher(get_settings(), pool=None)

    def test_inline_needs_no_pool_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`inline` is a supported mode, not a dev fallback, so it must not require Redis."""
        apply_committed_baseline_env(monkeypatch)
        monkeypatch.setenv("TASK_DISPATCHER", "inline")
        from src.core.config import get_settings

        assert isinstance(build_dispatcher(get_settings(), pool=None), InlineDispatcher)

    @pytest.mark.asyncio
    async def test_the_pool_is_built_once_on_first_use_and_cached(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Drive the real dependency against a real Redis, twice, and get the same pool.

        This is the clause that makes `arq_pool` genuinely wiring-tested rather than merely
        declared: `_arq_pool` is what the production dependency calls, `ArqDispatcher` is what the
        route must end up holding, and the second call must reuse the first pool rather than open a
        connection per request.
        """
        # `require_capability` is an unconditional skip-locally / fail-in-CI, so it is called ONLY
        # once the resource is known to be missing. Calling it first would fail every run.
        redis_url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
        if not redis_url:
            require_capability("redis", "the task pool IS a Redis connection; this needs a real one")

        apply_committed_baseline_env(monkeypatch)
        monkeypatch.setenv("TASK_DISPATCHER", "arq")
        monkeypatch.setenv("REDIS_URL", redis_url)

        from src.main import create_app
        from src.policies.routes import _arq_pool

        app: FastAPI = create_app()
        async with LifespanManager(app):
            settings = app.state.settings

            class _Request:
                """The one attribute `_arq_pool` reads. A Starlette Request cannot be built here."""

                def __init__(self, application: FastAPI) -> None:
                    self.app = application

            request = _Request(app)
            first = await _arq_pool(request, settings)  # type: ignore[arg-type]
            assert first is not None, "a reachable Redis must yield a pool"
            assert app.state.arq_pool is first, "the pool must be cached on the composition"

            second = await _arq_pool(request, settings)  # type: ignore[arg-type]
            assert second is first, "a second request must reuse the pool, not open another"

            # And the dispatcher the route would hand to PolicyBundleService is the real one.
            assert isinstance(build_dispatcher(settings, pool=first), ArqDispatcher)
