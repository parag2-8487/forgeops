# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for agent discovery and uninstall endpoints (§1.2, §1.3, Phase 1/3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.agents.uninstall_routes import router as uninstall_router
from src.auth.dependencies import require_principal
from src.auth.device_models import DeviceStatus
from src.auth.models import UserRole
from src.auth.principal import Principal
from src.core.db import get_session

TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
USER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
PROJECT = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
DEVICE = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def _principal() -> Principal:
    return Principal.for_user(
        user_id=USER,
        subject="test-user",
        email="test@example.com",
        role=UserRole.ADMIN,
        tenant_id=TENANT,
    )


class FakeMappingResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar(self):
        return len(self._rows)


@pytest.fixture
def mock_session():
    session = AsyncMock()
    return session


@pytest.fixture
def app(mock_session):
    app = FastAPI()
    app.include_router(uninstall_router)

    app.state.settings = MagicMock()
    app.state.settings.agent_release_tag = "v0.1.2"
    app.state.device_service = AsyncMock()

    app.dependency_overrides[require_principal] = lambda: _principal()
    app.dependency_overrides[get_session] = lambda: mock_session

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_installations_unauthenticated():
    from fastapi import HTTPException

    unauthed_app = FastAPI()
    unauthed_app.include_router(uninstall_router)
    mock_verifier = AsyncMock()
    mock_verifier.verify_principal.side_effect = HTTPException(status_code=401, detail="Unauthenticated")
    unauthed_app.state.app_token_verifier = mock_verifier
    client = TestClient(unauthed_app)
    response = client.get("/api/v1/agents/installations")
    assert response.status_code == 401


def test_installations_authenticated_empty(client, mock_session):
    mock_session.execute.return_value = FakeMappingResult([])
    response = client.get("/api/v1/agents/installations")
    assert response.status_code == 200
    data = response.json()
    assert "installations" in data
    assert data["release_tag"] == "v0.1.2"


def test_installations_returns_registered_devices(client, mock_session):
    mock_session.execute.return_value = FakeMappingResult(
        [
            {
                "id": str(DEVICE),
                "project_id": str(PROJECT),
                "status": DeviceStatus.ACTIVE.value,
                "agent_version": "v0.1.1",
                "platform": "windows",
                "cert_fingerprint": "sha256:1234",
                "last_seen": datetime.now(UTC),
            }
        ]
    )
    response = client.get(f"/api/v1/agents/installations?project_id={PROJECT}")
    assert response.status_code == 200
    data = response.json()
    assert len(data["installations"]) >= 1
    inst = data["installations"][0]
    assert inst["id"] == str(DEVICE)
    assert inst["status"] == DeviceStatus.ACTIVE.value
    assert inst["is_running"] is True


def test_uninstall_dry_run(client, mock_session):
    mock_session.execute.return_value = FakeMappingResult(
        [{"id": str(DEVICE), "project_id": str(PROJECT), "status": DeviceStatus.ACTIVE.value}]
    )
    response = client.post(
        "/api/v1/agents/uninstall",
        json={"dry_run": True, "project_id": str(PROJECT)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert str(DEVICE) in data["stopped_devices"]
    assert str(DEVICE) in data["removed_devices"]
    # Verify device_service.revoke was not called in dry run
    client.app.state.device_service.revoke.assert_not_called()


def test_uninstall_actual_revocation(client, mock_session):
    mock_session.execute.return_value = FakeMappingResult(
        [{"id": str(DEVICE), "project_id": str(PROJECT), "status": DeviceStatus.ACTIVE.value}]
    )
    response = client.post(
        "/api/v1/agents/uninstall",
        json={"dry_run": False, "project_id": str(PROJECT), "wipe_credentials": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert str(DEVICE) in data["removed_devices"]
    assert "revoked_tokens" in data["cleaned_entries"]
    # Verify device_service.revoke was called
    client.app.state.device_service.revoke.assert_called_once()
