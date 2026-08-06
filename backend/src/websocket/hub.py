# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent WebSocket hub — the backend half of the JSON-RPC session (design.md §7.3, §11.10).

Deliberately thin. It is a transport-and-correlation layer, not a decision-maker.

What it does
------------
Runs the handshake, keeps heartbeats, checks the Redis revocation set **per inbound frame**
(Q-16), correlates `command.execute` → `command.result` by JSON-RPC id, fans `command.progress`
out for SSE, and delivers commands across replicas through a Redis stream keyed by device id.

What it must never do
---------------------
Decide whether an operation is allowed, mint authority, or sign an envelope. `send_command` is in
§2.2.1's banned-api table, so only `governance/` can name it, and this module holds no envelope
key and imports nothing that can fetch one. A hub that could originate a command would be a
second mutation path, and the phase's entire premise is that there is exactly one.

Why every collaborator arrives as a Protocol
--------------------------------------------
Not taste: `src.auth.devices` is banned outside `governance/` by §2.4's table, and the hub
genuinely needs three things from the device service — authenticate a peer, check revocation,
rotate a certificate. Declaring the seam here, in the consumer, is what lets the composition root
pass the real `DeviceService` without this module naming it. The same shape `identity.
CredentialSource` uses on the agent side, for the same reason.

Why there is no tenth JSON-RPC method
-------------------------------------
§7.3 fixes nine. Certificate rotation therefore rides `session.heartbeat`: an agent that includes
a `csr` in its heartbeat params gets a fresh certificate in the heartbeat result (D-80). The
rejected alternative was a `session.rotate` method, which would have been a tenth — and the reason
the catalogue is closed is that a reader can enumerate every message that exists.

Multi-replica behaviour
-----------------------
A device is connected to exactly one replica, and the chokepoint may run on another. Delivery goes
through a Redis stream keyed by device id, which the owning replica consumes — the same "Redis is
the shared state, processes are stateless" arrangement Phase 0 used for the MCP gateway. Results
travel back over a pub/sub channel keyed by command id, so the replica that minted an envelope can
await its result without owning the socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol, runtime_checkable

from sqlalchemy import text

from ..core.errors import problem

__all__ = [
    "AGENT_ORIGINATED_METHODS",
    "BACKEND_ORIGINATED_METHODS",
    "CLOSE_HANDSHAKE_FAILED",
    "CLOSE_HEARTBEAT_TIMEOUT",
    "CLOSE_REVOKED",
    "CLOSE_UNAUTHENTICATED",
    "JSONRPC_METHODS",
    "AgentHub",
    "ClientCertificateSource",
    "CommandFuture",
    "DeviceDirectory",
    "HubDeps",
    "ProgressSink",
    "RedisProgressSink",
    "SessionRegistry",
    "TlsPeerCertificate",
]

logger = logging.getLogger(__name__)

# ─── the vocabulary §7.3 fixes ───────────────────────────────────────────────

#: The nine methods, and nothing else. A frozenset rather than a comment, because "no tenth
#: method" is a property the dispatch table can be asserted against.
JSONRPC_METHODS: Final[frozenset[str]] = frozenset(
    {
        "session.connect",
        "session.heartbeat",
        "command.execute",
        "command.result",
        "command.progress",
        "approval.request",
        "approval.response",
        "agent.error",
        "agent.status",
    }
)

#: What an agent may originate. `command.execute` and `approval.response` are absent on purpose:
#: an agent that could send itself a command would be authorising its own mutation.
AGENT_ORIGINATED_METHODS: Final[frozenset[str]] = frozenset(
    {
        "session.connect",
        "session.heartbeat",
        "command.result",
        "command.progress",
        "approval.request",
        "agent.status",
        "agent.error",
    }
)

#: What the backend may originate.
BACKEND_ORIGINATED_METHODS: Final[frozenset[str]] = frozenset({"command.execute", "approval.response", "agent.error"})

# ─── close codes ─────────────────────────────────────────────────────────────
#
# 4401/4403 are §3.1's own numbers. The 44xx range is application-defined, and using HTTP's
# semantics inside it means an operator reading a close code does not need a second table.

CLOSE_UNAUTHENTICATED: Final[int] = 4401
CLOSE_REVOKED: Final[int] = 4403
CLOSE_HANDSHAKE_FAILED: Final[int] = 4400
CLOSE_HEARTBEAT_TIMEOUT: Final[int] = 4408

#: Redis key shapes. All prefixed, so one Redis can serve more than this product.
_SESSION_KEY: Final[str] = "forgeops:agentsession:{device_id}"
_COMMAND_STREAM: Final[str] = "forgeops:agentcmd:{device_id}"
_RESULT_CHANNEL: Final[str] = "forgeops:cmdresult:{command_id}"
_PROGRESS_CHANNEL: Final[str] = "forgeops:sse:command:{command_id}"

