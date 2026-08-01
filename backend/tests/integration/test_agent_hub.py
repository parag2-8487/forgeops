# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent hub against a real Redis (design.md §3.1, §7.3, §7.4, §11.10; Q-16; leaf 8.4).

Real Redis, fake socket, fake device directory. That split is deliberate: the properties this file
asserts — revocation takes effect on the *next frame*, a command reaches the replica that owns the
socket through a stream, a session that stops beating is dropped — are properties of the Redis
protocol between replicas, and a fake Redis would be asserting that the fake behaves. The socket is
faked because a real one adds a uvicorn process and proves nothing extra: `serve` takes anything
with `receive_json`, `send_json` and `close`.

The device directory is a stub for one reason: authenticating a peer needs a certificate, a CA and a
device row, all of which `test_agent_pairing.py` already drives end to end. What the hub is
responsible for is what it does with an *already authenticated* device, so the stub supplies exactly
that and the revocation half reads the real Redis set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from src.auth.devices import REVOCATION_CHANNEL, REVOCATION_SET_KEY, RevocationUnavailableError
from src.core.errors import ProblemException
from src.websocket.hub import (
    AGENT_ORIGINATED_METHODS,
    BACKEND_ORIGINATED_METHODS,
    CLOSE_HANDSHAKE_FAILED,
    CLOSE_HEARTBEAT_TIMEOUT,
    CLOSE_REVOKED,
    CLOSE_UNAUTHENTICATED,
    JSONRPC_METHODS,
    AgentHub,
    HubDeps,
)

from ..synthetic_secrets import pem_armour

pytestmark = [pytest.mark.integration, pytest.mark.mandatory]


# ─── doubles ─────────────────────────────────────────────────────────────────


@dataclass
class StubDevice:
    """The `AuthenticatedDevice` shape the route hands the hub."""

    device_id: uuid.UUID
    project_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    policy_bundle_digest: str = ""
    last_seq: int = 7
    agent_version: str = "1.0.0"
    platform: str = "linux/amd64"


class StubDirectory:
    """The three methods the hub's `DeviceDirectory` Protocol names.

    `is_revoked` reads the REAL Redis set rather than a boolean flag: Q-16's guarantee is about what
    is in Redis, and a stub that answered from an attribute would let a hub that never queried
    Redis pass this file.
    """

    def __init__(self, redis: Any, *, unavailable: bool = False) -> None:
        self._redis = redis
        self.unavailable = unavailable
        self.checks = 0
        self.rotations: list[bytes] = []
        self.rotation_error: Exception | None = None

    async def authenticate_session(self, session: Any, *, certificate_pem: bytes, device_token: str) -> Any:
        raise AssertionError("the hub must not authenticate; the route does (§2.4's import ban)")

    async def is_revoked(self, device_id: uuid.UUID) -> bool:
        self.checks += 1
        if self.unavailable:
            raise RevocationUnavailableError("redis is down")
        return bool(await self._redis.sismember(REVOCATION_SET_KEY, str(device_id)))

    async def rotate_certificate(self, session: Any, *, device_id: uuid.UUID, csr_pem: bytes) -> Any:
        self.rotations.append(csr_pem)
        if self.rotation_error is not None:
            raise self.rotation_error
        raise AssertionError("no test exercises a successful rotation without setting a bundle")


class NoopSession:
    """A session whose statements go nowhere.

    The hub's two database reads — `last_seen` and the active bundle digest — are both wrapped in
    "a failure here must not drop a healthy socket", so a no-op session exercises exactly the path
    a real outage would take.
    """

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no database in this test")

    async def commit(self) -> None: ...

    async def close(self) -> None: ...


class CollectingProgress:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, *, command_id: str, event: Any) -> None:
        self.events.append({"command_id": command_id, **dict(event)})


_BLOCK = object()
_HOLD = object()


