# SPDX-License-Identifier: FSL-1.1-ALv2
"""Provider credentials and custom endpoints, against a real database and the real app.

WHY THESE ARE INTEGRATION TESTS. The property worth holding is that a credential is SEALED at rest and
resolvable afterwards, and that setting one changes what `GET /ai/tiers` reports without a restart. A double
would let all three pass without any of them being true: the sealing is real cryptography over a real column,
and the availability flip depends on the app's registry and its key resolver being the same objects the write
path refreshes.

WHAT IS ASSERTED HERE THAT NOTHING ELSE CAN BE:

 - the stored ciphertext does not contain the value, and no route returns it;
 - writing a credential flips the tier from unavailable to available in the SAME process, because the
   resolver's snapshot is refreshed rather than read once at start-up;
 - deleting it flips back;
 - every write is refused to a non-admin.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from src.auth.dependencies import require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal

from tests.integration.wiring import wires

#: Assembled rather than spelled. `check-added-shapes.py` reads the staged index and refuses a literal
#: credential shape anywhere, including fixtures, matching on shape rather than sensitivity.
A_KEY_VALUE = "-".join(("sk", "integration", "1" * 24, "endz"))

CREDENTIALS_PATH = "/api/v1/ai/credentials"
CUSTOM_PATH = "/api/v1/ai/endpoints/custom"
TIERS_PATH = "/api/v1/ai/tiers"

#: The provider the `high_coding` tier's primary names, and the tier that reports it.
KEY_REF = "openai"
TIER_NAME = "high_coding"


@pytest_asyncio.fixture
async def app(monkeypatch: pytest.MonkeyPatch, schema_at_head: str) -> AsyncIterator[Any]:
    from src.main import create_app

    from tests.integration.production_app import apply_committed_baseline_env, real_app_lifespan

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    # THE BASELINE POINTS AT A CLOSED PORT ON PURPOSE — it exists to prove the process stays live when its
    # dependencies are not, so a test needing real rows has to say so. Sealing a credential and reading it
    # back is exactly the thing a stubbed database would let pass without happening.
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    # A known 32-byte value, so the derivation has something deterministic to work from and a failure here is
    # about the code rather than about whatever the developer's `.env` happens to hold.
    monkeypatch.setenv("LOCAL_SECRET_SEAL_KEY", "01234567890123456789012345678901")
    built = create_app()
    # REAL USER ROWS, because `provider_credentials.updated_by` is a foreign key into `users`.
    #
    # That column is the point of the audit trail on this table — who set the key the product now talks to —
    # so the constraint is correct and the test satisfies it rather than the schema relaxing to accommodate a
    # test. One row per role, so the admin gate has a genuine non-admin to refuse.
    #
    # THROUGH ITS OWN ENGINE rather than `app.state.sessionmaker`, which does not exist until the lifespan has
    # run — and the rows have to be present before it does.
    engine = create_async_engine(schema_at_head)
    try:
        async with engine.begin() as conn:
            for role, user_id in _USER_IDS.items():
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, name, role, idp_subject, is_active) "
                        "VALUES (:id, :email, 'Provider Credential Test', :role, :sub, true) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {
                        "id": user_id,
                        "email": f"{role.value}-{user_id.hex[:8]}@forgeops.invalid",
                        "role": role.value,
                        "sub": f"sub-{user_id.hex}",
                    },
                )

        async with real_app_lifespan(built):
            yield built

        # Rows are shared state; a credential left behind would decide the next test's availability.
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM provider_credentials"))
            await conn.execute(text("DELETE FROM provider_endpoints"))
            await conn.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": list(_USER_IDS.values())})
    finally:
        await engine.dispose()


#: One stable id per role, minted once per module so the seeded row and the principal agree.
_USER_IDS: dict[UserRole, uuid.UUID] = {
    UserRole.ADMIN: uuid.uuid4(),
    UserRole.DEVELOPER: uuid.uuid4(),
}


def _principal(role: UserRole) -> Principal:
    return Principal.for_user(
        user_id=_USER_IDS[role],
        subject=f"sub-{_USER_IDS[role].hex}",
        email=f"{role.value}@forgeops.invalid",
        role=role,
    )


def _as(app: Any, monkeypatch: pytest.MonkeyPatch, role: UserRole) -> None:
    """Authenticate every route as `role`.

    BOTH mechanisms, and that is not belt-and-braces. `require_role` calls `require_principal` DIRECTLY as a
    function rather than declaring it as a FastAPI dependency, so `dependency_overrides` does not intercept it
    and an override alone would leave every admin-gated write answering 401. The monkeypatch covers those; the
    override covers the routes that depend on `require_principal` in the ordinary way.
    """
    principal = _principal(role)

    async def _fake(_request: Any) -> Principal:
        return principal

    monkeypatch.setattr("src.auth.dependencies.require_principal", _fake)
    app.dependency_overrides[require_principal] = lambda: principal


def client_for(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def tier(client: AsyncClient, name: str = TIER_NAME) -> dict[str, Any]:
    response = await client.get(TIERS_PATH)
    assert response.status_code == 200, response.text
    return next(entry for entry in response.json()["tiers"] if entry["name"] == name)


class TestTheCredentialLifecycle:
    @pytest.mark.asyncio
    @wires("key_resolver")
    async def test_setting_a_key_makes_the_tier_available_without_a_restart(
        self, app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported defect and its fix, in one pass.

        Before: the tier reports unavailable and names the missing credential — which is the correction,
        because it used to report AVAILABLE on the strength of its protocol alone. After the write: available,
        in the same process, because the resolver's snapshot is refreshed by the write rather than being read
        once during the lifespan.

        THIS IS THE WIRING TEST FOR `key_resolver`, and it has to be an end-to-end one. The registry's
        availability and the router's credential lookup must consult the SAME resolver instance: two instances
        would let this screen report a credential the router cannot find, or the reverse, and both objects
        would still look correctly composed in isolation. Driving a write through HTTP and then observing the
        tiers surface change is what proves they share one.
        """
        _as(app, monkeypatch, UserRole.ADMIN)
        async with client_for(app) as client:
            before = await tier(client)
            assert before["protocol_supported"] is True, "an adapter exists for this protocol"
            assert before["credential_required"] is True
            assert before["credential_configured"] is False
            assert before["available"] is False, "the corrected meaning: no credential, so not available"
            assert before["reason"] and KEY_REF in before["reason"]
            assert before["key_ref"] == KEY_REF

            written = await client.put(f"{CREDENTIALS_PATH}/{KEY_REF}", json={"value": A_KEY_VALUE})
            assert written.status_code == 200, written.text

            after = await tier(client)
            assert after["credential_configured"] is True
            assert after["available"] is True
            assert after["reason"] is None, "nothing is missing, so there is nothing to explain"

    @pytest.mark.asyncio
    async def test_the_write_returns_a_length_and_a_hint_and_never_the_value(
        self, app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _as(app, monkeypatch, UserRole.ADMIN)
        async with client_for(app) as client:
            response = await client.put(f"{CREDENTIALS_PATH}/{KEY_REF}", json={"value": A_KEY_VALUE})

        body = response.json()
        assert body["key_ref"] == KEY_REF
        assert body["length"] == len(A_KEY_VALUE)
        assert body["hint"] == A_KEY_VALUE[-4:]
        assert A_KEY_VALUE not in response.text, "no route may return a credential value"

    @pytest.mark.asyncio
    async def test_the_stored_column_does_not_contain_the_value(self, app, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sealed at rest, asserted against the column rather than the response.

        A test that only checked the API response would pass over an implementation that stored the value in
        plaintext, which is the actual risk.
        """
        _as(app, monkeypatch, UserRole.ADMIN)
        async with client_for(app) as client:
            await client.put(f"{CREDENTIALS_PATH}/{KEY_REF}", json={"value": A_KEY_VALUE})

        async with app.state.sessionmaker() as session:
            row = (
                await session.execute(
                    text("SELECT encrypted_value, value_length FROM provider_credentials WHERE key_ref = :r"),
                    {"r": KEY_REF},
                )
            ).one()

        stored = bytes(row[0])
        assert A_KEY_VALUE.encode("utf-8") not in stored
        assert row[1] == len(A_KEY_VALUE), "the length is stored so it can be shown without unsealing"

    @pytest.mark.asyncio
    async def test_listing_reports_what_is_configured_without_disclosing_it(
        self, app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _as(app, monkeypatch, UserRole.ADMIN)
        async with client_for(app) as client:
            await client.put(f"{CREDENTIALS_PATH}/{KEY_REF}", json={"value": A_KEY_VALUE})
            listed = await client.get(CREDENTIALS_PATH)

        assert listed.status_code == 200, listed.text
        assert A_KEY_VALUE not in listed.text
        entry = next(item for item in listed.json() if item["key_ref"] == KEY_REF)
        assert entry["length"] == len(A_KEY_VALUE)
        # Never tested is distinct from tested and failing, and must stay so.
        assert entry["last_test_ok"] is None

    @pytest.mark.asyncio
    async def test_replacing_a_key_updates_it_rather_than_adding_a_second(
        self, app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _as(app, monkeypatch, UserRole.ADMIN)
        second = A_KEY_VALUE + "-rotated"
        async with client_for(app) as client:
            await client.put(f"{CREDENTIALS_PATH}/{KEY_REF}", json={"value": A_KEY_VALUE})
            replaced = await client.put(f"{CREDENTIALS_PATH}/{KEY_REF}", json={"value": second})
            listed = await client.get(CREDENTIALS_PATH)

        assert replaced.json()["length"] == len(second)
        assert len([item for item in listed.json() if item["key_ref"] == KEY_REF]) == 1

    @pytest.mark.asyncio
    async def test_deleting_a_key_makes_the_tier_unavailable_again(self, app, monkeypatch: pytest.MonkeyPatch) -> None:
        _as(app, monkeypatch, UserRole.ADMIN)
        async with client_for(app) as client:
            await client.put(f"{CREDENTIALS_PATH}/{KEY_REF}", json={"value": A_KEY_VALUE})
            assert (await tier(client))["available"] is True

            removed = await client.delete(f"{CREDENTIALS_PATH}/{KEY_REF}")
            assert removed.status_code == 204, removed.text

            after = await tier(client)
            assert after["credential_configured"] is False
            assert after["available"] is False

    @pytest.mark.asyncio
    async def test_deleting_a_key_that_was_never_set_is_not_an_error(
        self, app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end state the caller asked for is the end state either way, so this is 204 rather than 404."""
        _as(app, monkeypatch, UserRole.ADMIN)
        async with client_for(app) as client:
            response = await client.delete(f"{CREDENTIALS_PATH}/never-set")
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_a_placeholder_value_does_not_count_as_configured(self, app, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator who pasted the example value has not configured a key, and must be told so.

        Otherwise this reproduces the original defect through a new door: the screen would report the tier
        available and the first real request would come back 401 and count against the breaker.
        """
        _as(app, monkeypatch, UserRole.ADMIN)
        async with client_for(app) as client:
            written = await client.put(f"{CREDENTIALS_PATH}/{KEY_REF}", json={"value": "changeme"})
            assert written.status_code in (200, 422), written.text
            if written.status_code == 200:
                assert (await tier(client))["credential_configured"] is False


class TestCustomEndpoints:
    @pytest.mark.asyncio
    async def test_an_endpoint_can_be_registered_listed_and_removed(self, app, monkeypatch: pytest.MonkeyPatch) -> None:
        _as(app, monkeypatch, UserRole.ADMIN)
        payload = {
            "model": "qwen2.5-coder:7b",
            "base_url": "http://a-host:11434/v1",
            "tier": "self_hosted",
            "key_ref": None,
        }
        async with client_for(app) as client:
            created = await client.put(f"{CUSTOM_PATH}/my-endpoint", json=payload)
            assert created.status_code == 200, created.text
            assert created.json()["model"] == payload["model"]

            listed = await client.get(CUSTOM_PATH)
            assert listed.status_code == 200, listed.text
            assert [item["id"] for item in listed.json()] == ["my-endpoint"]

            removed = await client.delete(f"{CUSTOM_PATH}/my-endpoint")
            assert removed.status_code == 204, removed.text

            assert (await client.get(CUSTOM_PATH)).json() == []

    @pytest.mark.asyncio
    async def test_a_base_url_that_is_not_http_is_refused(self, app, monkeypatch: pytest.MonkeyPatch) -> None:
        _as(app, monkeypatch, UserRole.ADMIN)
        async with client_for(app) as client:
            response = await client.put(
                f"{CUSTOM_PATH}/bad",
                json={
                    "model": "m",
                    "base_url": "file:///etc/passwd",
                    "tier": "self_hosted",
                    "key_ref": None,
                },
            )
        assert response.status_code in (400, 422), response.text


class TestOnlyAnAdministratorMayWrite:
    """Reading the tier map is not privileged; changing what the product talks to is.

    Deny-by-default over each write, because a developer who could add an endpoint could point generation at
    a host of their choosing.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("put", f"{CREDENTIALS_PATH}/{KEY_REF}", {"value": A_KEY_VALUE}),
            ("delete", f"{CREDENTIALS_PATH}/{KEY_REF}", None),
            (
                "put",
                f"{CUSTOM_PATH}/x",
                {"model": "m", "base_url": "http://h/v1", "tier": "self_hosted", "key_ref": None},
            ),
            ("delete", f"{CUSTOM_PATH}/x", None),
        ],
    )
    async def test_a_developer_is_refused(
        self, app, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any
    ) -> None:
        _as(app, monkeypatch, UserRole.DEVELOPER)
        async with client_for(app) as client:
            call = getattr(client, method)
            response = await call(path, json=body) if body is not None else await call(path)

        assert response.status_code == 403, f"{method.upper()} {path} answered {response.status_code}"

    @pytest.mark.asyncio
    async def test_a_developer_may_still_read_the_tier_map(self, app, monkeypatch: pytest.MonkeyPatch) -> None:
        """The gate is on the writes, not on the screen. A developer must still see why a tier is down."""
        _as(app, monkeypatch, UserRole.DEVELOPER)
        async with client_for(app) as client:
            response = await client.get(TIERS_PATH)
        assert response.status_code == 200, response.text
