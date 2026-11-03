# SPDX-License-Identifier: FSL-1.1-ALv2
"""A registry push through the chokepoint. Phase 2 §2.2.

WHAT THESE TESTS ARE FOR. The design claim is that a registry credential reaches the signed envelope and
reaches nothing else — not `change_sets.operation_args`, not an audit row, not a response body. That is a
claim about what is written where, so it is established by driving a real push through the real chokepoint
and READING THE ROWS BACK, which is what caught both of the previous pass's defects.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.governance.chokepoint import DOCKER_IMAGE_OPERATION
from src.secrets.registry_credentials import (
    REGISTRY_CREDENTIAL_ENVIRONMENT,
    REGISTRY_TOKEN_KEY,
    REGISTRY_USERNAME_KEY,
    SecretStoreRegistryCredentials,
)

from tests.integration.chokepoint_support import (
    ScriptedPolicy,
    allow,
    build_chokepoint,
    make_fixture,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

#: A distinctive token, so a substring search over every governance row is a meaningful assertion rather
#: than a coincidence.
TEST_TOKEN = "forgeops-registry-token-must-not-be-persisted-8f3a"


class _Pending:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def result(self, timeout: float | None = None) -> dict[str, Any]:
        return self._payload


class RecordingSink:
    """Keeps every command handed to it, so the ENVELOPE can be inspected.

    The transport, not the agent: what travels in the envelope is the fact under test, and the only way to
    see it is to keep what the sink was given.
    """

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_command(self, *, device_id: uuid.UUID, command: Any) -> Any:
        self.sent.append(command)
        return _Pending({})


class _MapStore:
    """A `SecretStore` over a map, standing in for Infisical.

    NOT A MOCK ON A RUNTIME PATH. It implements the same Protocol the production stores implement, so the
    code under test takes the identical path; what it replaces is a network service these tests must not
    require. The sealing format is exercised by the secrets suite, not here.
    """

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.reads: list[str] = []

    async def get_value(self, secret: Any) -> str:
        self.reads.append(secret.key)
        return self._values[secret.key]

    async def set_value(self, secret: Any, value: str) -> None:
        self._values[secret.key] = value


async def _seed_secret(session: AsyncSession, project_id: uuid.UUID, key: str) -> None:
    await session.execute(
        text(
            "INSERT INTO secrets (id, project_id, tenant_id, environment, key, encrypted_value) "
            "VALUES (:id, :project_id, NULL, :environment, :key, :blob)"
        ),
        {
            "id": str(uuid.uuid4()),
            "project_id": str(project_id),
            "environment": REGISTRY_CREDENTIAL_ENVIRONMENT,
            "key": key,
            # A non-null blob satisfies the table's exactly-one-storage CHECK. The VALUE comes from the
            # store, not this column.
            "blob": b"sealed-by-the-store",
        },
    )
    await session.commit()


class TestResolution:
    async def test_no_registry_secrets_resolves_to_absent(self, sessions: Any) -> None:
        """`None` is a real answer: a push to an unauthenticated registry needs no credential."""
        provider = SecretStoreRegistryCredentials(_MapStore({}))
        async with sessions() as session:
            fixture = await make_fixture(session)
            resolved = await provider.registry_credential_for(session, project_id=fixture.project_id, registry="")
        assert resolved is None

    async def test_half_a_credential_is_no_credential(self, sessions: Any) -> None:
        """A username with no token resolves to `None`, not to a login with an empty secret.

        Otherwise the registry answers with an authentication failure and an operator reads that as "my
        token is wrong" when the truth is "I never set one".
        """
        provider = SecretStoreRegistryCredentials(_MapStore({REGISTRY_USERNAME_KEY: "somebody"}))
        async with sessions() as session:
            fixture = await make_fixture(session)
            await _seed_secret(session, fixture.project_id, REGISTRY_USERNAME_KEY)
            resolved = await provider.registry_credential_for(session, project_id=fixture.project_id, registry="")
        assert resolved is None

    async def test_a_stored_empty_token_resolves_to_absent(self, sessions: Any) -> None:
        provider = SecretStoreRegistryCredentials(
            _MapStore({REGISTRY_USERNAME_KEY: "somebody", REGISTRY_TOKEN_KEY: "   "})
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            await _seed_secret(session, fixture.project_id, REGISTRY_USERNAME_KEY)
            await _seed_secret(session, fixture.project_id, REGISTRY_TOKEN_KEY)
            resolved = await provider.registry_credential_for(session, project_id=fixture.project_id, registry="")
        assert resolved is None

    async def test_both_present_resolves_to_the_pair_through_the_injection_path(self, sessions: Any) -> None:
        store = _MapStore({REGISTRY_USERNAME_KEY: "pusher", REGISTRY_TOKEN_KEY: TEST_TOKEN})
        provider = SecretStoreRegistryCredentials(store)
        async with sessions() as session:
            fixture = await make_fixture(session)
            await _seed_secret(session, fixture.project_id, REGISTRY_USERNAME_KEY)
            await _seed_secret(session, fixture.project_id, REGISTRY_TOKEN_KEY)
            resolved = await provider.registry_credential_for(
                session, project_id=fixture.project_id, registry="registry.example.com"
            )
        assert resolved == ("pusher", TEST_TOKEN)
        # The unseal went through `inject_secrets`, which asked the store for both keys. Asserted because
        # the confinement gate enforces the import and only this proves the call happens.
        assert sorted(store.reads) == sorted([REGISTRY_USERNAME_KEY, REGISTRY_TOKEN_KEY])


class TestTheCredentialNeverReachesAPersistedRow:
    async def test_a_push_transit_writes_no_credential_anywhere(self, sessions: Any, redis_client: Any) -> None:
        """Drive a real push transit, then search every governance column for the token.

        THIS IS THE ASSERTION THE DESIGN EXISTS FOR. `FORBIDDEN_ARG_KEYS` and revision 0022's CHECK both
        guard `operation_args`, but neither would catch a credential written into an audit row's
        `after_state` — and `transit_host_action` copies every argument into exactly that field. Searching
        the rows, rather than reasoning about which fields get copied, is what keeps the claim true when
        somebody adds a field later.
        """
        sink = RecordingSink()
        store = _MapStore({REGISTRY_USERNAME_KEY: "pusher", REGISTRY_TOKEN_KEY: TEST_TOKEN})
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=allow()),
            sink=sink,
            redis_client=redis_client,
            registry_credential_provider=SecretStoreRegistryCredentials(store),
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            await _seed_secret(session, fixture.project_id, REGISTRY_USERNAME_KEY)
            await _seed_secret(session, fixture.project_id, REGISTRY_TOKEN_KEY)
            await chokepoint.transit_host_action(
                session,
                project_id=fixture.project_id,
                principal=fixture.principal,
                operation=DOCKER_IMAGE_OPERATION,
                target="registry.example.com/app:v1",
                args={
                    "action": "push",
                    "image": "registry.example.com/app:v1",
                    "registry": "registry.example.com",
                },
                environment_name=None,
                environment_requires_approval=False,
                reason="pushing the built image",
            )

        async with sessions() as session:
            for table, column in (
                ("change_sets", "operation_args"),
                ("audit_events", "after_state"),
                ("audit_events", "before_state"),
                ("audit_events", "reason"),
            ):
                rows = (
                    (
                        await session.execute(
                            text(f"SELECT CAST({column} AS text) AS value FROM {table} WHERE {column} IS NOT NULL")
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    assert TEST_TOKEN not in (row["value"] or ""), (
                        f"the registry token was persisted into {table}.{column}, where anyone who can "
                        "read the governance history can read it, for ever"
                    )

    async def test_the_credential_does_reach_the_envelope(self, sessions: Any, redis_client: Any) -> None:
        """The other half: absent from every row AND present in the envelope.

        Without this, the test above passes for the wrong reason — a provider returning nothing at all
        would satisfy it, and every push would then fail at the registry.
        """
        sink = RecordingSink()
        store = _MapStore({REGISTRY_USERNAME_KEY: "pusher", REGISTRY_TOKEN_KEY: TEST_TOKEN})
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=allow()),
            sink=sink,
            redis_client=redis_client,
            registry_credential_provider=SecretStoreRegistryCredentials(store),
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            await _seed_secret(session, fixture.project_id, REGISTRY_USERNAME_KEY)
            await _seed_secret(session, fixture.project_id, REGISTRY_TOKEN_KEY)
            await chokepoint.transit_host_action(
                session,
                project_id=fixture.project_id,
                principal=fixture.principal,
                operation=DOCKER_IMAGE_OPERATION,
                target="registry.example.com/app:v1",
                args={
                    "action": "push",
                    "image": "registry.example.com/app:v1",
                    "registry": "registry.example.com",
                },
                environment_name=None,
                environment_requires_approval=False,
                reason="pushing the built image",
            )

        assert sink.sent, "nothing was delivered, so the envelope's contents are untested"
        envelope_args = sink.sent[-1].envelope["args"]
        assert envelope_args.get("registry_secret") == TEST_TOKEN
        assert envelope_args.get("registry_user") == "pusher"

    async def test_a_pull_gets_no_credential(self, sessions: Any, redis_client: Any) -> None:
        """Only a push resolves a credential.

        The branch is on the ACTION, not on the operation, because all four verbs share one operation — so
        keying off `docker.image_action` alone would attach a registry token to every image removal,
        widening the credential's exposure for no reason at all.
        """
        sink = RecordingSink()
        store = _MapStore({REGISTRY_USERNAME_KEY: "pusher", REGISTRY_TOKEN_KEY: TEST_TOKEN})
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=allow()),
            sink=sink,
            redis_client=redis_client,
            registry_credential_provider=SecretStoreRegistryCredentials(store),
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            await _seed_secret(session, fixture.project_id, REGISTRY_USERNAME_KEY)
            await _seed_secret(session, fixture.project_id, REGISTRY_TOKEN_KEY)
            await chokepoint.transit_host_action(
                session,
                project_id=fixture.project_id,
                principal=fixture.principal,
                operation=DOCKER_IMAGE_OPERATION,
                target="alpine:3.20",
                args={"action": "pull", "image": "alpine:3.20"},
                environment_name=None,
                environment_requires_approval=False,
                reason="pulling a base image",
            )
        assert sink.sent
        assert "registry_secret" not in sink.sent[-1].envelope["args"]