class FakeSocket:
    """Enough WebSocket for `serve`: a scripted inbound queue and a recorded outbound list."""

    def __init__(self, inbound: list[Any] | None = None) -> None:
        self._inbound: list[Any] = list(inbound or [])
        self.sent: list[dict[str, Any]] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._gone = asyncio.Event()
        #: Released by a test that wants the session to stay open until it has observed something
        #: and then end cleanly. A concurrent watcher task was the first shape of this and it hung
        #: the suite: when `serve` returned first, the watcher polled forever.
        self.release = asyncio.Event()

    def feed(self, frame: Any) -> None:
        self._inbound.append(frame)

    async def receive_json(self) -> Any:
        while True:
            if self._inbound:
                item = self._inbound.pop(0)
                if item is _BLOCK:
                    await asyncio.sleep(3600)
                if item is _HOLD:
                    await self.release.wait()
                    raise RuntimeError("peer gone")
                if isinstance(item, Exception):
                    raise item
                return item
            if self.close_code is not None:
                raise RuntimeError("socket closed")
            await asyncio.sleep(0.005)

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        self._gone.set()

    # ── helpers the assertions read ──
    def results(self) -> list[dict[str, Any]]:
        return [f["result"] for f in self.sent if "result" in f]

    def errors(self) -> list[dict[str, Any]]:
        out = []
        for frame in self.sent:
            if frame.get("method") == "agent.error":
                out.append(frame["params"])
            elif "error" in frame:
                out.append(frame["error"].get("data") or {})
        return out

    def commands(self) -> list[dict[str, Any]]:
        return [f for f in self.sent if f.get("method") == "command.execute"]


def build_hub(
    redis: Any,
    directory: Any,
    progress: Any,
    *,
    interval: int = 30,
    timeout: int = 90,
) -> AgentHub:
    return AgentHub(
        HubDeps(
            redis=redis,
            devices=directory,
            sessionmaker=NoopSession,
            progress=progress,
            heartbeat_interval_seconds=interval,
            heartbeat_timeout_seconds=timeout,
        )
    )


def connect_frame(device: StubDevice, *, digest: str = "", request_id: str = "1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session.connect",
        "params": {
            "device_id": str(device.device_id),
            "agent_version": device.agent_version,
            "platform": device.platform,
            "policy_bundle_digest": digest,
            "capabilities": ["scan", "apply"],
        },
    }


@pytest.fixture()
def device() -> StubDevice:
    return StubDevice(device_id=uuid.uuid4(), project_id=uuid.uuid4())


@pytest.fixture(autouse=True)
async def _clean_revocations(redis_client: Any, device: StubDevice) -> Any:
    yield
    await redis_client.srem(REVOCATION_SET_KEY, str(device.device_id))
    await redis_client.delete(f"forgeops:agentsession:{device.device_id}")
    await redis_client.delete(f"forgeops:agentcmd:{device.device_id}")


# ─── the method catalogue ────────────────────────────────────────────────────


class TestTheCatalogueIsClosed:
    def test_there_are_exactly_nine_methods(self) -> None:
        """§7.3 fixes nine, and D-41 is the reason a tenth is not needed: a journalled intent is
        replayed as `approval.request`, not as a new method."""
        assert len(JSONRPC_METHODS) == 9
        assert AGENT_ORIGINATED_METHODS <= JSONRPC_METHODS
        assert BACKEND_ORIGINATED_METHODS <= JSONRPC_METHODS
        assert AGENT_ORIGINATED_METHODS | BACKEND_ORIGINATED_METHODS == JSONRPC_METHODS

    def test_an_agent_cannot_originate_a_command_or_an_approval(self) -> None:
        """The direction is the authorisation boundary: an agent that could send itself
        `command.execute` would be authorising its own mutation."""
        assert "command.execute" not in AGENT_ORIGINATED_METHODS
        assert "approval.response" not in AGENT_ORIGINATED_METHODS


# ─── handshake ──────────────────────────────────────────────────────────────


