# SPDX-License-Identifier: FSL-1.1-ALv2
"""The device read surface (design.md §3.1, §3.7; criterion 10 step 4).

Pairing was write-only: a POST to mint a code, a public POST to exchange one, a DELETE to revoke, and
no GET at all. These tests pin the three properties that matter about the read surface now that it
exists — it is authenticated, it never returns credential columns, and it distinguishes "never
reported" from "stale".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from src.auth.dependencies import require_principal
from src.auth.device_models import DeviceStatus
from src.auth.models import UserRole
from src.auth.principal import Principal

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

DEVICES_PATH = "/api/v1/agents/devices"
TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
USER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

#: Columns on `agent_devices` that must never cross the wire. Two are HMACs of bearer credentials
#: and the third is a wrapped key, so a read surface returning any of them turns "list my devices"
#: into credential exfiltration.
SECRET_COLUMNS = ("pairing_token_hmac", "device_token_hmac", "envelope_key_enc")


def _principal() -> Principal:
    return Principal.for_user(
        user_id=USER,
        subject="devices-test",
        email="operator@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=TENANT,
    )


@pytest_asyncio.fixture
async def app_no_auth(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    from src.main import create_app
    from tests.integration.production_app import apply_committed_baseline_env

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app()
    async with LifespanManager(app):
        yield app


class TestItExistsAtAll:
    """There was no GET on this router, which is why step 4 had nothing to assert against."""

    async def test_both_read_routes_are_registered(self, app_no_auth: Any) -> None:
        paths = app_no_auth.openapi()["paths"]
        assert DEVICES_PATH in paths
        assert "get" in paths[DEVICES_PATH]
        assert "get" in paths[f"{DEVICES_PATH}/{{device_id}}"]

    async def test_the_write_routes_are_untouched(self, app_no_auth: Any) -> None:
        paths = app_no_auth.openapi()["paths"]
        # The read surface is additive: minting, exchanging and revoking are unchanged.
        assert "post" in paths["/api/v1/agents/pairing-codes"]
        assert "post" in paths["/api/v1/agents/pair/exchange"]
        assert "delete" in paths["/api/v1/agents/{device_id}"]


class TestDenyByDefault:
    @pytest.mark.parametrize("path", [DEVICES_PATH, f"{DEVICES_PATH}/{uuid.uuid4()}"])
    async def test_it_refuses_an_unauthenticated_caller(self, app_no_auth: Any, path: str) -> None:
        transport = ASGITransport(app=app_no_auth)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(path)
        assert response.status_code == 401

    async def test_neither_read_route_is_public(self) -> None:
        from src.auth.public_routes import is_public

        assert not is_public(DEVICES_PATH, "GET")
        assert not is_public(f"{DEVICES_PATH}/{uuid.uuid4()}", "GET")
        # The exchange remains the one public route in Phase 1, and this must not have widened it.
        assert is_public("/api/v1/agents/pair/exchange", "POST")


class TestNoCredentialColumnsAreExposed:
    def test_the_response_model_has_no_secret_field(self) -> None:
        from src.auth.device_read_routes import DeviceRead

        for column in SECRET_COLUMNS:
            assert column not in DeviceRead.model_fields, f"{column} must not be on the wire"

    def test_the_select_list_names_no_secret_column(self) -> None:
        from src.auth.device_read_routes import _COLUMNS

        # Asserted on the SELECT list rather than only on the model, because `SELECT *` plus a
        # narrow model would still pull secrets into the process — and a later migration adding a
        # secret column would silently start including it.
        for column in SECRET_COLUMNS:
            assert column not in _COLUMNS

    async def test_the_schema_advertises_no_secret_field(self, app_no_auth: Any) -> None:
        schema = app_no_auth.openapi()["components"]["schemas"]["DeviceRead"]
        for column in SECRET_COLUMNS:
            assert column not in schema["properties"]


class TestTheHeartbeatIsTriState:
    """`None` means never reported. `False` means reported and stale. They are not the same.

    `AgentPairing.tsx` displayed "Connected & Attested" with no props and no fetch, so this
    distinction is the substance of the fix rather than a nicety: a boolean would force "never
    reported" to render as either connected or disconnected, and both would be assertions about
    something never observed.
    """

    def _row(self, **overrides: Any) -> dict[str, Any]:
        row = {
            "id": uuid.uuid4(),
            "project_id": uuid.uuid4(),
            "status": DeviceStatus.ACTIVE,
            "agent_version": "0.4.1",
            "platform": "linux/amd64",
            "cert_serial": None,
            "cert_fingerprint": None,
            "cert_not_after": None,
            "last_seq": 0,
            "last_seen": None,
            "pairing_expires_at": None,
            "revoked_at": None,
            "created_at": datetime.now(UTC),
        }
        row.update(overrides)
        return row

    def test_a_device_never_seen_reports_none_not_false(self) -> None:
        from src.auth.device_read_routes import _to_read

        read = _to_read(self._row(last_seen=None), timeout_seconds=90)
        assert read.heartbeat_fresh is None
        assert read.seconds_since_last_seen is None

    def test_a_recent_heartbeat_is_fresh(self) -> None:
        from src.auth.device_read_routes import _to_read

        read = _to_read(self._row(last_seen=datetime.now(UTC) - timedelta(seconds=5)), timeout_seconds=90)
        assert read.heartbeat_fresh is True
        assert 0 <= (read.seconds_since_last_seen or -1) <= 10

    def test_an_old_heartbeat_is_stale(self) -> None:
        from src.auth.device_read_routes import _to_read

        read = _to_read(self._row(last_seen=datetime.now(UTC) - timedelta(seconds=600)), timeout_seconds=90)
        assert read.heartbeat_fresh is False
        assert (read.seconds_since_last_seen or 0) >= 590

    def test_the_boundary_is_inclusive_and_reported(self) -> None:
        from src.auth.device_read_routes import _to_read

        read = _to_read(self._row(last_seen=datetime.now(UTC) - timedelta(seconds=90)), timeout_seconds=90)
        # Exactly at the timeout counts as fresh, and the threshold travels with the judgement so a
        # client renders the server's decision rather than inventing its own.
        assert read.heartbeat_fresh is True
        assert read.heartbeat_timeout_seconds == 90

    def test_a_naive_timestamp_is_treated_as_utc_rather_than_local(self) -> None:
        from src.auth.device_read_routes import _to_read

        naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10)
        read = _to_read(self._row(last_seen=naive), timeout_seconds=90)
        # The column is `timestamptz` so this should not arise, but assuming local time would make
        # freshness wrong by the server's UTC offset — silently, and only outside UTC.
        assert read.heartbeat_fresh is True
        assert (read.seconds_since_last_seen or 0) < 60


class TestStatusFilteringUsesTheStateMachine:
    async def test_the_filter_is_typed_to_the_five_states(self, app_no_auth: Any) -> None:
        spec = app_no_auth.openapi()
        enum_schema = spec["components"]["schemas"]["DeviceStatus"]
        assert set(enum_schema["enum"]) == {"pending", "active", "policy_stale", "revoked", "abandoned"}

    async def test_an_unknown_status_is_a_validation_failure_not_an_empty_list(
        self, app_no_auth: Any
    ) -> None:
        app_no_auth.dependency_overrides[require_principal] = _principal
        try:
            transport = ASGITransport(app=app_no_auth)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get(f"{DEVICES_PATH}?status=not_a_state")
            # An empty inventory and an invalid filter are different answers, and only one of them
            # should look like "you have no devices".
            assert response.status_code == 422, response.text
        finally:
            app_no_auth.dependency_overrides.clear()
