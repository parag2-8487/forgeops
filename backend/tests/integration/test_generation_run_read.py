# SPDX-License-Identifier: FSL-1.1-ALv2
"""A generation run must be able to show the instruction its model was given.

The row recorded the tier, the endpoint, the token counts, the retrieval record and the outcome —
everything except what the model was asked to do. So when a run wrote a file to the wrong path there was
no way to tell whether the model had disobeyed a correct instruction or obeyed a bad one, and those two
faults have opposite fixes: one is a prompt defect, the other a model or parser defect.

The `compiled_prompt` column was added with the compiler and NOTHING READ IT. A column written and never
read is not evidence, it is storage, and it fails silently — the write can rot for months and every test
still passes. These tests are the reader.

They also pin the disclosure rule: a run belonging to another tenant and a run that does not exist answer
IDENTICALLY, because a distinguishable 404 makes this an enumeration oracle for run ids.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from src.auth.dependencies import require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal

pytestmark = [pytest.mark.mandatory, pytest.mark.asyncio]

COMPILED = (
    "## 1. ESTABLISHED FACTS ABOUT THIS REPOSITORY\n"
    "- language: python (from requirements.txt)\n\n"
    "## 2. WHAT TO PRODUCE\n"
    "### MODIFY `Dockerfile`\n"
    "- line 3: the image runs as root\n"
)


@pytest_asyncio.fixture
async def app_no_auth(monkeypatch: pytest.MonkeyPatch, database_url: str) -> AsyncIterator[Any]:
    """The REAL app under its committed baseline env, with authentication left in place.

    Only the principal dependency is overridden per test, so what is proved is which principal a route
    accepts and what it discloses — not whether the route runs at all.

    The baseline env names the compose hostnames, which do not resolve from the host, so the database and
    cache are pointed at the test containers. That is an ADDRESS substitution and nothing else: this is a
    real PostgreSQL with pgvector and a real Redis, because a route whose disclosure rule was proved
    against a stub would be proved against the stub's behaviour rather than the database's.
    """
    from src.main import create_app

    from tests.integration.production_app import apply_committed_baseline_env

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", os.environ.get("FORGEOPS_TEST_REDIS_URL", "redis://127.0.0.1:26379/0"))
    app = create_app()
    async with LifespanManager(app):
        yield app


def _principal(tenant_id: uuid.UUID) -> Principal:
    """An ordinary signed-in developer, deliberately NOT an admin.

    Reading back the prompt for a run you asked for is not a privileged operation, and proving it only
    for an admin would leave the common case unproven.
    """
    return Principal.for_user(
        user_id=uuid.uuid4(),
        subject=f"read-run-{uuid.uuid4()}",
        email="developer@forgeops.invalid",
        role=UserRole.DEVELOPER,
        tenant_id=tenant_id,
    )


async def _seed_run(app: Any, *, tenant_id: uuid.UUID, compiled: str | None) -> uuid.UUID:
    """Insert one project and one finished run, returning the run id."""
    run_id = uuid.uuid4()
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with app.state.sessionmaker() as session:
        # A REAL user row, because `generation_runs.requested_by` is a foreign key. Satisfying it with a
        # bare uuid would mean the insert under test differs from the insert the route performs.
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, name, role, idp_subject, is_active) "
                "VALUES (:i, :t, :e, 'Prompt Reader', 'developer', :s, true)"
            ),
            {
                "i": user_id,
                "t": tenant_id,
                "e": f"reader-{user_id}@forgeops.invalid",
                "s": f"reader-{user_id}",
            },
        )
        await session.execute(
            text("INSERT INTO projects (id, name, path) VALUES (:i, :n, :p)"),
            {"i": project_id, "n": "prompt-read", "p": "/tmp/prompt-read"},
        )
        await session.execute(
            text(
                "INSERT INTO generation_runs (id, project_id, tenant_id, requested_by, status, "
                "iterations_used, served_from, tier, endpoint_id, prompt_tokens, "
                "completion_tokens, compiled_prompt, prompt_token_estimate, prompt_token_budget, "
                "addressed_checks, deferred_checks) "
                "VALUES (:i, :p, :t, :r, 'accepted', 1, 'provider', 'balanced', 'primary', 120, "
                "340, :c, 900, 24000, CAST(:a AS jsonb), CAST(:d AS jsonb))"
            ),
            {
                "i": run_id,
                "p": project_id,
                "t": tenant_id,
                "r": user_id,
                "c": compiled,
                "a": '["dockerfile_runs_as_non_root"]',
                "d": '["kubernetes_probes_declared"]',
            },
        )
        await session.commit()
    return run_id


async def _client(app: Any, principal: Principal) -> AsyncClient:
    app.dependency_overrides[require_principal] = lambda: principal
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_the_exact_prompt_the_model_received_is_readable(app_no_auth: Any) -> None:
    """Byte-for-byte, because a paraphrase cannot be used to diagnose a bad instruction."""
    tenant = uuid.uuid4()
    run_id = await _seed_run(app_no_auth, tenant_id=tenant, compiled=COMPILED)
    async with await _client(app_no_auth, _principal(tenant)) as client:
        response = await client.get(f"/api/v1/generation/runs/{run_id}")
    assert response.status_code == 200
    assert response.json()["compiled_prompt"] == COMPILED


async def test_the_budget_is_reported_beside_the_estimate(app_no_auth: Any) -> None:
    """A prompt that dropped a section is only explicable next to the limit that forced the drop."""
    tenant = uuid.uuid4()
    run_id = await _seed_run(app_no_auth, tenant_id=tenant, compiled=COMPILED)
    async with await _client(app_no_auth, _principal(tenant)) as client:
        body = (await client.get(f"/api/v1/generation/runs/{run_id}")).json()
    assert body["prompt_token_estimate"] == 900
    assert body["prompt_token_budget"] == 24000


async def test_deferred_checks_are_distinguished_from_addressed_ones(app_no_auth: Any) -> None:
    """A user looking at an unchanged score must learn a check was never attempted.

    Without this the two failures are indistinguishable: "the model tried and produced something the
    validator rejected" and "the budget could not hold this check at all" look the same from outside,
    and only the second is fixed by raising the budget or narrowing the request.
    """
    tenant = uuid.uuid4()
    run_id = await _seed_run(app_no_auth, tenant_id=tenant, compiled=COMPILED)
    async with await _client(app_no_auth, _principal(tenant)) as client:
        body = (await client.get(f"/api/v1/generation/runs/{run_id}")).json()
    assert body["addressed_checks"] == ["dockerfile_runs_as_non_root"]
    assert body["deferred_checks"] == ["kubernetes_probes_declared"]


async def test_a_run_with_no_recorded_prompt_says_so_rather_than_showing_nothing(
    app_no_auth: Any,
) -> None:
    """None means "not recorded". An empty string would claim the model was sent nothing."""
    tenant = uuid.uuid4()
    run_id = await _seed_run(app_no_auth, tenant_id=tenant, compiled=None)
    async with await _client(app_no_auth, _principal(tenant)) as client:
        body = (await client.get(f"/api/v1/generation/runs/{run_id}")).json()
    assert body["compiled_prompt"] is None


async def test_another_tenants_run_is_refused(app_no_auth: Any) -> None:
    """The prompt quotes the repository's own files, so this is a content leak, not only metadata."""
    run_id = await _seed_run(app_no_auth, tenant_id=uuid.uuid4(), compiled=COMPILED)
    async with await _client(app_no_auth, _principal(uuid.uuid4())) as client:
        response = await client.get(f"/api/v1/generation/runs/{run_id}")
    assert response.status_code == 403


