# SPDX-License-Identifier: FSL-1.1-ALv2
"""Publishing a policy bundle must not invalidate every paired agent.

Admission compares `agent_devices.policy_bundle_digest` against the project's active digest and refuses
with `policy-bundle-stale` when they differ. So publishing moved the active digest and stranded every
device pinned to the previous one. The only remedy was `pair --wipe` and a fresh code per agent, which
`auth/devices.py` stated outright: submissions "are refused until one is published and it pairs again".

§3.1 always specified that `session.connect` returns the bundle when the pin is stale. The handshake
reported only THAT the pin had moved — the site's own comment said the body was out of reach — so an
agent was told it was stale and given nothing to do about it.

Two halves are covered here:

  * `_active_bundle` reads the body, so the handshake has something to hand over.
  * `_repin_device_bundle` advances the pin, driven by the agent's own `agent.status` report rather
    than by the handover, so the pin never claims more than the device can prove.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.websocket.hub import AgentHub, HubDeps

from .chokepoint_support import make_fixture

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def device_for_repin(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A real project with a real active device, built by the same helper the chokepoint tests use.

    A hand-inserted row would not carry the sealed key and the pin that `make_fixture` sets up, and the
    point of these tests is the interaction between those columns and the hub.
    """
    async with sessions() as session:
        fixture = await make_fixture(session)
        return fixture.device_id


@pytest_asyncio.fixture()
async def hub_for_repin(
    sessions: async_sessionmaker[AsyncSession], redis_client: Any, device_for_repin: uuid.UUID
) -> tuple[Any, uuid.UUID]:
    """A hub wired to the REAL sessionmaker.

    The existing hub tests use a no-op session because they exercise framing. These two methods read and
    write `policy_bundles` and `agent_devices`, so a stub would prove only that the code runs.
    """
    async with sessions() as session:
        project_id = (
            await session.execute(
                text("SELECT project_id FROM agent_devices WHERE id = :i"), {"i": device_for_repin}
            )
        ).scalar_one()

    class _Directory:
        async def get(self, device_id: uuid.UUID) -> None:
            return None

    class _Progress:
        async def publish(self, **_: Any) -> None:
            return None

    hub = AgentHub(
        HubDeps(
            redis=redis_client,
            devices=_Directory(),
            sessionmaker=sessions,
            progress=_Progress(),
            heartbeat_interval_seconds=30,
            heartbeat_timeout_seconds=90,
        )
    )
    return hub, project_id


def _bundle(marker: str) -> tuple[bytes, str]:
    """A real gzipped tar, because the column holds one and a digest over a fake proves nothing."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = f"package {marker}\n".encode()
        info = tarfile.TarInfo(f"{marker}.rego")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    body = buf.getvalue()
    return body, "sha256:" + hashlib.sha256(body).hexdigest()


async def _publish(session: AsyncSession, marker: str) -> tuple[bytes, str]:
    body, digest = _bundle(marker)
    await session.execute(text("UPDATE policy_bundles SET active = false WHERE active"))
    await session.execute(
        text(
            "INSERT INTO policy_bundles (id, digest, bundle, project_id, tenant_id, active, created_at) "
            "VALUES (:i, :d, :b, NULL, NULL, true, now())"
        ),
        {"i": uuid.uuid4(), "d": digest, "b": body},
    )
    await session.commit()
    return body, digest


class TestTheHubCanReadTheBundleBody:
    """Without the body there is nothing to hand a stale device."""

    async def test_it_returns_the_active_digest_and_body_together(
        self, hub_for_repin, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        hub, project_id = hub_for_repin
        async with sessions() as session:
            body, digest = await _publish(session, "handover")

        got_digest, got_body = await hub._active_bundle(project_id)
        assert got_digest == digest
        assert got_body == body
        # The digest must describe the body that came with it, or the agent will refuse the adoption.
        assert "sha256:" + hashlib.sha256(got_body).hexdigest() == got_digest
        # And it must really be a gzipped tar, not an opaque blob that happens to hash.
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(got_body) and got_body), mode="r:gz") as tar:
            assert tar.getnames() == ["handover.rego"]

    async def test_no_active_bundle_reports_nothing_rather_than_an_empty_body(
        self, hub_for_repin, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """D-30 makes a missing bundle a DENY, so an empty body must never stand in for an absent one."""
        hub, project_id = hub_for_repin
        async with sessions() as session:
            await session.execute(text("UPDATE policy_bundles SET active = false WHERE active"))
            await session.commit()
        digest, body = await hub._active_bundle(project_id)
        assert digest == ""
        assert body is None


class TestTheDeviceCanBeRepinned:
    """The half that did not exist. `agent.status` agreement previously did nothing at all."""

    async def test_a_matching_report_advances_the_pin(
        self, hub_for_repin, sessions: async_sessionmaker[AsyncSession], device_for_repin: uuid.UUID
    ) -> None:
        hub, _ = hub_for_repin
        async with sessions() as session:
            _, digest = await _publish(session, "advance")

        assert await hub._repin_device_bundle(device_for_repin, digest) is True

        async with sessions() as session:
            row = (
                await session.execute(
                    text("SELECT policy_bundle_digest, status FROM agent_devices WHERE id = :i"),
                    {"i": device_for_repin},
                )
            ).mappings().one()
        assert row["policy_bundle_digest"] == digest
        # A device marked stale by the drift check must come back, or it stays unable to submit while
        # holding exactly the right bundle.
        assert str(row["status"]) in {"active", "DeviceStatus.ACTIVE"}

    async def test_repinning_to_the_digest_already_held_changes_nothing(
        self, hub_for_repin, sessions: async_sessionmaker[AsyncSession], device_for_repin: uuid.UUID
    ) -> None:
        """`agent.status` arrives on a timer, so agreement is the steady state, not an event."""
        hub, _ = hub_for_repin
        async with sessions() as session:
            _, digest = await _publish(session, "steady")
        assert await hub._repin_device_bundle(device_for_repin, digest) is True
        assert await hub._repin_device_bundle(device_for_repin, digest) is False

    async def test_a_revoked_device_does_not_become_active_again(
        self, hub_for_repin, sessions: async_sessionmaker[AsyncSession], device_for_repin: uuid.UUID
    ) -> None:
        """Re-pinning restores only the status it is responsible for.

        `policy_stale` is a consequence of the pin and is the one status this may clear. A revoked
        device holding a current bundle is still revoked, and a re-pin that resurrected it would be a
        policy update quietly undoing a security decision.
        """
        hub, _ = hub_for_repin
        async with sessions() as session:
            await session.execute(
                text("UPDATE agent_devices SET status = 'revoked' WHERE id = :i"),
                {"i": device_for_repin},
            )
            await session.commit()
            _, digest = await _publish(session, "revoked")

        await hub._repin_device_bundle(device_for_repin, digest)

        async with sessions() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM agent_devices WHERE id = :i"), {"i": device_for_repin}
                )
            ).scalar_one()
        assert "revoked" in str(status).lower()
