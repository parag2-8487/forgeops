# SPDX-License-Identifier: FSL-1.1-ALv2
"""Per-device envelope `seq` allocation and nonce reservation (design.md §7.6, §11.10).

Why this lands with the chokepoint rather than with the hub
----------------------------------------------------------
§7.6 makes `seq` allocation Redis-authoritative via a Lua compare-and-set, and leaf 8.4
lists that allocation among the hub's responsibilities. But an envelope cannot be *minted*
without a `seq` and a nonce — both are signed members (§7.6) — so the chokepoint needs the
allocator one wave before the hub exists. Implementing it here and letting 8.4 consume it
is the same disposition leaves 2.5, 4.2, 7.3 and 7.6 already carry: the leaf's wording
reached a wave forward, not its work.

Why Redis is authoritative and the column is not
------------------------------------------------
`agent_devices.last_seq` is a mirror kept for forensics (§6.3's own comment). Two API
replicas allocating from the column would need a row lock per envelope and would still
disagree with the agent, whose replay rejection is `seq ≤ last_seq` against what it has
*seen*. Redis holds the high-water mark and a Lua script advances it atomically, which is
the identical arrangement Phase 0 used for its TTL cache and Tasks state machine.

The two conditions are separate calls on purpose
------------------------------------------------
`next_seq` advances a counter; `reserve_nonce` refuses a repeat. §7.6 lists them as
independent replay conditions and both are required, so folding them into one call would
make "the nonce was fresh" and "the sequence advanced" a single outcome — and a caller
could no longer tell which of the two refused.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any, Final, Protocol, runtime_checkable

__all__ = [
    "NONCE_HEX_LENGTH",
    "EnvelopeSequencer",
    "NonceCollisionError",
    "RedisEnvelopeSequencer",
    "SequenceUnavailableError",
    "generate_nonce",
]

#: 128 bits rendered as 32 lowercase hex characters — the spelling the committed fixture
#: corpus uses (`agent/testdata/envelopes/*.json`), so one logical nonce has one form on
#: both sides of the protocol.
NONCE_HEX_LENGTH: Final[int] = 32

#: The Lua script §7.6 calls a compare-and-set. `INCR` alone would be enough for
#: monotonicity, but not for the guarantee that matters: the returned value must be
#: strictly greater than every value ever returned for this device **and** greater than
#: the floor the caller knows about from the database mirror. A device row restored from a
#: backup with a higher `last_seq` than Redis would otherwise hand out sequence numbers the
#: agent has already seen and will reject as replays — an outage that looks like an attack.
_ADVANCE_SEQ: Final[str] = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local floor = tonumber(ARGV[1])
if floor > current then
  current = floor
end
current = current + 1
redis.call('SET', KEYS[1], current)
return current
"""


class SequenceUnavailableError(RuntimeError):
    """The sequence high-water mark could not be advanced.

    A hard failure rather than a fallback to an in-process counter. An in-process counter
    would produce duplicate `seq` values across replicas, and a duplicate `seq` is
    indistinguishable at the agent from a replay attempt.
    """


class NonceCollisionError(RuntimeError):
    """The nonce is already reserved for this device inside the freshness window."""


def generate_nonce() -> str:
    """A fresh 128-bit nonce as 32 lowercase hex characters."""
    return secrets.token_hex(NONCE_HEX_LENGTH // 2)


@runtime_checkable
class EnvelopeSequencer(Protocol):
    """Allocates the two replay-protection members §7.6 requires the backend to mint."""

    async def next_seq(self, device_id: uuid.UUID, *, floor: int = 0) -> int: ...

    async def reserve_nonce(self, device_id: uuid.UUID, nonce: str, *, ttl_seconds: int) -> None: ...


class RedisEnvelopeSequencer:
    """§7.6's allocator over Redis: Lua CAS for `seq`, `SET NX EX` for the nonce.

    Takes the client rather than a URL, so it shares the one connection pool `create_app()`
    already builds and an outage is visible to `/health/ready` in one place.
    """

    def __init__(self, redis: Any, *, key_prefix: str = "forgeops") -> None:
        if redis is None:
            raise ValueError("RedisEnvelopeSequencer requires a Redis client; §7.6 makes Redis authoritative")
        if not key_prefix:
            raise ValueError("key_prefix must be non-empty; an unprefixed key would collide across products")
        self._redis = redis
        self._prefix = key_prefix

    def _seq_key(self, device_id: uuid.UUID) -> str:
        return f"{self._prefix}:envseq:{device_id}"

    def _nonce_key(self, device_id: uuid.UUID, nonce: str) -> str:
        return f"{self._prefix}:nonce:{device_id}:{nonce}"

    async def next_seq(self, device_id: uuid.UUID, *, floor: int = 0) -> int:
        """The next strictly-greater sequence number for this device.

        `floor` is the caller's known lower bound — in practice `agent_devices.last_seq`.
        Passing it means a Redis flush cannot restart the sequence at 1 and lock the device
        out until its own `last_seq` is exceeded again.
        """
        try:
            value = await self._redis.eval(_ADVANCE_SEQ, 1, self._seq_key(device_id), max(int(floor), 0))
        except Exception as exc:  # noqa: BLE001 - any client failure is the same outage
            raise SequenceUnavailableError(
                f"could not advance the envelope sequence for device {device_id}: {exc}"
            ) from exc
        seq = int(value)
        if seq < 1:
            raise SequenceUnavailableError(f"the sequence allocator returned {seq}, which is not a valid seq")
        return seq

    async def reserve_nonce(self, device_id: uuid.UUID, nonce: str, *, ttl_seconds: int) -> None:
        """`SETNX nonce:<device>:<nonce>` with the freshness-window TTL (§7.6).

        The TTL is the envelope's own max age: a nonce cannot be replayed usefully after the
        envelope carrying it has expired, so keeping the reservation longer would grow
        without bound for no additional protection.
        """
        if len(nonce) != NONCE_HEX_LENGTH:
            raise ValueError(f"a nonce is {NONCE_HEX_LENGTH} hex characters (§7.6), got {len(nonce)}")
        if ttl_seconds < 1:
            raise ValueError("the nonce reservation TTL must be at least one second")
        try:
            reserved = await self._redis.set(self._nonce_key(device_id, nonce), "1", nx=True, ex=ttl_seconds)
        except Exception as exc:  # noqa: BLE001 - any client failure is the same outage
            raise SequenceUnavailableError(
                f"could not reserve an envelope nonce for device {device_id}: {exc}"
            ) from exc
        if not reserved:
            raise NonceCollisionError(f"nonce already reserved for device {device_id} inside the freshness window")
