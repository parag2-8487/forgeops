# SPDX-License-Identifier: FSL-1.1-ALv2
"""P-09 — RFC 9457 compliance across all error paths.

Property: Every non-2xx response from the ForgeOps backend MUST:
  1. Carry Content-Type: application/problem+json
  2. Have body["status"] == HTTP status code
  3. Contain required fields: type, title, status
  4. Never leak secret patterns in the detail field

This test exercises known error paths and verifies the invariants hold.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.core.errors import PROBLEM_CONTENT_TYPE, ProblemException, install_problem_handlers

from tests.synthetic_secrets import (
    SYNTHETIC_MARKER,
    bearer_clause,
    openai_style_key,
    pem_header,
    postgres_dsn,
    redis_dsn,
)

# Synthetic, self-labelling values used only to prove the sanitiser removes them.
# They are assembled at runtime by tests/synthetic_secrets.py so no source file
# contains a contiguous credential-shaped literal for a scanner to flag; see that
# module's docstring for why (GitGuardian 35267706, gitleaks `private-key`).
LEAKY_DSN = postgres_dsn()
LEAKY_REDIS_DSN = redis_dsn()
LEAKY_BEARER = bearer_clause()
LEAKY_API_KEY = openai_style_key()
LEAKY_PEM_HEADER = pem_header()

# Every literal above must be absent from any emitted problem body.
LEAKY_SUBSTRINGS = (
    SYNTHETIC_MARKER,
    "postgresql+asyncpg://",
    "redis://",
    LEAKY_API_KEY,
    LEAKY_PEM_HEADER,
)

# Routes whose `detail` is built from a credential-bearing string.
LEAKY_ROUTES = [
    "/api/v1/leaky-dsn",
    "/api/v1/leaky-redis",
    "/api/v1/leaky-bearer",
    "/api/v1/leaky-key",
    "/api/v1/leaky-pem",
]

# Patterns that MUST NOT appear in any problem detail or body
_SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"postgresql(?:\+\w+)?://[^\s]+"),
    re.compile(r"redis://[^\s]+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9]{20,}"),
    re.compile(r"xai-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN\s+(?:RSA\s+)?(?:PRIVATE|PUBLIC)\s+KEY-----"),
]

# RFC 9457 required members
_REQUIRED_FIELDS = {"type", "title", "status"}


def _assert_rfc9457_compliant(response: Any, expected_status: int | None = None) -> None:
    """Assert a response complies with RFC 9457 invariants."""
    # 1. Content-Type
    ct = response.headers.get("content-type", "")
    assert PROBLEM_CONTENT_TYPE in ct, f"Expected {PROBLEM_CONTENT_TYPE} in content-type, got: {ct}"

    # 2. Parseable JSON body
    body = response.json()
    assert isinstance(body, dict), f"Expected dict body, got {type(body)}"

    # 3. Required fields present
    missing = _REQUIRED_FIELDS - set(body.keys())
    assert not missing, f"Missing RFC 9457 required fields: {missing}"

    # 4. body.status == HTTP status
    assert body["status"] == response.status_code, f"body.status={body['status']} != HTTP status={response.status_code}"
    if expected_status is not None:
        assert response.status_code == expected_status

    # 5. type must be a URI string
    assert isinstance(body["type"], str)
    assert body["type"].startswith("https://")

    # 6. No secret leakage in the entire JSON dump
    body_text = json.dumps(body)
    for pattern in _SECRET_PATTERNS:
        assert not pattern.search(body_text), f"Secret pattern leaked in response body: {pattern.pattern}"


@pytest.fixture()
def app():
    """Create a test app that mirrors the production error handling."""
    from fastapi import FastAPI
    from starlette.exceptions import HTTPException as StarletteHTTPException

    app = FastAPI()
    install_problem_handlers(app)

    @app.get("/auth/me")
    async def _auth_me():
        """Simulates a missing-auth 401 response."""
        raise StarletteHTTPException(status_code=401, detail="Not authenticated")

    @app.get("/api/v1/servers/{server_id}")
    async def _server_detail(server_id: str):
        """Simulates a not-found 404 response."""
        raise StarletteHTTPException(status_code=404, detail=f"Server '{server_id}' not found")

    @app.get("/api/v1/conflict")
    async def _conflict():
        """Simulates a 409 conflict via ProblemException."""
        raise ProblemException(
            status=409,
            type_suffix="resource-conflict",
            title="Resource conflict",
            detail="The resource version has changed.",
        )

    @app.get("/api/v1/rate-limited")
    async def _rate_limited():
        """Simulates a 429 Too Many Requests."""
        raise StarletteHTTPException(status_code=429, detail="Rate limit exceeded")

    @app.get("/api/v1/crash")
    async def _crash():
        """Simulates an unhandled exception with embedded secrets."""
        raise RuntimeError(
            "Connection failed: postgresql+asyncpg://admin:s3cr3t@db:5432/prod "
            "with token Bearer test-only-not-a-real-secret.not-a-jwt"
        )

    @app.post("/api/v1/analysis/plan")
    async def _plan_analysis():
        """Simulates a validation error (missing body)."""
        from pydantic import BaseModel

        class PlanInput(BaseModel):
            plan_text: str
            format: str

        # Force a validation response by using the model directly
        raise StarletteHTTPException(status_code=422, detail="Validation failed")

    @app.get("/api/v1/forbidden")
    async def _forbidden():
        """Simulates a 403 response."""
        raise StarletteHTTPException(status_code=403, detail="Insufficient permissions")

    # ── Routes whose problem `detail` carries a secret ────────────────────────
    #
    # These exist because P-09's secret clause was previously unreachable: the
    # 500 handler emits a fixed generic detail, so emptying the redaction pattern
    # lists changed nothing and all 13 tests stayed green. A handler that puts a
    # credential-bearing string into `detail` is the realistic leak, and it is
    # the path `_sanitize_detail` exists to guard. Every literal is synthetic and
    # self-labelling per .kiro/steering/secret-safety.md.

    @app.get("/api/v1/leaky-dsn")
    async def _leaky_dsn():
        raise ProblemException(
            status=503,
            type_suffix="dependency-unavailable",
            title="Dependency unavailable",
            detail=f"Could not connect to {LEAKY_DSN}",
        )

    @app.get("/api/v1/leaky-redis")
    async def _leaky_redis():
        raise ProblemException(
            status=503,
            type_suffix="dependency-unavailable",
            title="Dependency unavailable",
            detail=f"Could not connect to {LEAKY_REDIS_DSN}",
        )

    @app.get("/api/v1/leaky-bearer")
    async def _leaky_bearer():
        raise ProblemException(
            status=502,
            type_suffix="upstream-error",
            title="Upstream error",
            detail=f"Upstream rejected {LEAKY_BEARER}",
        )

    @app.get("/api/v1/leaky-key")
    async def _leaky_key():
        raise ProblemException(
            status=502,
            type_suffix="upstream-error",
            title="Upstream error",
            detail=f"Provider rejected the key {LEAKY_API_KEY}",
        )

    @app.get("/api/v1/leaky-pem")
    async def _leaky_pem():
        raise ProblemException(
            status=500,
            type_suffix="internal",
            title="Internal error",
            detail=f"Failed to parse {LEAKY_PEM_HEADER}",
        )

    return app


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class TestP09Rfc9457Compliance:
    """P-09: RFC 9457 compliance for all error responses."""

    # ── Parametrized: known error paths ─────────────────────────────────────

    @pytest.mark.parametrize(
        "method,path,expected_status",
        [
            ("GET", "/auth/me", 401),
            ("GET", "/api/v1/servers/unknown-id", 404),
            ("GET", "/api/v1/conflict", 409),
            ("GET", "/api/v1/rate-limited", 429),
            ("GET", "/api/v1/crash", 500),
            ("GET", "/api/v1/forbidden", 403),
            ("POST", "/api/v1/analysis/plan", 422),
        ],
        ids=[
            "missing-auth-401",
            "unknown-server-404",
            "conflict-409",
            "rate-limited-429",
            "unhandled-crash-500",
            "forbidden-403",
            "validation-422",
        ],
    )
    def test_error_response_rfc9457_compliant(self, client: TestClient, method: str, path: str, expected_status: int):
        """Every error response carries valid RFC 9457 problem details."""
        response = client.request(method, path)
        _assert_rfc9457_compliant(response, expected_status)

    # ── Specific invariant checks ───────────────────────────────────────────

    def test_401_type_uri_is_meaningful(self, client: TestClient):
        """401 type URI should indicate the auth failure category."""
        r = client.get("/auth/me")
        body = r.json()
        assert "unauthorized" in body["type"].lower() or "401" in body["type"]

    def test_404_title_is_human_readable(self, client: TestClient):
        """404 title should be a human-readable phrase, not a code."""
        r = client.get("/api/v1/servers/unknown-id")
        body = r.json()
        assert body["title"] == "Not Found"

    def test_500_never_leaks_connection_strings(self, client: TestClient):
        """500 from a crash with embedded secrets must not leak them."""
        r = client.get("/api/v1/crash")
        body = r.json()
        body_text = json.dumps(body)
        assert "postgresql" not in body_text.lower()
        assert "asyncpg" not in body_text.lower()
        assert "Bearer" not in body_text
        assert "test-only-not-a-real-secret" not in body_text

    def test_500_has_generic_detail(self, client: TestClient):
        """500 detail should be a safe generic message."""
        r = client.get("/api/v1/crash")
        body = r.json()
        assert body["detail"] == "An unexpected error occurred. Quote the trace_id when reporting this."

    def test_status_field_always_integer(self, client: TestClient):
        """status field in the body is always an integer, never a string."""
        for path in ["/auth/me", "/api/v1/servers/x", "/api/v1/crash"]:
            r = client.get(path)
            body = r.json()
            assert isinstance(body["status"], int)

    def test_instance_is_request_path(self, client: TestClient):
        """instance field equals the request URL path."""
        r = client.get("/api/v1/servers/test-123")
        body = r.json()
        assert body["instance"] == "/api/v1/servers/test-123"


class TestP09SecretClauseAtRouteLevel:
    """P-09 clause 4, asserted where it can actually fail.

    The rest of this file exercised routes whose `detail` was already a safe
    constant, so emptying `src.core.errors._LEAK_PATTERNS` left all of them green
    — the secret clause was decorative. These tests drive handlers that put a
    synthetic credential into `detail` and then assert the emitted body is clean,
    so removing the sanitiser fails P-09 rather than passing it.
    """

    @pytest.mark.parametrize("path", LEAKY_ROUTES, ids=[p.rsplit("/", 1)[-1] for p in LEAKY_ROUTES])
    def test_a_credential_in_the_detail_never_reaches_the_body(self, client: TestClient, path: str):
        response = client.get(path)

        assert response.status_code >= 400
        body_text = json.dumps(response.json())

        for pattern in _SECRET_PATTERNS:
            assert not pattern.search(body_text), f"{path}: pattern {pattern.pattern!r} matched the problem body"
        for literal in LEAKY_SUBSTRINGS:
            assert literal not in body_text, f"{path}: {literal!r} leaked into the problem body"

    @pytest.mark.parametrize("path", LEAKY_ROUTES, ids=[p.rsplit("/", 1)[-1] for p in LEAKY_ROUTES])
    def test_the_response_is_still_rfc9457_after_sanitisation(self, client: TestClient, path: str):
        """Suppressing `detail` must not break the required members or status."""
        response = client.get(path)

        _assert_rfc9457_compliant(response)
        body = response.json()
        assert body["status"] == response.status_code
        # `_sanitize_detail` suppresses the whole field rather than masking part
        # of it, so a leaky detail becomes absent — never a partial credential.
        assert body.get("detail") in (None, ""), f"detail survived sanitisation: {body.get('detail')!r}"

    def test_every_error_route_in_this_module_is_secret_free(self, client: TestClient):
        """One sweep over both the safe and the leaky routes."""
        paths = [
            "/auth/me",
            "/api/v1/servers/unknown-id",
            "/api/v1/conflict",
            "/api/v1/rate-limited",
            "/api/v1/crash",
            "/api/v1/forbidden",
            *LEAKY_ROUTES,
        ]
        offenders: list[str] = []
        for path in paths:
            body_text = json.dumps(client.get(path).json())
            if any(p.search(body_text) for p in _SECRET_PATTERNS):
                offenders.append(path)
            elif any(literal in body_text for literal in LEAKY_SUBSTRINGS):
                offenders.append(path)

        assert offenders == [], f"secret material leaked from: {offenders}"
