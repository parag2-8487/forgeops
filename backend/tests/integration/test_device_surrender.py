# SPDX-License-Identifier: FSL-1.1-ALv2
"""An agent giving back a device whose credential it could not persist (§3.7).

WHY THIS PATH EXISTS AT ALL

`pair` is not atomic across the network and the agent's local credential store. The exchange burns
a single-use code, issues a 24-hour certificate and marks the device `active`; only then does the
agent try to write what it received. On Windows that write could never succeed — the full credential
bundle is past the Credential Manager's 2560-byte ceiling — so every pairing attempt left an
`active` row whose device token existed nowhere, counted against the project, and gave no operator
any reason to look at it.

The agent now checks capacity before spending the code, which makes the size case unreachable. This
covers what happens when a write fails anyway: a full disk, a keychain locked between the probe and
the write. The device is surrendered rather than left active.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.auth.devices import (
    AgentMeta,
    DeviceAuthenticationError,
    DeviceService,
)

from .test_agent_pairing import (
    audit_for,
    build_csr,
    device_rows,
    make_project,
    make_service,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def service(redis_client: Any) -> DeviceService:
    return make_service(redis_client)


async def pair_a_device(
    sessions: async_sessionmaker[AsyncSession],
    service: DeviceService,
) -> tuple[uuid.UUID, str, uuid.UUID]:
    """Pair one device for real and return (project_id, device_token_hex, device_id)."""
    async with sessions() as session:
        project_id, principal = await make_project(session)
        issued = await service.issue_pairing_code(session, project_id=project_id, actor=principal)
        await session.commit()

    csr_pem, fingerprint = build_csr()
    async with sessions() as session:
        credentials = await service.exchange(
            session,
            code=issued.code,
            csr_pem=csr_pem,
            meta=AgentMeta(agent_version="1.2.3-test", platform="windows/amd64", fingerprint=fingerprint),
            client_ip="127.0.0.1",
        )
        await session.commit()

    return project_id, credentials.device_token.get_secret_value().hex(), credentials.device_id


class TestAbandonSelf:
    async def test_it_moves_an_active_device_to_abandoned(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        project_id, token, device_id = await pair_a_device(sessions, service)

        async with sessions() as session:
            before = await device_rows(session, project_id)
            assert [r["status"] for r in before] == ["active"], (
                "the exchange must leave the device active, or this test is not covering the "
                "state that needed giving back"
            )

        async with sessions() as session:
            returned = await service.abandon_self(session, device_token=token)
            await session.commit()

        assert returned == device_id

        async with sessions() as session:
            after = await device_rows(session, project_id)
        assert [r["status"] for r in after] == ["abandoned"]

        # THE WHOLE POINT: no active device is left for a credential nobody holds.
        assert all(r["status"] != "active" for r in after)

    async def test_it_clears_the_token_so_the_credential_is_dead(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        project_id, token, _ = await pair_a_device(sessions, service)

        async with sessions() as session:
            await service.abandon_self(session, device_token=token)
            await session.commit()

        async with sessions() as session:
            rows = await device_rows(session, project_id)
        # A surrendered device must not keep a usable token hash. Leaving it would mean the
        # credential the agent failed to store still authenticated something.
        assert rows[0]["device_token_hmac"] is None

    async def test_a_second_surrender_is_refused_rather_than_writing_a_second_row(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        project_id, token, _ = await pair_a_device(sessions, service)

        async with sessions() as session:
            await service.abandon_self(session, device_token=token)
            await session.commit()

        async with sessions() as session:
            with pytest.raises(DeviceAuthenticationError):
                await service.abandon_self(session, device_token=token)

        async with sessions() as session:
            events = await audit_for(session, project_id)
        abandoned = [e for e in events if e["action"] == "device_abandoned"]
        assert len(abandoned) == 1, "a repeat surrender must not write a second audit row"

    async def test_it_writes_an_audit_row_with_no_user_behind_it(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        project_id, token, device_id = await pair_a_device(sessions, service)

        async with sessions() as session:
            await service.abandon_self(session, device_token=token)
            await session.commit()

        async with sessions() as session:
            events = await audit_for(session, project_id)

        rows = [e for e in events if e["action"] == "device_abandoned"]
        assert len(rows) == 1
        row = rows[0]
        assert row["outcome"] == "allowed"
        assert str(device_id) in str(row["resource_id"])
        # No person did this. A user id here would put somebody's name against an action they
        # did not take, which is worse than an empty field.
        assert row["actor_user_id"] is None
        assert row["after_state"]["surrendered_by"] == "agent"
        assert row["reason"].strip(), "NFR-14 requires a stated reason"

    @pytest.mark.parametrize(
        "bad_token",
        [
            "",
            "not-hex",
            "ab" * 16,  # right shape, wrong length
            "ab" * 64,  # too long
        ],
    )
    async def test_it_refuses_a_token_that_is_not_a_live_credential(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService, bad_token: str
    ) -> None:
        await pair_a_device(sessions, service)

        async with sessions() as session:
            with pytest.raises(DeviceAuthenticationError):
                await service.abandon_self(session, device_token=bad_token)

    async def test_one_device_cannot_surrender_another(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """The token selects the row, so there is no parameter through which to name a victim.

        This is the property that makes one-factor authentication defensible on this route: the
        worst a token holder can do is destroy the credential they already hold. If the operation
        could ever name another device, that argument would collapse.
        """
        first_project, first_token, _ = await pair_a_device(sessions, service)
        second_project, second_token, _ = await pair_a_device(sessions, service)

        async with sessions() as session:
            await service.abandon_self(session, device_token=first_token)
            await session.commit()

        async with sessions() as session:
            first_rows = await device_rows(session, first_project)
            second_rows = await device_rows(session, second_project)

        assert first_rows[0]["status"] == "abandoned"
        assert second_rows[0]["status"] == "active", "surrendering one device changed another device's row"
        assert second_token != first_token
