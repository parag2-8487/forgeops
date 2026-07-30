# SPDX-License-Identifier: FSL-1.1-ALv2
"""The app-factory-derived production fixture (design.md §0.4.1, §7.8, §11.1).

Why this file exists
--------------------
Phase 0 shipped 419 passing backend tests while its MCP gateway could not serve a
single request. `tests/unit/test_mcp_e2e.py` built `AsyncMock(spec=OpaGatewayPolicy)`
and then reassigned the spec'd child (`policy.filter_tools = AsyncMock(...)`).
Reassignment discards `spec`'s signature enforcement, so the doubles implemented
the contract the *caller* wanted while the real collaborators implemented a
different one. Neither type checking nor coverage could see it, because
collaborators arrive by constructor injection and the call sites dispatch
dynamically. `REVIEW-PHASE-0.md` Pass 4 / Pass 8 recorded it; D-23 records it as
the phase's main lesson.

The rule that makes this fixture non-negotiable
-----------------------------------------------
`production_app` may substitute a **transport** — `httpx.MockTransport`, a local
fixture HTTP server, a Redis or Postgres URL pointing at a container — and may
**never** substitute a collaborator object. If a test needs a different
`OpaGatewayPolicy`, the answer is a different OPA policy file, not a different
Python object.

That rule is not merely documented here. `scripts/check-test-doubles.py` rule
`FO-TD004` fails the build on any `Mock` under `tests/integration/**`, so the
prohibition is mechanical rather than aspirational.

The app is built by `create_app()` — the same callable uvicorn runs — and driven
through the real ASGI lifespan, so `app.state` holds the real composition.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

# A port on the loopback interface that nothing listens on. Pointing the two
# infrastructure DSNs here is a *transport* substitution, which §0.4.1 permits and
# which keeps the fixture honest: the lifespan's own best-effort probes then fail
# fast with a connection refusal instead of burning their 2 s timeout, and the
# composition is exercised exactly as it would be during a dependency outage.
#
# `create_app()`'s lifespan is non-destructive by design (§4.4, §11.1): an
# unreachable Postgres or Redis changes readiness, not process liveness. A test
# that needs real data asks for the `postgres` or `redis` capability and overrides
# these values with a container DSN.
_CLOSED_PORT = 1
UNREACHABLE_DATABASE_URL = f"postgresql+asyncpg://forgeops:forgeops@127.0.0.1:{_CLOSED_PORT}/forgeops"
UNREACHABLE_REDIS_URL = f"redis://127.0.0.1:{_CLOSED_PORT}/0"


def apply_committed_baseline_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Put the committed `.env.example` baseline into the environment.

    Since debt D1 was closed (task 2.1) the lifespan builds the model router from
    `config/model-tiers.yaml`, whose `base_url` values are `${VAR}` placeholders that
    `load_tier_config` refuses to leave unexpanded. That refusal is correct — Phase 0
    shipped the literal `${OPENAI_BASE_URL}/chat/completions` to httpx — so the app
    now genuinely requires those variables at startup.

    Sourcing them from `.env.example` rather than hard-coding them here does double
    duty: the fixture stays a *configuration* substitution rather than a collaborator
    substitution, and any key the baseline forgets breaks these tests, which is the
    fresh-clone guarantee §13.3 asks for ("no committed `.env` required,
    `.env.example` supplies every value").
    """
    from src.core.config import load_project_dotenv

    baseline = load_project_dotenv((".env.example",))
    for key, value in baseline.items():
        monkeypatch.setenv(key, value)
    return baseline


@pytest.fixture
async def production_app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    """Build the app through the PRODUCTION factory, substituting only I/O edges.

    Yields the live `FastAPI` instance with its lifespan entered, so
    `app.state` carries every collaborator the composition root created.
    """
    # Import inside the fixture so collection of this module never depends on the
    # application package importing cleanly — a broken import should fail the
    # tests that use the fixture, not every test in the session.
    from src.core.config import get_settings
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", UNREACHABLE_REDIS_URL)
    monkeypatch.setenv("APP_ENV", "test")
    # `get_settings` is not cached, but a stale process-wide cache would silently
    # serve a previous test's configuration; assert the contract rather than
    # assume it, so adding an lru_cache later fails here instead of mysteriously
    # elsewhere.
    assert not hasattr(get_settings, "cache_clear"), (
        "get_settings became cached; production_app must clear it so each test's "
        "environment is the one the app is built from"
    )

    app = create_app()
    async with LifespanManager(app):
        yield app


def composed_state_names(app: FastAPI) -> frozenset[str]:
    """The `app.state` names the real lifespan composed, minus private ones.

    Deviation from the §0.4.1 snippet, recorded deliberately. The design writes
    this as `{k for k in vars(production_app.state) if not k.startswith("_")}`.
    Against Starlette's `State` that expression evaluates to the **empty set**:
    `State.__init__` does `super().__setattr__("_state", state)` and every
    subsequent `setattr` lands inside that one dict, so `vars(state)` is
    `{'_state': {...}}` and the `startswith("_")` filter discards it.

    Taken literally the clause would therefore compare `set() <= covered` and pass
    for any codebase, including one with no wiring tests at all — the same vacuity
    trap §0.4.5 exists to close. The intent in the surrounding prose is
    unambiguous ("every attribute placed on it by the lifespan"), so the intent is
    what is implemented: unwrap the mapping Starlette actually stores. The
    non-emptiness assertion in `test_wiring_coverage.py` keeps this honest if the
    framework's internals ever move again.
    """
    state_mapping = vars(app.state).get("_state", vars(app.state))
    return frozenset(k for k in state_mapping if not k.startswith("_"))


@pytest.fixture
def composed_state_attributes(production_app: FastAPI) -> frozenset[str]:
    """The `app.state` names the real lifespan composed, minus private ones."""
    return composed_state_names(production_app)