#: How long a delivered-but-unread command stays on the stream.
#:
#: Bounded by trimming rather than by a TTL: a stream key with a TTL would drop commands that are
#: still in flight when it expired. `MAXLEN ~ 256` keeps a disconnected device's backlog finite
#: while leaving enough room that a burst is not silently truncated.
_STREAM_MAXLEN: Final[int] = 256


@runtime_checkable
class DeviceDirectory(Protocol):
    """What the hub needs from the device service, and nothing more.

    Three methods, declared in the consumer. `authenticate_session` and `is_revoked` are the
    handshake and the per-message guarantee; `rotate_certificate` is the live-session renewal
    §3.1 requires so a reconnect never needs to re-pair.
    """

    async def authenticate_session(self, session: Any, *, certificate_pem: bytes, device_token: str) -> Any: ...

    async def is_revoked(self, device_id: uuid.UUID) -> bool: ...

    async def rotate_certificate(self, session: Any, *, device_id: uuid.UUID, csr_pem: bytes) -> Any: ...


@runtime_checkable
class ProgressSink(Protocol):
    """Where `command.progress` goes on its way to an SSE stream (§7.5, §11.11)."""

    async def publish(self, *, command_id: str, event: Mapping[str, Any]) -> None: ...


class RedisProgressSink:
    """Publishes progress as an SSE-shaped event on a per-command channel.

    Publishes rather than stores. §7.5's rule is that an SSE stream is a *view* and never a source
    of truth — the change-set state lives in Postgres — so a dropped progress frame costs a
    smoother progress bar and nothing else. Storing progress would create a second copy of state
    that the REST surface would then have to reconcile.

    The SSE producer route that subscribes to this channel is leaf 11.x's; until it exists the
    events are published to nobody, which is visible here rather than implied.
    """

    def __init__(self, redis: Any) -> None:
        if redis is None:
            raise ValueError("RedisProgressSink requires a Redis client")
        self._redis = redis

    async def publish(self, *, command_id: str, event: Mapping[str, Any]) -> None:
        payload = json.dumps({"event": "progress", "data": dict(event)}, separators=(",", ":"))
        with contextlib.suppress(Exception):
            # Suppressed deliberately, and only here: a progress frame that cannot be published
            # must not fail the command it describes. Every other Redis failure in this module
            # closes the socket.
            await self._redis.publish(_PROGRESS_CHANNEL.format(command_id=command_id), payload)


@dataclass(slots=True)
class CommandFuture:
    """The pending result of one delivered command, correlated by `command_id`.

    §11.10 gives `send_command` this return type. It resolves from whichever replica owns the
    socket: the owner publishes the result on `_RESULT_CHANNEL`, and the replica that minted the
    envelope is already subscribed by the time the stream entry is written — subscribing after
    the send would be a race in which a fast agent's result arrives before anybody is listening.
    """

    command_id: str
    _future: asyncio.Future[Mapping[str, Any]]

    async def result(self, timeout: float | None = None) -> Mapping[str, Any]:
        """Await the agent's `command.result` params.

        A timeout raises `asyncio.TimeoutError` rather than returning a synthetic failure: a
        caller that cannot tell "the agent said it failed" from "the agent said nothing" would
        record the wrong outcome, and those two need different operator responses.
        """
        if timeout is None:
            return await self._future
        return await asyncio.wait_for(asyncio.shield(self._future), timeout)

    def done(self) -> bool:
        return self._future.done()


@dataclass(slots=True)
class _LocalSession:
    """One socket this replica owns."""

    device_id: uuid.UUID
    session_id: str
    send: Callable[[Mapping[str, Any]], Any]
    pending: dict[str, asyncio.Future[Mapping[str, Any]]] = field(default_factory=dict)
    closing: bool = False


@runtime_checkable
class SessionRegistry(Protocol):
    """The sockets this process owns. A Protocol so tests can observe it without reaching in."""

    def owns(self, device_id: uuid.UUID) -> bool: ...


@runtime_checkable
class ClientCertificateSource(Protocol):
    """How the handshake obtains the peer's certificate.

    A seam rather than a direct read of the TLS transport, because the certificate reaches the
    process differently depending on where TLS terminates, and the wrong answer must be "no
    certificate" rather than "trust this header". A deployment behind a proxy composes a source
    that reads the proxy's verified-client header; the default reads the TLS peer certificate and
    returns `None` when there is not one.
    """

    def certificate_pem(self, scope: Mapping[str, Any]) -> bytes | None: ...


