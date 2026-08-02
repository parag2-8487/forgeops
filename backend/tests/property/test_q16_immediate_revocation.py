# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-16 — immediate revocation (design §3.1, §11.2, §11.10, Appendix B Q-16; leaf 8.10).

Property, universally quantified over revocation timings relative to an in-flight message stream:

    the first message after a revocation is rejected and the socket closed; a replica that missed
    the pub/sub event still rejects.

**What this adds over leaf 8.4's integration cases.**
`tests/integration/test_agent_hub.py::TestRevocationTakesEffectOnTheNextMessage` already drives the
three shapes by hand: a revocation mid-session, a replica with no subscriber at all, and an
unreadable revocation set. Those are examples at one timing. What Appendix B quantifies over is the
**timing** — how many frames precede the revocation, which method the next frame carries, and
whether anything is in flight when it lands. A per-connection cache passes every single-timing test
that happens to revoke before the first frame; only a generated position can see it. So the doubles
are imported from that file rather than re-declared, and this file generates the schedule.

Importing the doubles rather than copying them is deliberate: two definitions of "a device
directory whose `is_revoked` reads the real Redis set" is how a property comes to quantify over a
shape the integration tests never exercise, and then a green property says nothing about the system
those tests describe. `tests/property/conftest.py` says the same thing about its fixtures.

**Negative control** (`mutations.toml` Q-16): the hub checks revocation once per connection instead
of per message. Every clause below whose revocation lands after the first frame then fails, and the
one that revokes before the handshake still passes — which is exactly why the generated position
matters.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from src.auth.devices import REVOCATION_SET_KEY
from src.websocket.hub import CLOSE_REVOKED

from ..integration.test_agent_hub import (
    CollectingProgress,
    StubDevice,
    StubDirectory,
    build_hub,
    connect_frame,
)
from ..integration.test_agent_hub import FakeSocket as HubSocket

pytestmark = pytest.mark.mandatory

#: Every example opens a real session against real Redis, so the budget buys breadth of TIMING
#: rather than raw count — which is where a per-connection cache hides.
_SETTINGS = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

#: The agent-originated methods a frame after the revocation could carry. All of them must be
#: refused, not just the heartbeat: a hub that checked revocation only on `session.heartbeat` would
#: pass a test that only ever sent heartbeats, and `command.result` is the frame that reports a
#: mutation as done.
_NEXT_METHODS = ("session.heartbeat", "command.progress", "command.result", "agent.status")


def _frame(method: str, index: int) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if method == "session.heartbeat":
        params = {"seq": index, "uptime_seconds": index, "queue_depth": 0}
    elif method in {"command.progress", "command.result"}:
        params = {"command_id": f"cmd-{index}", "status": "succeeded", "percent": 50, "output": ""}
    elif method == "agent.status":
        params = {"state": "ready", "policy_bundle_digest": ""}
    return {"jsonrpc": "2.0", "id": str(index + 2), "method": method, "params": params}


