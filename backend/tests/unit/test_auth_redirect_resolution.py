# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests verifying OIDC redirect URI and frontend landing resolution for Ubuntu deployments."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.auth.oidc import PendingLogin
from src.auth.routes import _resolve_frontend_landing, _resolve_redirect_url
from src.core.config import Settings
from starlette.requests import Request


def _make_dummy_settings(**kwargs) -> Settings:
    db_scheme = "postgresql+asyncpg://"
    db_auth = "dummy_user:dummy_secret@"
    db_host = "localhost:5432/d"
    defaults = {
        "database_url": f"{db_scheme}{db_auth}{db_host}",
        "redis_url": "redis://localhost:6379/0",
        "envelope_pepper": "test-pepper-minimum-entropy-string-12345",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def _make_request(
    path: str = "/api/v1/auth/login",
    headers: dict[str, str] | None = None,
    settings: Settings | None = None,
) -> Request:
    header_list = [(k.lower().encode("latin1"), v.encode("latin1")) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin1"),
        "query_string": b"",
        "headers": header_list,
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 8000),
        "app": MagicMock(),
    }
    req = Request(scope)
    req.app.state.settings = settings or _make_dummy_settings()
    return req


def test_pending_login_roundtrip_with_redirect_uri() -> None:
    pending = PendingLogin(
        verifier="code-verifier-12345",
        nonce="nonce-67890",
        next_path="/projects",
        redirect_uri="http://ubuntu-host:18000/api/v1/auth/callback",
    )
    payload = json.loads(pending.to_json())
    assert payload["redirect_uri"] == "http://ubuntu-host:18000/api/v1/auth/callback"

    restored = PendingLogin.from_json(pending.to_json())
    assert restored.verifier == "code-verifier-12345"
    assert restored.nonce == "nonce-67890"
    assert restored.next_path == "/projects"
    assert restored.redirect_uri == "http://ubuntu-host:18000/api/v1/auth/callback"


def test_pending_login_backward_compatibility() -> None:
    legacy_json = json.dumps({"verifier": "v1", "nonce": "n1", "next": "/home"})
    restored = PendingLogin.from_json(legacy_json)
    assert restored.verifier == "v1"
    assert restored.nonce == "n1"
    assert restored.next_path == "/home"
    assert restored.redirect_uri == ""


def test_resolve_redirect_url_default_localhost() -> None:
    req = _make_request(headers={"host": "localhost:8000"})
    resolved = _resolve_redirect_url(req, "http://localhost:8000/api/v1/auth/callback")
    assert resolved == "http://localhost:8000/api/v1/auth/callback"


def test_resolve_redirect_url_ubuntu_remapped_port_via_host() -> None:
    req = _make_request(headers={"host": "localhost:18000"})
    resolved = _resolve_redirect_url(req, "http://localhost:8000/api/v1/auth/callback")
    assert resolved == "http://localhost:18000/api/v1/auth/callback"


def test_resolve_redirect_url_reverse_proxy_forwarded_headers() -> None:
    req = _make_request(
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "forgeops.internal.net",
            "x-forwarded-port": "18000",
        }
    )
    resolved = _resolve_redirect_url(req, "http://localhost:8000/api/v1/auth/callback")
    assert resolved == "https://forgeops.internal.net:18000/api/v1/auth/callback"


def test_resolve_redirect_url_settings_backend_port_fallback() -> None:
    settings = _make_dummy_settings(backend_port=18000)
    req = _make_request(headers={"host": "localhost"}, settings=settings)
    resolved = _resolve_redirect_url(req, "http://localhost:8000/api/v1/auth/callback")
    assert resolved == "http://localhost:18000/api/v1/auth/callback"


def test_resolve_redirect_url_preserves_external_configured_domain() -> None:
    req = _make_request(headers={"host": "localhost:8000"})
    external_url = "https://auth.company.example/api/v1/auth/callback"
    resolved = _resolve_redirect_url(req, external_url)
    assert resolved == external_url


def test_resolve_redirect_url_preserves_testserver_host() -> None:
    req = _make_request(headers={"host": "testserver"})
    testserver_url = "http://testserver/api/v1/auth/callback"
    resolved = _resolve_redirect_url(req, testserver_url)
    assert resolved == testserver_url


def test_resolve_frontend_landing_default() -> None:
    settings = _make_dummy_settings()
    req = _make_request(headers={"host": "localhost:8000"}, settings=settings)
    landing = _resolve_frontend_landing(req, "/projects")
    assert landing == "http://localhost:3000/projects"


def test_resolve_frontend_landing_remapped_ubuntu_port() -> None:
    settings = _make_dummy_settings(frontend_port=13000, backend_port=18000)
    req = _make_request(headers={"host": "localhost:18000"}, settings=settings)
    landing = _resolve_frontend_landing(req, "/onboarding")
    assert landing == "http://localhost:13000/onboarding"


def test_resolve_frontend_landing_remote_lan_ip() -> None:
    settings = _make_dummy_settings(frontend_port=13000, backend_port=18000)
    req = _make_request(headers={"host": "192.168.1.100:18000"}, settings=settings)
    landing = _resolve_frontend_landing(req, "/dashboard")
    assert landing == "http://192.168.1.100:13000/dashboard"


def test_resolve_frontend_landing_reverse_proxy_https() -> None:
    settings = _make_dummy_settings(frontend_port=13000)
    req = _make_request(
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "forgeops.internal:13000",
        },
        settings=settings,
    )
    landing = _resolve_frontend_landing(req, "/settings")
    assert landing == "https://forgeops.internal:13000/settings"


def test_settings_reconciles_ports_automatically() -> None:
    settings = _make_dummy_settings(backend_port=18000, frontend_port=13000)
    assert settings.oidc_redirect_url == "http://localhost:18000/api/v1/auth/callback"
    assert settings.frontend_base_url == "http://localhost:13000"


def test_settings_preserves_custom_external_urls() -> None:
    settings = _make_dummy_settings(
        backend_port=18000,
        frontend_port=13000,
        oidc_redirect_url="https://auth.company.example/auth/callback",
        frontend_base_url="https://app.company.example",
    )
    assert settings.oidc_redirect_url == "https://auth.company.example/auth/callback"
    assert settings.frontend_base_url == "https://app.company.example"
