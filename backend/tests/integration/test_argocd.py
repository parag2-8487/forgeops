# SPDX-License-Identifier: FSL-1.1-ALv2
"""The ArgoCD webhook and the governed sync. Phase 2 §2.7.

THE ASSERTION THIS FILE EXISTS FOR: the webhook does not sync.

An unauthenticated HTTP request causing a production deployment is a hole straight through everything §3
exists to do. The obvious implementation has exactly that shape, so the test suite has to pin the choice —
otherwise a later change that "makes the webhook useful" would look like an improvement.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

SECRET = "a-webhook-secret-for-this-test-only"


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(repository: str = "https://github.com/acme/api", after: str = "abc123") -> bytes:
    return json.dumps({"repository": {"html_url": repository}, "after": after}).encode()


def _app(sessions: Any, secret: str, monkeypatch: Any) -> Any:
    """The argocd router alone, over the REAL database session and the REAL settings.

    Mounting one router rather than using `production_app` because that fixture deliberately points at
    an unreachable database -- it exists to prove the composition root builds without I/O. These tests
    need rows read back, so the session dependency is overridden to the real factory. That is a
    TRANSPORT substitution, not a collaborator one: the route, the signature check and the SQL are all
    the production ones.
    """
    from fastapi import FastAPI
    from src.argocd.routes import router
    from src.auth.dependencies import require_principal
    from src.auth.principal import Principal
    from src.core.db import get_session
    from src.core.errors import install_problem_handlers

    # THE SECRET GOES INTO THE ENVIRONMENT, not a dependency override. The route reads
    # `get_settings()` directly, so overriding the dependency does nothing -- which the first version
    # of this harness discovered by getting a 503 where it expected a 202.
    monkeypatch.setenv("ARGOCD_WEBHOOK_LOGIN_SECRET", secret)

    app = FastAPI()
    # THE REAL PROBLEM HANDLERS. Without them a `ProblemException` escapes as a 500 and every assertion
    # about a status code would be testing Starlette's default error page rather than the RFC 9457
    # contract the route is written against.
    install_problem_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def _session() -> Any:
        async with sessions() as session:
            yield session

    def _principal() -> Principal:
        # `for_user` and not the constructor: blast radius is DERIVED from the role there, and a test
        # that built one by hand would be asserting against a principal shape production cannot make.
        return Principal.for_user(
            user_id=uuid.uuid4(),
            subject="test-subject",
            email="tester@example.com",
            role="developer",
        )

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_principal] = _principal
    return app


async def _post(
    app: Any, path: str, *, content: bytes | None = None, json_body: Any = None, headers: dict[str, str] | None = None
) -> Any:
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        if json_body is not None:
            return await client.post(path, json=json_body, headers=headers or {})
        return await client.post(path, content=content or b"", headers=headers or {})


class TestTheWebhookRecordsAndDoesNotSync:
    async def test_a_signed_payload_is_recorded_and_says_nothing_was_synced(
        self, sessions: Any, monkeypatch: Any
    ) -> None:
        payload = _payload()
        response = await _post(
            _app(sessions, SECRET, monkeypatch),
            "/api/v1/argocd/webhook",
            content=payload,
            headers={"X-Hub-Signature-256": _signature(SECRET, payload)},
        )
        assert response.status_code == 202, response.text
        detail = response.json()["detail"]
        # THE WORDS ARE LOAD-BEARING. A client that believed this triggered a deployment would stop
        # waiting for the human step that actually performs one.
        assert "NOTHING WAS SYNCED" in detail
        assert "approval chokepoint" in detail

        async with sessions() as session:
            rows = (
                (await session.execute(text("SELECT repository, revision FROM argocd_repository_events")))
                .mappings()
                .all()
            )
        assert len(rows) == 1
        assert rows[0]["repository"] == "https://github.com/acme/api"
        assert rows[0]["revision"] == "abc123"

    async def test_no_change_set_is_created_by_a_webhook(self, sessions: Any, monkeypatch: Any) -> None:
        """The structural half of the claim: a webhook produces no governance record, because it mutates
        nothing. If a later change made it sync, this is the test that fails.
        """
        async with sessions() as session:
            before = (await session.execute(text("SELECT count(*) AS n FROM change_sets"))).scalar_one()

        payload = _payload()
        await _post(
            _app(sessions, SECRET, monkeypatch),
            "/api/v1/argocd/webhook",
            content=payload,
            headers={"X-Hub-Signature-256": _signature(SECRET, payload)},
        )

        async with sessions() as session:
            after = (await session.execute(text("SELECT count(*) AS n FROM change_sets"))).scalar_one()
        assert after == before, (
            "the webhook created a change set, which means it performed a mutation from an unauthenticated request"
        )

    async def test_an_unsigned_payload_is_refused(self, sessions: Any, monkeypatch: Any) -> None:
        response = await _post(_app(sessions, SECRET, monkeypatch), "/api/v1/argocd/webhook", content=_payload())
        assert response.status_code == 403

    async def test_a_wrongly_signed_payload_is_refused(self, sessions: Any, monkeypatch: Any) -> None:
        payload = _payload()
        response = await _post(
            _app(sessions, SECRET, monkeypatch),
            "/api/v1/argocd/webhook",
            content=payload,
            headers={"X-Hub-Signature-256": _signature("the-wrong-secret", payload)},
        )
        assert response.status_code == 403

    async def test_a_modified_body_with_a_valid_old_signature_is_refused(self, sessions: Any, monkeypatch: Any) -> None:
        """The signature covers the BODY, so a replayed signature over different content fails.

        Without this, anybody who observed one legitimate delivery could substitute any repository name
        and have it recorded in somebody else's history.
        """
        original = _payload("https://github.com/acme/api")
        tampered = _payload("https://github.com/attacker/evil")
        response = await _post(
            _app(sessions, SECRET, monkeypatch),
            "/api/v1/argocd/webhook",
            content=tampered,
            headers={"X-Hub-Signature-256": _signature(SECRET, original)},
        )
        assert response.status_code == 403

    async def test_with_no_secret_configured_everything_is_refused(self, sessions: Any, monkeypatch: Any) -> None:
        """Absent capability rather than open door, and the message names the setting."""
        payload = _payload()
        response = await _post(
            _app(sessions, "", monkeypatch),
            "/api/v1/argocd/webhook",
            content=payload,
            headers={"X-Hub-Signature-256": _signature(SECRET, payload)},
        )
        assert response.status_code == 503
        assert "ARGOCD_WEBHOOK_LOGIN_SECRET" in response.text

    async def test_a_payload_naming_no_repository_is_refused(self, sessions: Any, monkeypatch: Any) -> None:
        """Recording it would put an entry in the history that matches no Application."""
        payload = json.dumps({"zen": "a ping event"}).encode()
        response = await _post(
            _app(sessions, SECRET, monkeypatch),
            "/api/v1/argocd/webhook",
            content=payload,
            headers={"X-Hub-Signature-256": _signature(SECRET, payload)},
        )
        assert response.status_code == 422


class TestTheRenderRouteIsARead:
    async def test_rendering_creates_no_change_set(self, sessions: Any, monkeypatch: Any) -> None:
        """A manifest is a file the operator commits. Generating one mutates nothing."""
        project_id = uuid.uuid4()
        async with sessions() as session:
            before = (await session.execute(text("SELECT count(*) AS n FROM change_sets"))).scalar_one()

        response = await _post(
            _app(sessions, SECRET, monkeypatch),
            f"/api/v1/projects/{project_id}/argocd/manifests",
            json_body={
                "kind": "application",
                "app_name": "api",
                "repo_url": "https://github.com/acme/api",
                "target_revision": "v1.0.0",
                "path": "k8s/production",
                "destination_namespace": "api-production",
            },
        )
        assert response.status_code == 200, response.text
        rendered = response.json()
        assert rendered["filename"].endswith("api.yaml")
        # PARSED, NOT SEARCHED. The rendered file CONTAINS the word `automated` in a comment explaining
        # how to enable it, so a substring assertion fails for the wrong reason -- which is exactly the
        # mistake the renderer tests warn about, made here and caught here.
        import yaml

        document = yaml.safe_load(rendered["content"])
        assert "automated" not in document["spec"]["syncPolicy"], (
            "the route turned automation on, which applies Git to the cluster with no human in the loop"
        )
        assert rendered["prerequisites"], "a manifest with unstated prerequisites fails silently in a cluster"

        async with sessions() as session:
            after = (await session.execute(text("SELECT count(*) AS n FROM change_sets"))).scalar_one()
        assert after == before

    async def test_enabling_automation_states_what_it_means(self, sessions: Any, monkeypatch: Any) -> None:
        """Turning Git into the authority bypasses this product's approval gate, and the response says so
        rather than leaving an operator to infer it.
        """
        response = await _post(
            _app(sessions, SECRET, monkeypatch),
            f"/api/v1/projects/{uuid.uuid4()}/argocd/manifests",
            json_body={
                "kind": "application",
                "app_name": "api",
                "repo_url": "https://github.com/acme/api",
                "automated": True,
            },
        )
        assert response.status_code == 200, response.text
        prerequisites = " ".join(response.json()["prerequisites"])
        assert "AUTOMATED SYNC IS ON" in prerequisites
        assert "will not pass through this product's approval chokepoint" in prerequisites

    async def test_an_applicationset_with_no_environments_is_refused(self, sessions: Any, monkeypatch: Any) -> None:
        response = await _post(
            _app(sessions, SECRET, monkeypatch),
            f"/api/v1/projects/{uuid.uuid4()}/argocd/manifests",
            json_body={
                "kind": "applicationset",
                "app_name": "api",
                "repo_url": "https://github.com/acme/api",
                "environments": [],
            },
        )
        assert response.status_code == 422
        assert "looks like a deployment and does nothing" in response.text