class TestTheHandshake:
    async def test_it_answers_session_connect_with_the_session_parameters(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        socket = FakeSocket([connect_frame(device), RuntimeError("peer gone")])

        await hub.serve(socket, device=device)

        result = socket.results()[0]
        assert result["heartbeat_interval"] == 30
        assert result["heartbeat_timeout"] == 90
        assert result["seq_base"] == device.last_seq
        assert result["session_id"]
        # `seq` does not reset across reconnects (§7.6), so the handshake reports the device's
        # existing high-water mark rather than zero.
        assert result["seq_base"] != 0

    async def test_a_first_frame_that_is_not_session_connect_is_refused(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        socket = FakeSocket([{"jsonrpc": "2.0", "id": "1", "method": "session.heartbeat", "params": {}}])

        await hub.serve(socket, device=device)

        assert socket.close_code == CLOSE_HANDSHAKE_FAILED
        assert any(err.get("code") == "handshake-required" for err in socket.errors())

    async def test_a_connect_naming_another_device_is_refused(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        """Everything downstream keys off the device id, so a frame that claims a different one is
        an authentication failure rather than a field to prefer the certificate over."""
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        frame = connect_frame(device)
        frame["params"]["device_id"] = str(uuid.uuid4())
        socket = FakeSocket([frame])

        await hub.serve(socket, device=device)

        assert socket.close_code == CLOSE_UNAUTHENTICATED
        assert any(err.get("code") == "device-mismatch" for err in socket.errors())

    async def test_no_first_frame_at_all_closes_rather_than_waiting_forever(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress(), interval=1, timeout=2)
        socket = FakeSocket([_BLOCK])

        await asyncio.wait_for(hub.serve(socket, device=device), timeout=10)

        assert socket.close_code == CLOSE_HANDSHAKE_FAILED

    async def test_a_device_revoked_before_the_first_frame_gets_no_session(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        await redis_client.sadd(REVOCATION_SET_KEY, str(device.device_id))
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        socket = FakeSocket([connect_frame(device)])

        await hub.serve(socket, device=device)

        assert socket.close_code == CLOSE_REVOKED
        assert socket.results() == []


# ─── per-message revocation: Q-16 ───────────────────────────────────────────


class TestRevocationTakesEffectOnTheNextMessage:
    async def test_the_frame_after_a_revocation_is_refused_and_the_socket_closed(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        """The property §3.1 calls non-trivial: the session was established and healthy, and the
        very next frame after `SADD` is refused. Nothing reconnects, and no cache is waited out."""
        directory = StubDirectory(redis_client)
        hub = build_hub(redis_client, directory, CollectingProgress())
        socket = FakeSocket(
            [
                connect_frame(device),
                {"jsonrpc": "2.0", "id": "2", "method": "session.heartbeat", "params": {"seq": 1}},
            ]
        )

        async def revoke_then_send() -> None:
            # Wait until the first heartbeat has been answered, then revoke and send one more.
            while len(socket.results()) < 2:
                await asyncio.sleep(0.005)
            await redis_client.sadd(REVOCATION_SET_KEY, str(device.device_id))
            socket.feed({"jsonrpc": "2.0", "id": "3", "method": "session.heartbeat", "params": {"seq": 2}})

        await asyncio.gather(
            asyncio.wait_for(hub.serve(socket, device=device), timeout=10),
            revoke_then_send(),
        )

        assert socket.close_code == CLOSE_REVOKED
        assert any(err.get("code") == "device-revoked" for err in socket.errors())
        # The third frame was refused BEFORE it was handled, so it produced no result.
        assert len(socket.results()) == 2
        # Checked per frame, not once per connection: connect + two heartbeats.
        assert directory.checks >= 3

    async def test_a_replica_that_missed_the_pubsub_event_still_refuses(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        """No subscriber is running in this test at all. The `SISMEMBER` is the guarantee, and the
        pub/sub close is the optimisation — so a hub that never heard the announcement must still
        refuse the next frame."""
        await redis_client.sadd(REVOCATION_SET_KEY, str(device.device_id))
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        socket = FakeSocket([connect_frame(device)])

        await hub.serve(socket, device=device)

        assert socket.close_code == CLOSE_REVOKED

    async def test_an_unreadable_revocation_set_closes_the_socket(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        """Fails closed. Treating an unreachable Redis as "nobody is revoked" would mean an outage
        silently re-enabled every revoked device, which is the inverse of Q-16."""
        hub = build_hub(redis_client, StubDirectory(redis_client, unavailable=True), CollectingProgress())
        socket = FakeSocket([connect_frame(device)])

        await hub.serve(socket, device=device)

        assert socket.close_code == CLOSE_REVOKED
        assert any(err.get("code") == "revocation-unavailable" for err in socket.errors())


# ─── heartbeat ──────────────────────────────────────────────────────────────


class TestHeartbeat:
    async def test_a_heartbeat_refreshes_the_session_key_with_the_timeout_as_its_ttl(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        socket = FakeSocket(
            [
                connect_frame(device),
                {"jsonrpc": "2.0", "id": "2", "method": "session.heartbeat", "params": {"seq": 1, "queue_depth": 3}},
                _HOLD,
            ]
        )

        key = f"forgeops:agentsession:{device.device_id}"
        serving = asyncio.create_task(hub.serve(socket, device=device))
        try:
            for _ in range(400):
                if len(socket.results()) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert len(socket.results()) >= 2, "the heartbeat was never answered"
            ttl = int(await redis_client.ttl(key))
        finally:
            socket.release.set()
            await asyncio.wait_for(serving, timeout=10)

        assert 0 < ttl <= 90
        assert ttl > 60, "the TTL is the heartbeat timeout, so another replica can tell a live device from a gone one"
        # Dropped when the session ends, so `send_command` refuses rather than enqueueing for a
        # device nobody is listening for.
        assert await redis_client.exists(key) == 0

    async def test_silence_past_the_timeout_drops_the_session(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        """The 90 s timeout is configuration (`HEARTBEAT_TIMEOUT_SECONDS`, validated `> interval`);
        the mechanism is asserted at two seconds so the suite does not sleep for a minute and a
        half. The configured value is asserted in the handshake result above."""
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress(), interval=1, timeout=2)
        socket = FakeSocket([connect_frame(device), _BLOCK])

        await asyncio.wait_for(hub.serve(socket, device=device), timeout=15)

        assert socket.close_code == CLOSE_HEARTBEAT_TIMEOUT
        assert await redis_client.exists(f"forgeops:agentsession:{device.device_id}") == 0

    async def test_a_heartbeat_carrying_a_csr_asks_for_rotation_rather_than_a_tenth_method(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        """D-80: rotation rides the heartbeat. A refusal is reported in the result and does not drop
        the socket, because a certificate problem must not become a reconnect loop."""
        directory = StubDirectory(redis_client)
        directory.rotation_error = RuntimeError("device is not active")
        hub = build_hub(redis_client, directory, CollectingProgress())
        # Assembled rather than written out: FO-SEC001 matches on PEM *shape* and not on
        # sensitivity, and a certificate request is not a secret — but a gate that could be waved
        # through for "obviously fine" cases would be no gate. `pem_armour` is the sanctioned way.
        csr = pem_armour("CERTIFICATE REQUEST") + "\nnot-a-real-csr\n"
        socket = FakeSocket(
            [
                connect_frame(device),
                {
                    "jsonrpc": "2.0",
                    "id": "2",
                    "method": "session.heartbeat",
                    "params": {"seq": 1, "csr": csr},
                },
                RuntimeError("peer gone"),
            ]
        )

        await asyncio.wait_for(hub.serve(socket, device=device), timeout=10)

        assert directory.rotations, "the CSR never reached rotate_certificate"
        assert socket.results()[1]["certificate"]["rotated"] is False
        assert socket.close_code is None


# ─── direction and dispatch ─────────────────────────────────────────────────


class TestDispatch:
    async def test_a_backend_originated_method_from_an_agent_is_refused(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        socket = FakeSocket(
            [
                connect_frame(device),
                {"jsonrpc": "2.0", "id": "2", "method": "command.execute", "params": {}},
                RuntimeError("peer gone"),
            ]
        )

        await asyncio.wait_for(hub.serve(socket, device=device), timeout=10)

        assert any(err.get("code") == "method-not-allowed" for err in socket.errors())
        assert socket.close_code is None, "one bad frame is refused, not fatal"

    async def test_progress_is_fanned_out_for_sse(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        progress = CollectingProgress()
        hub = build_hub(redis_client, StubDirectory(redis_client), progress)
        socket = FakeSocket(
            [
                connect_frame(device),
                {
                    "jsonrpc": "2.0",
                    "method": "command.progress",
                    "params": {"command_id": "c-1", "percent": 40, "stage": "apply", "message": "writing"},
                },
                RuntimeError("peer gone"),
            ]
        )

        await asyncio.wait_for(hub.serve(socket, device=device), timeout=10)

        assert progress.events == [{"command_id": "c-1", "percent": 40, "stage": "apply", "message": "writing"}]

    async def test_an_agent_originated_approval_request_is_refused_as_retryable(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        """The chokepoint has no agent-originated entry yet. A retryable refusal keeps the intent in
        the agent's journal (D-41) instead of letting the agent believe it was accepted."""
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        socket = FakeSocket(
            [
                connect_frame(device),
                {"jsonrpc": "2.0", "id": "2", "method": "approval.request", "params": {"command_id": "c-1"}},
                RuntimeError("peer gone"),
            ]
        )

        await asyncio.wait_for(hub.serve(socket, device=device), timeout=10)

        errors = socket.errors()
        assert any(err.get("code") == "approval-intake-unavailable" and err["retryable"] for err in errors)


# ─── delivery across replicas ───────────────────────────────────────────────


class TestCrossReplicaDelivery:
    async def test_a_command_minted_on_one_replica_reaches_the_socket_on_another(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        """Two hubs, one Redis, one socket. The minting hub owns no socket at all — which is the
        arrangement §11.10 describes, and the reason delivery is a stream rather than a local call."""
        owner = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        minter = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        # No hold sentinel here: an idle `FakeSocket` keeps the session open by spinning on an empty
        # inbound list, which is what lets the test feed `command.result` mid-session. A hold would
        # park the receive loop and the fed frame would queue up behind it forever — which is
        # exactly how this test failed first time round.
        socket = FakeSocket([connect_frame(device)])

        serving = asyncio.create_task(owner.serve(socket, device=device))
        try:
            while not owner.owns(device.device_id):
                await asyncio.sleep(0.005)
            assert not minter.owns(device.device_id)

            command = {
                "command_id": str(uuid.uuid4()),
                "device_id": str(device.device_id),
                "operation": "changeset.apply",
                "signature": "test-only-not-a-real-signature",
            }
            future = await minter.send_command(device_id=device.device_id, command=command)

            for _ in range(400):
                if socket.commands():
                    break
                await asyncio.sleep(0.01)
            delivered = socket.commands()
            assert delivered, "the command never reached the replica owning the socket"
            assert delivered[0]["params"]["command_id"] == command["command_id"]
            assert delivered[0]["params"]["signature"] == command["signature"]

            # The result travels back to the minting replica over the result channel.
            socket.feed(
                {
                    "jsonrpc": "2.0",
                    "id": "9",
                    "method": "command.result",
                    "params": {"command_id": command["command_id"], "status": "succeeded"},
                }
            )
            params = await future.result(timeout=10)
            assert params["status"] == "succeeded"
        finally:
            socket.feed(RuntimeError("peer gone"))
            with contextlib.suppress(Exception):
                await asyncio.wait_for(serving, timeout=10)

    async def test_delivery_to_a_device_with_no_live_session_is_refused(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        """The refusal `UnavailableCommandSink` used to give, kept. A transit that cannot deliver
        must leave the change set `approved` and retryable rather than `applying` with nothing in
        flight."""
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        with pytest.raises(ProblemException) as raised:
            await hub.send_command(
                device_id=device.device_id,
                command={"command_id": str(uuid.uuid4()), "device_id": str(device.device_id)},
            )
        assert raised.value.problem.status == 409
        # Nothing was enqueued, so a device that reconnects later does not receive an expired
        # envelope it can only reject.
        assert await redis_client.exists(f"forgeops:agentcmd:{device.device_id}") == 0

    async def test_a_queued_command_is_not_replayed_to_a_reconnecting_agent(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        """Reads from `$`. Every envelope carries `not_after` (§7.6), so a command written while the
        device was away is expired by the time it returns, and delivering it would produce an
        `envelope-expired` rejection indistinguishable from an attack."""
        stream = f"forgeops:agentcmd:{device.device_id}"
        await redis_client.xadd(
            stream,
            {"frame": json.dumps({"jsonrpc": "2.0", "id": "old", "method": "command.execute", "params": {}})},
        )
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        socket = FakeSocket([connect_frame(device), _HOLD])

        serving = asyncio.create_task(hub.serve(socket, device=device))
        try:
            while not hub.owns(device.device_id):
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.3)
            assert socket.commands() == []
        finally:
            socket.release.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(serving, timeout=10)


# ─── the proactive close ────────────────────────────────────────────────────


class TestBroadcastRevocation:
    async def test_the_announcement_closes_a_local_socket_promptly(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        socket = FakeSocket([connect_frame(device), _HOLD])
        serving = asyncio.create_task(hub.serve(socket, device=device))
        subscriber = asyncio.create_task(hub.subscribe_revocations(channel=REVOCATION_CHANNEL))
        try:
            while not hub.owns(device.device_id):
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.2)  # let the subscription settle
            await redis_client.publish(REVOCATION_CHANNEL, str(device.device_id))

            for _ in range(400):
                if any(f.get("method") == "agent.error" for f in socket.sent):
                    break
                await asyncio.sleep(0.01)
            assert any(
                f.get("method") == "agent.error" and f["params"]["code"] == "device-revoked" for f in socket.sent
            ), "the pub/sub announcement did not reach the local socket"
        finally:
            subscriber.cancel()
            socket.release.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(serving, timeout=10)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await subscriber

    async def test_an_announcement_for_a_device_elsewhere_is_a_no_op(
        self,
        redis_client: Any,
        device: StubDevice,
    ) -> None:
        hub = build_hub(redis_client, StubDirectory(redis_client), CollectingProgress())
        await hub.broadcast_revocation(uuid.uuid4())  # must not raise


class TestConstruction:
    def test_a_timeout_below_the_interval_is_refused(self, redis_client: Any) -> None:
        with pytest.raises(ValueError, match="heartbeat_timeout_seconds"):
            build_hub(redis_client, StubDirectory(redis_client), CollectingProgress(), interval=90, timeout=30)

    def test_a_hub_without_redis_is_refused(self) -> None:
        """seq, revocation and delivery are all Redis-side, so a hub without it could only pretend."""
        with pytest.raises(ValueError, match="Redis"):
            build_hub(None, object(), CollectingProgress())
