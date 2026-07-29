# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for src/core/logging.py and src/core/errors.py.

P-09 focused examples: redaction, field presence, status/content-type equality,
validation pointers, generic 500 details.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.core.errors import (
    PROBLEM_CONTENT_TYPE,
    TYPE_BASE,
    ProblemException,
    install_problem_handlers,
)
from src.core.logging import redact_secrets
from starlette.exceptions import HTTPException as StarletteHTTPException

from tests.synthetic_secrets import pem_header


class TestSecretRedaction:
    """SecretRedactingFilter scrubs known patterns."""

    def test_redacts_bearer_token(self):
        result = redact_secrets("Authorization: Bearer test-only-not-a-real-secret.not-a-jwt")
        assert "test-only-not-a-real-secret" not in result
        assert "[REDACTED]" in result

    def test_redacts_postgresql_url(self):
        result = redact_secrets("connecting to postgresql+asyncpg://user:secret@host:5432/db")
        assert "user:secret" not in result
        assert "[REDACTED]" in result

    def test_redacts_redis_url(self):
        result = redact_secrets("cache at redis://secret@redis:6379/0")
        assert "secret" not in result
        assert "[REDACTED]" in result

    def test_redacts_openai_key(self):
        result = redact_secrets("using key sk-proj1234567890abcdefghij")
        assert "sk-proj1234567890abcdefghij" not in result
        assert "[REDACTED]" in result

    def test_redacts_anthropic_key(self):
        result = redact_secrets("key is sk-ant-1234567890abcdefghijklmnop")
        assert "sk-ant-1234567890abcdefghijklmnop" not in result

    def test_redacts_pem_material(self):
        result = redact_secrets("cert: " + pem_header("PRIVATE"))
        assert "PRIVATE KEY" not in result

    def test_safe_text_unchanged(self):
        safe = "Normal log message about a request"
        assert redact_secrets(safe) == safe


class TestProblemHandlers:
    """RFC 9457 error rendering tests."""

    @pytest.fixture()
    def app(self) -> FastAPI:
        app = FastAPI()
        install_problem_handlers(app)

        @app.get("/raise-problem")
        async def _raise_problem():
            raise ProblemException(
                status=409,
                type_suffix="conflict",
                title="Resource conflict",
                detail="The resource already exists.",
            )

        @app.get("/raise-validation")
        async def _raise_validation():
            from pydantic import BaseModel

            class M(BaseModel):
                name: str

            M.model_validate({"wrong": "field"})  # This won't trigger FastAPI validation

        @app.get("/raise-http")
        async def _raise_http():
            raise StarletteHTTPException(status_code=404, detail="Not found here")

        @app.get("/raise-unhandled")
        async def _raise_unhandled():
            raise RuntimeError("Something broke: postgresql+asyncpg://user:pass@host/db")

        @app.get("/raise-bearer-leak")
        async def _raise_bearer_leak():
            raise RuntimeError("Failed with Bearer test-only-not-a-real-secret.not-a-jwt")

        return app

    @pytest.fixture()
    def client(self, app: FastAPI) -> TestClient:
        return TestClient(app, raise_server_exceptions=False)

    def test_problem_exception_content_type(self, client: TestClient):
        """ProblemException produces application/problem+json."""
        r = client.get("/raise-problem")
        assert r.status_code == 409
        assert PROBLEM_CONTENT_TYPE in r.headers["content-type"]

    def test_problem_exception_status_matches(self, client: TestClient):
        """body.status must equal the HTTP status code (P-09)."""
        r = client.get("/raise-problem")
        body = r.json()
        assert body["status"] == 409
        assert body["status"] == r.status_code

    def test_problem_exception_has_required_fields(self, client: TestClient):
        """type, title, status must always be present."""
        r = client.get("/raise-problem")
        body = r.json()
        assert "type" in body
        assert "title" in body
        assert "status" in body
        assert body["type"] == f"{TYPE_BASE}/conflict"

    def test_problem_exception_has_instance(self, client: TestClient):
        """instance = request path."""
        r = client.get("/raise-problem")
        body = r.json()
        assert body["instance"] == "/raise-problem"

    def test_http_exception_renders_as_problem(self, client: TestClient):
        """StarletteHTTPException produces RFC 9457."""
        r = client.get("/raise-http")
        assert r.status_code == 404
        assert PROBLEM_CONTENT_TYPE in r.headers["content-type"]
        body = r.json()
        assert body["status"] == 404
        assert body["title"] == "Not Found"

    def test_unhandled_exception_generic_500(self, client: TestClient):
        """Unhandled exceptions produce generic 500 — no connection string leaked."""
        r = client.get("/raise-unhandled")
        assert r.status_code == 500
        body = r.json()
        assert body["status"] == 500
        assert "postgresql" not in json.dumps(body)
        assert body["detail"] == "An unexpected error occurred. Quote the trace_id when reporting this."

    def test_unhandled_exception_no_bearer_leak(self, client: TestClient):
        """Bearer tokens must never appear in problem detail."""
        r = client.get("/raise-bearer-leak")
        assert r.status_code == 500
        body = r.json()
        assert "Bearer" not in json.dumps(body)
        assert "test-only-not-a-real-secret" not in json.dumps(body)

    def test_problem_status_equals_http_status(self, client: TestClient):
        """For every rendered problem: body status == HTTP status."""
        for path, expected_status in [
            ("/raise-problem", 409),
            ("/raise-http", 404),
            ("/raise-unhandled", 500),
        ]:
            r = client.get(path)
            body = r.json()
            assert body["status"] == r.status_code == expected_status, (
                f"Status mismatch at {path}: body={body['status']}, http={r.status_code}"
            )