async def test_a_missing_run_is_indistinguishable_from_a_forbidden_one(app_no_auth: Any) -> None:
    """Byte-identical, or the difference enumerates which run ids exist."""
    tenant = uuid.uuid4()
    other = await _seed_run(app_no_auth, tenant_id=uuid.uuid4(), compiled=COMPILED)
    async with await _client(app_no_auth, _principal(tenant)) as client:
        forbidden = await client.get(f"/api/v1/generation/runs/{other}")
        absent = await client.get(f"/api/v1/generation/runs/{uuid.uuid4()}")
    assert forbidden.status_code == absent.status_code == 403
    # `instance` echoes the caller's OWN request URL and `trace_id` is per-request, so neither can carry
    # information about the run. Everything a caller could compare between two responses must match.
    disclosing = ("type", "title", "status", "detail")
    assert {k: forbidden.json().get(k) for k in disclosing} == {k: absent.json().get(k) for k in disclosing}


async def test_an_anonymous_caller_cannot_read_a_prompt(app_no_auth: Any) -> None:
    """No dependency override: the route's real authentication decides."""
    tenant = uuid.uuid4()
    run_id = await _seed_run(app_no_auth, tenant_id=tenant, compiled=COMPILED)
    async with AsyncClient(transport=ASGITransport(app=app_no_auth), base_url="http://test") as client:
        response = await client.get(f"/api/v1/generation/runs/{run_id}")
    assert response.status_code in (401, 403)