class TlsPeerCertificate:
    """Reads the client certificate from the TLS connection this process terminated.

    Returns `None` — never a guess — when the socket is plaintext or the peer sent nothing, and
    the hub turns that into a refused handshake. **No header is trusted by default.** An
    `X-Forwarded-Client-Cert`-style source is a deployment's deliberate composition, because a
    header is caller-supplied data unless a proxy is known to strip and rewrite it, and a hub that
    accepted one by default would authenticate anybody who could reach the port.
    """

    def certificate_pem(self, scope: Mapping[str, Any]) -> bytes | None:
        transport = scope.get("transport")
        ssl_object = None
        if transport is not None and hasattr(transport, "get_extra_info"):
            ssl_object = transport.get_extra_info("ssl_object")
        if ssl_object is None:
            # Some servers put the parsed certificate straight into the scope's extensions.
            extensions = scope.get("extensions") or {}
            tls = extensions.get("tls") if isinstance(extensions, Mapping) else None
            if isinstance(tls, Mapping):
                chain = tls.get("client_certificate_chain")
                if isinstance(chain, list | tuple) and chain:
                    leaf = chain[0]
                    return leaf.encode("utf-8") if isinstance(leaf, str) else bytes(leaf)
            return None
        der = ssl_object.getpeercert(binary_form=True)
        if not der:
            return None
        return ssl.DER_cert_to_PEM_cert(der).encode("utf-8")


async def _close_pubsub(pubsub: Any) -> None:
    """Close a pub/sub connection across redis-py versions.

    `close()` is deprecated in favour of `aclose()` since redis 5.0.1, and the lock file pins one
    version — but a subscription left open leaks a pooled connection per revoked device, so this
    prefers the current spelling and keeps the old one rather than choosing between a deprecation
    warning and a leak.
    """
    closer = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
    if closer is None:
        return
    with contextlib.suppress(Exception):
        await closer()