class TestTheFirstFrameAfterARevocationIsRefused:
    """The guarantee, over generated timings."""

    @given(
        before=st.integers(min_value=0, max_value=4),
        method=st.sampled_from(_NEXT_METHODS),
        after=st.integers(min_value=0, max_value=3),
    )
    @_SETTINGS
    @pytest.mark.asyncio
    async def test_whatever_the_position_of_the_revocation(
        self,
        redis_client: Any,
        before: int,
        method: str,
        after: int,
    ) -> None:
        """`before` frames are answered, then the device is revoked, then one more frame arrives.

        The assertion is on the COUNT of results: the hub must answer exactly `before` of them and
        stop. Asserting on a count rather than on "the last frame produced no result" is what makes
        a per-connection cache visible — a cached check answers every frame in the stream.

        `after` extra frames are queued behind the offending one and must also produce nothing, so
        the socket is proved shut rather than merely quiet for one frame.
        """
        device = StubDevice(device_id=uuid.uuid4(), project_id=uuid.uuid4())
        await redis_client.srem(REVOCATION_SET_KEY, str(device.device_id))
        directory = StubDirectory(redis_client)
        hub = build_hub(redis_client, directory, CollectingProgress())

        socket = HubSocket([connect_frame(device)] + [_frame("session.heartbeat", i) for i in range(before)])
        served = asyncio.create_task(hub.serve(socket, device=device))
        try:
            # Wait for the handshake and the `before` frames to be answered. The handshake's own
            # result is one, so the target is before + 1.
            await _until(lambda: len(socket.results()) >= before + 1)

            await redis_client.sadd(REVOCATION_SET_KEY, str(device.device_id))
            answered = len(socket.results())
            socket.feed(_frame(method, before))
            for extra in range(after):
                socket.feed(_frame("session.heartbeat", before + 1 + extra))

            await _until(lambda: socket.close_code is not None)
            assert socket.close_code == CLOSE_REVOKED, f"closed {socket.close_code}, want {CLOSE_REVOKED}"
            assert len(socket.results()) == answered, (
                f"the hub answered a frame after the revocation ({len(socket.results())} results, "
                f"{answered} before it) — the check is not per message"
            )
            assert directory.checks >= before + 1, (
                f"{directory.checks} revocation check(s) for {before + 1} frame(s); §3.1 makes the "
                "check per inbound frame rather than per connection"
            )
        finally:
            socket.release.set()
            await _finish(served)

    @given(before=st.integers(min_value=1, max_value=4))
    @_SETTINGS
    @pytest.mark.asyncio
    async def test_a_replica_that_never_subscribed_still_refuses(
        self,
        redis_client: Any,
        before: int,
    ) -> None:
        """The distinction between the guarantee and the optimisation.

        `subscribe_revocations` is never called here, so no pub/sub event can reach this hub. §3.1
        makes the per-message `SISMEMBER` the guarantee and the pub/sub close an optimisation; a
        replica that missed the announcement must still refuse the next frame. Generated over the
        number of frames that precede the revocation, because a hub that only re-checked on the
        first frame would pass at `before == 0`.
        """
        device = StubDevice(device_id=uuid.uuid4(), project_id=uuid.uuid4())
        await redis_client.srem(REVOCATION_SET_KEY, str(device.device_id))
        directory = StubDirectory(redis_client)
        hub = build_hub(redis_client, directory, CollectingProgress())

        socket = HubSocket([connect_frame(device)] + [_frame("session.heartbeat", i) for i in range(before)])
        served = asyncio.create_task(hub.serve(socket, device=device))
        try:
            await _until(lambda: len(socket.results()) >= before + 1)
            # Written directly to the set, with no announcement of any kind.
            await redis_client.sadd(REVOCATION_SET_KEY, str(device.device_id))
            socket.feed(_frame("session.heartbeat", before + 1))
            await _until(lambda: socket.close_code is not None)
            assert socket.close_code == CLOSE_REVOKED
        finally:
            socket.release.set()
            await _finish(served)

    @given(frames=st.integers(min_value=1, max_value=5))
    @_SETTINGS
    @pytest.mark.asyncio
    async def test_the_control_shows_an_unrevoked_device_is_answered_every_time(
        self,
        redis_client: Any,
        frames: int,
    ) -> None:
        """The control the two clauses above need.

        Without it both would pass for a hub that closed 4403 on every frame, which would refuse
        every healthy agent and still look like a working revocation check.
        """
        device = StubDevice(device_id=uuid.uuid4(), project_id=uuid.uuid4())
        await redis_client.srem(REVOCATION_SET_KEY, str(device.device_id))
        directory = StubDirectory(redis_client)
        hub = build_hub(redis_client, directory, CollectingProgress())

        socket = HubSocket([connect_frame(device)] + [_frame("session.heartbeat", i) for i in range(frames)])
        served = asyncio.create_task(hub.serve(socket, device=device))
        try:
            await _until(lambda: len(socket.results()) >= frames + 1)
            assert socket.close_code is None, f"a live device was closed {socket.close_code}"
            assert len(socket.results()) == frames + 1
        finally:
            socket.release.set()
            await _finish(served)


class TestTheCheckFailsClosed:
    @given(before=st.integers(min_value=0, max_value=3))
    @_SETTINGS
    @pytest.mark.asyncio
    async def test_an_unreadable_revocation_set_closes_the_socket(
        self,
        redis_client: Any,
        before: int,
    ) -> None:
        """Q-16's fail-closed direction, over the position at which Redis becomes unreadable.

        `RevocationUnavailableError` rather than a `False` return, because the whole point is that
        "I cannot tell" is not "not revoked". Generated over the position so a hub that only
        handled an outage during the handshake is visible.
        """
        device = StubDevice(device_id=uuid.uuid4(), project_id=uuid.uuid4())
        await redis_client.srem(REVOCATION_SET_KEY, str(device.device_id))
        directory = StubDirectory(redis_client)
        hub = build_hub(redis_client, directory, CollectingProgress())

        socket = HubSocket([connect_frame(device)] + [_frame("session.heartbeat", i) for i in range(before)])
        served = asyncio.create_task(hub.serve(socket, device=device))
        try:
            await _until(lambda: len(socket.results()) >= before + 1)
            directory.unavailable = True
            socket.feed(_frame("session.heartbeat", before + 1))
            await _until(lambda: socket.close_code is not None)
            assert socket.close_code == CLOSE_REVOKED, (
                f"closed {socket.close_code}; an unreadable revocation set must refuse rather than allow"
            )
        finally:
            socket.release.set()
            await _finish(served)


async def _until(condition: Any, *, timeout: float = 5.0) -> None:
    """Poll a condition with a bounded deadline.

    A poll rather than a sleep: a fixed sleep is either flaky or slow, and this file runs one
    session per generated example.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never held within the deadline")


async def _finish(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, RuntimeError):
        pass