def _utc_now() -> datetime:
    """The hub's clock. A named function rather than a lambda default, so a test can replace it
    and a reader can see there is exactly one source of `server_time`."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class HubDeps:
    """Everything the hub needs, composed once in `create_app`'s lifespan.

    A frozen struct rather than eleven constructor arguments, so adding a collaborator in a later
    leaf does not rewrite every construction site — and so a half-composed hub is impossible to
    build by accident.
    """

    redis: Any
    devices: DeviceDirectory
    sessionmaker: Callable[[], Any]
    progress: ProgressSink
    heartbeat_interval_seconds: int = 30
    heartbeat_timeout_seconds: int = 90
    clock: Callable[[], datetime] = _utc_now


class AgentHub:
    """§11.10's hub. One instance per process, holding the sockets that process owns."""

    def __init__(self, deps: HubDeps) -> None:
        if deps.redis is None:
            raise ValueError("AgentHub requires a Redis client: seq, revocation and delivery are all Redis-side")
        if deps.heartbeat_timeout_seconds <= deps.heartbeat_interval_seconds:
            raise ValueError(
                "heartbeat_timeout_seconds must exceed heartbeat_interval_seconds; otherwise "
                "every healthy agent is dropped between beats"
            )
        self._deps = deps
        self._sessions: dict[uuid.UUID, _LocalSession] = {}
        #: Futures for commands this replica minted, keyed by command id. Separate from a
        #: session's `pending`, because the minting replica need not own the socket.
        self._awaiting: dict[str, asyncio.Future[Mapping[str, Any]]] = {}

    # ── introspection ─────────────────────────────────────────────────────────────────────

    def owns(self, device_id: uuid.UUID) -> bool:
        """Whether this process holds the socket for `device_id`."""
        return device_id in self._sessions

    @property
    def connected_devices(self) -> tuple[uuid.UUID, ...]:
        return tuple(self._sessions)

    # ── the session ───────────────────────────────────────────────────────────────────────

    async def serve(self, ws: Any, *, device: Any) -> None:
        """Run one authenticated session until it closes (§3.1, §7.3, §7.4).

        `device` is the `AuthenticatedDevice` the route's handshake produced. The hub does not
        authenticate — it is handed an already-authenticated peer — because authentication needs
        the device service, and a hub that imported it would be a hub that could fetch an envelope
        key.
        """
        device_id: uuid.UUID = device.device_id
        session_id = str(uuid.uuid4())

        connected = await self._handshake(ws, device=device, session_id=session_id)
        if not connected:
            return

        local = _LocalSession(device_id=device_id, session_id=session_id, send=ws.send_json)
        self._sessions[device_id] = local
        delivery = asyncio.create_task(self._deliver_from_stream(local))
        try:
            await self._receive_loop(ws, local, device)
        finally:
            delivery.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await delivery
            self._sessions.pop(device_id, None)
            await self._drop_session_key(device_id)

    async def _handshake(self, ws: Any, *, device: Any, session_id: str) -> bool:
        """Require `session.connect` as the first frame, and answer it (§3.1).

        Returns False when the socket has been closed. The device id in the params is compared
        with the authenticated one: a frame that claims to be another device is not a mistake to
        tolerate, because everything downstream keys off that id.
        """
        timeout = float(self._deps.heartbeat_timeout_seconds)
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        except TimeoutError:
            await self._close(ws, CLOSE_HANDSHAKE_FAILED, "no session.connect within the heartbeat timeout")
            return False
        except Exception:
            return False

        method = str(first.get("method") or "")
        if method != "session.connect":
            await self._reject(
                ws,
                request_id=first.get("id"),
                code="handshake-required",
                message="the first frame must be session.connect (§7.3)",
                close_code=CLOSE_HANDSHAKE_FAILED,
            )
            return False

        params = first.get("params") or {}
        claimed = str(params.get("device_id") or "")
        if claimed and claimed != str(device.device_id):
            await self._reject(
                ws,
                request_id=first.get("id"),
                code="device-mismatch",
                message="session.connect names a different device than the client certificate",
                close_code=CLOSE_UNAUTHENTICATED,
            )
            return False

        # Revocation is checked here as well as per message: a device revoked between the
        # certificate check and the first frame must not get a session id.
        if not await self._live(ws, device.device_id):
            return False

        active_digest = await self._active_bundle_digest(device.project_id)
        presented = str(params.get("policy_bundle_digest") or "")
        stale = bool(active_digest) and presented != active_digest

        await self._refresh_session_key(device.device_id, session_id)
        await self._touch_last_seen(device.device_id, agent_version=params.get("agent_version"))

        result: dict[str, Any] = {
            "session_id": session_id,
            "heartbeat_interval": self._deps.heartbeat_interval_seconds,
            "heartbeat_timeout": self._deps.heartbeat_timeout_seconds,
            "seq_base": int(getattr(device, "last_seq", 0) or 0),
            "server_time": self._now_iso(),
        }
        if stale:
            # §3.1 returns the bundle itself here. `PolicyBundleService.publish` is leaf 9.3, so
            # what this can honestly say is *that* the digest is stale and what the current one
            # is. The bundle body is absent rather than empty: D-30 makes a missing bundle a DENY
            # on the agent side, so a zero-byte bundle would be a field that means "refuse
            # everything" while looking like a bundle.
            result["policy_bundle_stale"] = True
            result["policy_bundle_digest"] = active_digest
        else:
            result["policy_bundle_digest"] = presented or active_digest or ""

        await self._respond(ws, first.get("id"), result)
        logger.info(
            "agent session connected",
            extra={"device_id": str(device.device_id), "session_id": session_id, "bundle_stale": stale},
        )
        return True

    async def _receive_loop(self, ws: Any, local: _LocalSession, device: Any) -> None:
        """One frame at a time, with the heartbeat deadline and the per-message revocation check.

        The revocation check runs **before the frame is dispatched** — not after, and not once per
        connection. A revoked device's next frame must have no effect, and the only way to
        guarantee that is to refuse before anything reads it (Q-16).
        """
        timeout = float(self._deps.heartbeat_timeout_seconds)
        while True:
            try:
                frame = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
            except TimeoutError:
                logger.info(
                    "agent session heartbeat timeout",
                    extra={"device_id": str(local.device_id), "timeout_seconds": timeout},
                )
                await self._close(ws, CLOSE_HEARTBEAT_TIMEOUT, "no frame within the heartbeat timeout")
                return
            except Exception:
                return  # the peer went away; the finally block in serve cleans up

            if not await self._live(ws, local.device_id):
                return

            method = str(frame.get("method") or "")
            if method and method not in AGENT_ORIGINATED_METHODS:
                await self._reject(
                    ws,
                    request_id=frame.get("id"),
                    code="method-not-allowed",
                    message=(
                        f"{method!r} is not an agent-originated method; §7.3 fixes the catalogue and its direction"
                    ),
                    close_code=None,
                )
                continue

            try:
                await self._dispatch(ws, local, device, frame, method)
            except Exception:  # noqa: BLE001 - one bad frame must not drop a healthy session
                logger.exception("agent frame handling failed", extra={"device_id": str(local.device_id)})
                await self._reject(
                    ws,
                    request_id=frame.get("id"),
                    code="internal",
                    message="the frame could not be handled",
                    close_code=None,
                )

    async def _dispatch(
        self, ws: Any, local: _LocalSession, device: Any, frame: Mapping[str, Any], method: str
    ) -> None:
        """Route one agent-originated frame. A table, not a chain of ifs."""
        params = frame.get("params") or {}
        request_id = frame.get("id")

        if method == "session.heartbeat":
            await self._on_heartbeat(ws, local, device, params, request_id)
        elif method == "command.result":
            await self._on_result(local, params)
            await self._respond(ws, request_id, {"accepted": True})
        elif method == "command.progress":
            await self._deps.progress.publish(command_id=str(params.get("command_id") or ""), event=dict(params))
        elif method == "agent.status":
            await self._touch_last_seen(local.device_id, agent_version=params.get("agent_version"))

            # Drift detection
            reported_digest = params.get("policy_bundle_digest")
            if reported_digest is not None:
                active = await self._active_bundle_digest(device.project_id)
                if active and reported_digest != active:
                    # Update status to POLICY_STALE
                    async with self._session_scope() as session:
                        from sqlmodel import update

                        from src.auth.device_models import AgentDevice, DeviceStatus

                        stmt = (
                            update(AgentDevice)
                            .where(AgentDevice.id == local.device_id)
                            .values(status=DeviceStatus.POLICY_STALE)
                        )
                        await session.execute(stmt)
                        await session.commit()

            await self._respond(ws, request_id, {"server_time": self._now_iso()})
        elif method == "agent.error":
            logger.warning(
                "agent reported an error",
                extra={
                    "device_id": str(local.device_id),
                    "agent_error_code": str(params.get("code") or ""),
                    "retryable": bool(params.get("retryable")),
                },
            )
        elif method == "approval.request":
            # The intake that turns this into a chokepoint transit is not built yet: the
            # chokepoint's entry points are `submit`, `approve` and `revert`, none of which takes
            # an agent-originated intent. Answering with a retryable error is the honest state —
            # the agent's journal keeps the intent queued (D-41) rather than believing it was
            # accepted and dropping it.
            await self._reject(
                ws,
                request_id=request_id,
                code="approval-intake-unavailable",
                message=(
                    "the backend cannot accept an agent-originated approval request yet; the "
                    "intent stays queued and will be replayed (design §7.3, D-41)"
                ),
                close_code=None,
                retryable=True,
            )
        elif method == "session.connect":
            await self._reject(
                ws,
                request_id=request_id,
                code="already-connected",
                message="this session is already established; session.connect is the first frame only",
                close_code=None,
            )

    async def _on_heartbeat(
        self, ws: Any, local: _LocalSession, device: Any, params: Mapping[str, Any], request_id: Any
    ) -> None:
        """Refresh the session key's TTL and answer with the server clock (§7.3, §7.4).

        `server_time` is what makes the agent's ±60 s skew check diagnosable: the agent compares
        it with its own clock and reports the difference in `agent.status`, so `agent doctor` can
        say "your clock is four minutes fast" instead of "signature invalid" (§7.6).
        """
        await self._refresh_session_key(local.device_id, local.session_id)
        await self._touch_last_seen(local.device_id, queue_depth=params.get("queue_depth"))

        result: dict[str, Any] = {
            "server_time": self._now_iso(),
            "policy_bundle_digest": await self._active_bundle_digest(device.project_id) or "",
        }

        csr = params.get("csr")
        if csr:
            # Rotation over the live session (§3.1), riding the heartbeat rather than a tenth
            # method (D-80). A refusal is reported in the result rather than raised: a device
            # whose rotation was refused is still connected, and dropping the socket would turn a
            # certificate problem into a reconnect loop.
            result["certificate"] = await self._rotate(local.device_id, str(csr).encode("utf-8"))

        await self._respond(ws, request_id, result)

    async def _rotate(self, device_id: uuid.UUID, csr_pem: bytes) -> Mapping[str, Any]:
        async with self._session_scope() as session:
            try:
                bundle = await self._deps.devices.rotate_certificate(session, device_id=device_id, csr_pem=csr_pem)
            except Exception as exc:  # noqa: BLE001 - every refusal is reported the same way
                logger.warning(
                    "certificate rotation refused",
                    extra={"device_id": str(device_id), "reason": type(exc).__name__},
                )
                return {"rotated": False, "reason": str(exc)}
            await session.commit()
        return {
            "rotated": True,
            "client_cert": bundle.certificate_pem.decode("utf-8"),
            "ca_bundle": bundle.ca_bundle_pem.decode("utf-8"),
            "cert_serial": bundle.serial,
            "cert_fingerprint": bundle.fingerprint,
            "cert_not_after": bundle.not_after.isoformat(),
            "renew_after": bundle.renew_after.isoformat(),
        }

    async def _on_result(self, local: _LocalSession, params: Mapping[str, Any]) -> None:
        """Resolve the waiting future locally, and republish for a replica that is waiting.

        Both, unconditionally, because the hub cannot tell which replica minted the envelope. A
        local `set_result` is free, and the publish is one small Redis call — the alternative
        would be storing the minting replica's identity in the stream entry and trusting it to
        still be alive.
        """
        command_id = str(params.get("command_id") or "")
        if not command_id:
            return
        for pending in (local.pending, self._awaiting):
            future = pending.pop(command_id, None)
            if future is not None and not future.done():
                future.set_result(dict(params))
        with contextlib.suppress(Exception):
            await self._deps.redis.publish(
                _RESULT_CHANNEL.format(command_id=command_id),
                json.dumps(dict(params), separators=(",", ":")),
            )

    # ── delivery (§2.2.1's confined surface) ──────────────────────────────────────────────

    async def send_command(self, *, device_id: uuid.UUID, command: Any) -> CommandFuture:
        """Deliver one signed envelope to a device, wherever its socket lives (§11.10).

        In §2.2.1's banned-api table: naming this function outside `governance/` is a build
        failure, because a caller that can reach it can make an agent execute. It signs nothing
        and checks no policy — it is the transport for something already authorised.

        Delivery is always through the Redis stream, even when this replica owns the socket. One
        path rather than two: a local fast path would be a second delivery implementation whose
        ordering could differ from the remote one, and a command delivered out of order is
        indistinguishable to the agent from a replay.
        """
        envelope = command.as_wire() if hasattr(command, "as_wire") else dict(command)
        command_id = str(envelope.get("command_id") or uuid.uuid4())

        # Refuse rather than enqueue for a device with no live session, and fail closed when Redis
        # cannot answer. This preserves exactly what `UnavailableCommandSink` guaranteed before the
        # hub existed: a transit that cannot deliver leaves the change set `approved` and retryable
        # instead of `applying` with nothing in flight. The session key's TTL is the heartbeat
        # timeout, so "connected" means "beat inside the window" rather than "once connected".
        if not await self._has_live_session(device_id):
            raise problem(
                "device-not-connected",
                detail=(
                    f"device {device_id} has no live agent session, so a signed command cannot be "
                    "delivered. The change set is approved and its rollback handle is reserved, so "
                    "the apply can be retried once the agent reconnects (design §11.10)."
                ),
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Mapping[str, Any]] = loop.create_future()
        self._awaiting[command_id] = future

        # Subscribed BEFORE the stream write, so a result that arrives immediately is not missed.
        # The reverse order is a race that only shows up under load, which is the worst kind.
        listener = asyncio.create_task(self._await_remote_result(command_id, future))
        try:
            await self._deps.redis.xadd(
                _COMMAND_STREAM.format(device_id=device_id),
                {
                    "frame": json.dumps(
                        {"jsonrpc": "2.0", "id": command_id, "method": "command.execute", "params": envelope},
                        separators=(",", ":"),
                    )
                },
                maxlen=_STREAM_MAXLEN,
                approximate=True,
            )
        except Exception:
            listener.cancel()
            self._awaiting.pop(command_id, None)
            raise
        future.add_done_callback(lambda _f: listener.cancel())
        return CommandFuture(command_id=command_id, _future=future)

    async def _has_live_session(self, device_id: uuid.UUID) -> bool:
        """Whether any replica holds a heartbeating session for this device.

        Read from Redis rather than from `self._sessions`, because the socket may belong to another
        replica and the chokepoint may run on this one. An unreadable Redis is `False` — refuse —
        which is the same direction every other unavailability in this module takes.
        """
        if device_id in self._sessions:
            return True
        try:
            return bool(await self._deps.redis.exists(_SESSION_KEY.format(device_id=device_id)))
        except Exception:  # noqa: BLE001 - unavailable means refuse
            logger.warning("session registry unreadable; refusing delivery", extra={"device_id": str(device_id)})
            return False

    async def _await_remote_result(self, command_id: str, future: asyncio.Future[Mapping[str, Any]]) -> None:
        """Resolve `future` from the result channel, for a command whose socket is elsewhere."""
        channel = _RESULT_CHANNEL.format(command_id=command_id)
        pubsub = self._deps.redis.pubsub()
        try:
            await pubsub.subscribe(channel)
            while not future.done():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    continue
                data = message.get("data")
                if isinstance(data, bytes | bytearray):
                    data = data.decode("utf-8")
                if not isinstance(data, str):
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    payload = json.loads(data)
                    if not future.done():
                        future.set_result(payload)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dead subscription must not take the caller with it
            logger.debug("result subscription ended", extra={"command_id": command_id})
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel)
            await _close_pubsub(pubsub)

    async def _deliver_from_stream(self, local: _LocalSession) -> None:
        """Consume this device's command stream and write each frame to its socket.

        Reads from `$` — new entries only. A backlog written while the device was offline is
        deliberately not delivered on reconnect: every envelope carries `not_after` (§7.6), so a
        queued command is expired by the time a disconnected agent returns, and delivering it
        would produce a `envelope-expired` rejection that looks like an attack.
        """
        stream = _COMMAND_STREAM.format(device_id=local.device_id)
        last_id = "$"
        while not local.closing:
            try:
                entries = await self._deps.redis.xread({stream: last_id}, count=16, block=1000)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a Redis blip must not close a healthy socket
                await asyncio.sleep(0.5)
                continue
            if not entries:
                continue
            for _stream_name, records in entries:
                for entry_id, fields in records:
                    last_id = entry_id if isinstance(entry_id, str) else entry_id.decode("utf-8")
                    raw = fields.get("frame") or fields.get(b"frame")
                    if isinstance(raw, bytes | bytearray):
                        raw = raw.decode("utf-8")
                    if not raw:
                        continue
                    with contextlib.suppress(json.JSONDecodeError):
                        frame = json.loads(raw)
                        command_id = str(frame.get("id") or "")
                        if command_id:
                            loop = asyncio.get_running_loop()
                            local.pending.setdefault(command_id, loop.create_future())
                        await local.send(frame)

    async def broadcast_revocation(self, device_id: uuid.UUID) -> None:
        """Close this replica's socket for `device_id`, if it holds one (§3.1's proactive half).

        Only the local socket. The announcement that reaches other replicas is the pub/sub event
        the device service publishes when it revokes; `subscribe_revocations` is what turns that
        event into this call. Neither is the guarantee — the per-message `SISMEMBER` is — and
        saying so here is what keeps a future change from quietly making promptness load-bearing.
        """
        local = self._sessions.get(device_id)
        if local is None:
            return
        local.closing = True
        with contextlib.suppress(Exception):
            await local.send(
                {
                    "jsonrpc": "2.0",
                    "method": "agent.error",
                    "params": {"code": "device-revoked", "message": "this device has been revoked", "retryable": False},
                }
            )

    async def subscribe_revocations(self, *, channel: str) -> None:
        """Long-running task: close local sockets as revocations are announced.

        Started by the lifespan. Runs forever, and every failure is logged and retried rather than
        raised: this task is an optimisation, and a crashed optimiser must not take the process
        with it while the per-message check is still enforcing correctness.
        """
        while True:
            pubsub = self._deps.redis.pubsub()
            try:
                await pubsub.subscribe(channel)
                async for device_id in self._revocation_events(pubsub):
                    await self.broadcast_revocation(device_id)
            except asyncio.CancelledError:
                await _close_pubsub(pubsub)
                raise
            except Exception:  # noqa: BLE001
                logger.warning("revocation subscription failed; retrying", exc_info=True)
                await asyncio.sleep(1.0)
            finally:
                await _close_pubsub(pubsub)

    async def _revocation_events(self, pubsub: Any) -> AsyncIterator[uuid.UUID]:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is None:
                continue
            data = message.get("data")
            if isinstance(data, bytes | bytearray):
                data = data.decode("utf-8")
            try:
                yield uuid.UUID(str(data))
            except (ValueError, AttributeError, TypeError):
                continue

    # ── small helpers ─────────────────────────────────────────────────────────────────────

    async def _live(self, ws: Any, device_id: uuid.UUID) -> bool:
        """The per-message revocation check, failing closed (Q-16).

        Any failure to *read* the revocation set closes the socket, because the device service
        raises rather than answering `False` — an agent that cannot be checked does not get to
        act.
        """
        try:
            revoked = await self._deps.devices.is_revoked(device_id)
        except Exception as exc:  # noqa: BLE001 - unavailable means refuse
            logger.warning(
                "revocation check unavailable; closing the session",
                extra={"device_id": str(device_id), "reason": str(exc)},
            )
            await self._reject(
                ws,
                request_id=None,
                code="revocation-unavailable",
                message="revocation cannot be checked right now, so this session is closed",
                close_code=CLOSE_REVOKED,
                retryable=True,
            )
            return False
        if revoked:
            await self._reject(
                ws,
                request_id=None,
                code="device-revoked",
                message="this device has been revoked",
                close_code=CLOSE_REVOKED,
            )
            return False
        return True

    async def _respond(self, ws: Any, request_id: Any, result: Mapping[str, Any]) -> None:
        """A JSON-RPC result, or nothing at all for a notification.

        `id is None` means the agent sent a notification, and JSON-RPC forbids answering one. A
        response with a null id is the shape that makes a client's correlation table grow without
        bound.
        """
        if request_id is None:
            return
        await ws.send_json({"jsonrpc": "2.0", "id": request_id, "result": dict(result)})

    async def _reject(
        self,
        ws: Any,
        *,
        request_id: Any,
        code: str,
        message: str,
        close_code: int | None,
        retryable: bool = False,
    ) -> None:
        """Send `agent.error` — and close, when `close_code` is given.

        `code` values mirror the RFC 9457 suffixes (Appendix C.2) so one vocabulary covers both
        transports: an operator who has seen `device-revoked` in a problem document does not have
        to learn a second name for it here.
        """
        payload = {"code": code, "message": message, "retryable": retryable}
        with contextlib.suppress(Exception):
            if request_id is None:
                await ws.send_json({"jsonrpc": "2.0", "method": "agent.error", "params": payload})
            else:
                await ws.send_json(
                    {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": message, "data": payload}}
                )
        if close_code is not None:
            await self._close(ws, close_code, code)

    async def _close(self, ws: Any, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await ws.close(code=code, reason=reason[:120])

    def _now_iso(self) -> str:
        return self._deps.clock().astimezone(UTC).isoformat()

    @contextlib.asynccontextmanager
    async def _session_scope(self) -> AsyncIterator[Any]:
        session = self._deps.sessionmaker()
        try:
            yield session
        finally:
            with contextlib.suppress(Exception):
                await session.close()

    async def _refresh_session_key(self, device_id: uuid.UUID, session_id: str) -> None:
        """`SET forgeops:agentsession:<device> <session_id> EX <timeout>` (§3.1's heartbeat step).

        The TTL is the heartbeat timeout, so the key's presence means "this device beat within the
        window" without anything having to write an absence. A missing key is how another replica
        learns a device is gone; a background sweeper would be a second source of that truth.
        """
        with contextlib.suppress(Exception):
            await self._deps.redis.set(
                _SESSION_KEY.format(device_id=device_id),
                session_id,
                ex=self._deps.heartbeat_timeout_seconds,
            )

    async def _drop_session_key(self, device_id: uuid.UUID) -> None:
        with contextlib.suppress(Exception):
            await self._deps.redis.delete(_SESSION_KEY.format(device_id=device_id))

    async def _touch_last_seen(
        self, device_id: uuid.UUID, *, agent_version: Any = None, queue_depth: Any = None
    ) -> None:
        """Record contact on `agent_devices.last_seen`.

        Its own short transaction rather than the caller's: a heartbeat must not be able to hold a
        row lock for the length of a session, and a failed `last_seen` write must not drop a
        healthy socket. The column is operational metadata — the chokepoint's device selection
        orders by it — and never an authorisation input.
        """
        del queue_depth  # accepted from the wire; §6.3 has no column for it
        try:
            async with self._session_scope() as session:
                if agent_version:
                    await session.execute(
                        text("UPDATE agent_devices SET last_seen = now(), agent_version = :v WHERE id = :id"),
                        {"id": device_id, "v": str(agent_version)[:64]},
                    )
                else:
                    await session.execute(
                        text("UPDATE agent_devices SET last_seen = now() WHERE id = :id"),
                        {"id": device_id},
                    )
                await session.commit()
        except Exception:  # noqa: BLE001 - operational metadata; never worth a dropped session
            logger.debug("last_seen update failed", extra={"device_id": str(device_id)})

    async def _active_bundle_digest(self, project_id: uuid.UUID | None) -> str:
        """The project's active policy-bundle digest, or the global one.

        The same query the chokepoint's `_active_bundle_digest` runs, and deliberately a second
        copy rather than an import: `src.governance` is not this domain, the two answers must
        agree in *value* rather than share a call path, and the hub's copy is read-only. If they
        ever disagree the chokepoint's answer is the one that decides a mutation — this one only
        decides what the handshake reports.
        """
        if project_id is None:
            return ""
        try:
            async with self._session_scope() as session:
                result = await session.execute(
                    text(
                        "SELECT digest FROM policy_bundles WHERE active AND "
                        "(project_id = :project OR project_id IS NULL) "
                        "ORDER BY (project_id IS NULL), created_at DESC LIMIT 1"
                    ),
                    {"project": project_id},
                )
                row = result.first()
                return "" if row is None else str(row[0])
        except Exception:  # noqa: BLE001 - an unreadable bundle table reports "unknown"
            logger.debug("active bundle digest unavailable", extra={"project_id": str(project_id)})
            return ""
