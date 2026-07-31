# SPDX-License-Identifier: FSL-1.1-ALv2
"""Device custody of the per-device envelope key (design.md §11.2, §2.2.1, §2.2.2, D-62).

What this module owns
---------------------
`agent_devices.envelope_key_enc` holds the one secret in the schema that must be *recoverable*
rather than hashed: the backend has to sign command envelopes with it (§7.6). Everything about
how that column is sealed and unsealed lives here, and nothing else in the backend may reach
it — `src.auth.devices` is banned cross-domain and `src.auth.devices.envelope_key` is banned by
symbol (§2.2.1), so the only importer is `governance/chokepoint.py`.

D-62 — how the key-encryption key is derived, and what that costs
-----------------------------------------------------------------
The key-encryption key is derived with **HKDF-SHA256 from `ENVELOPE_PEPPER`** under the label
`forgeops-envelope-key-v1`, and the per-device envelope key is sealed with **AES-256-GCM** into
`envelope_key_enc`. No new configuration is introduced, and the derivation is domain-separated
from the pepper's other use (the `HMAC-SHA256` under which pairing codes and device tokens are
stored), so neither use can be substituted for the other.

Two options were rejected:

* **A dedicated KEK environment variable.** It adds a secret to §13.1 whose rotation is already
  coupled to the pepper in practice — rotating the pepper invalidates every stored device-token
  and pairing-code HMAC anyway — so it buys independent rotation of something that cannot
  rotate independently.
* **Leaving the column plaintext.** The column name asserts ciphertext, and a column that lies
  about its contents is worse than one that is honestly named.

Two costs, both real:

* **The derived KEK cannot rotate independently of the pepper.** Rotating it means re-sealing
  every `agent_devices` row. Phase 1 does **not** implement that re-seal; it is a named gap
  (`OQ-33`), and the honest operational consequence is that a pepper rotation in Phase 1
  invalidates every device's pairing and requires re-pairing rather than a re-seal.
* **If the pepper leaks, the envelope keys fall with it.** The blast radius is genuinely
  smaller than it first appears — a leaked pepper already forges device-token and pairing-code
  HMACs, which is enough to impersonate a device to the backend — so the marginal loss from the
  coupling is the ability to forge *commands to* a device rather than *as* a device. That is
  still a real widening, and stating it beats implying the coupling is free.

Why the AEAD is bound to the row
--------------------------------
The device id is the AES-GCM **additional authenticated data**. Without it, an attacker holding
`UPDATE` on `agent_devices` could transplant a ciphertext whose plaintext they know onto a
victim device's row and then sign envelopes that device would accept, without ever learning the
victim's key. With the device id bound in, a ciphertext lifted from one row fails to unseal
under another — proved by `test_a_ciphertext_does_not_unseal_under_another_device_id`.

Why the nonce is random rather than derived
-------------------------------------------
A fresh 96-bit nonce from the OS CSPRNG per seal, stored in front of the ciphertext. A nonce
derived from anything reusable — the device id, a counter, a hash of the key — is a nonce that
repeats the moment a device is re-keyed, and AES-GCM nonce reuse under one key leaks the
authentication subkey outright. `test_every_seal_uses_a_fresh_nonce` asserts uniqueness across
a generated set rather than trusting the generator.

Why `envelope_key` is a module-level function as well as a method
----------------------------------------------------------------
§2.2.1's confinement is a Ruff `banned-api` entry naming `src.auth.devices.envelope_key`. That
mechanism matches **imports**, so it can only ever bite a module-level name; an entry naming a
method would be indistinguishable from a real entry while banning nothing, which is exactly the
vacuity §0.4.5 exists to close. The module-level function is therefore the real, banned surface,
and `DeviceService.envelope_key` is a thin delegation kept because §11.2 writes it as a method.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretBytes
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .device_models import AgentDevice, DeviceStatus

__all__ = [
    "ENVELOPE_KEY_BYTES",
    "ENVELOPE_KEY_LABEL",
    "KEK_BYTES",
    "SEAL_NONCE_BYTES",
    "DeviceKeyError",
    "DeviceService",
    "EnvelopeKeyUnavailableError",
    "derive_key_encryption_key",
    "envelope_key",
    "generate_envelope_key",
    "seal_envelope_key",
    "unseal_envelope_key",
]

#: HKDF's `info` label. Versioned, so a future scheme change is a new label rather than a silent
#: reinterpretation of the same bytes — and domain-separated from the pepper's HMAC use, which is
#: the whole reason one secret can safely serve both.
ENVELOPE_KEY_LABEL: Final[bytes] = b"forgeops-envelope-key-v1"

#: 32 bytes. The envelope key is an HMAC-SHA256 key (§7.6) and SHA-256's block size is 64 bytes,
#: so a 32-byte key is used verbatim by HMAC rather than being hashed down — one fewer place for
#: two implementations to disagree about what "the key" is.
ENVELOPE_KEY_BYTES: Final[int] = 32

#: 32 bytes, because the seal is AES-**256**-GCM.
KEK_BYTES: Final[int] = 32

#: 96 bits, the nonce length AES-GCM is specified for. Any other length forces the
#: implementation through GHASH-based derivation, which is a second code path for no benefit.
SEAL_NONCE_BYTES: Final[int] = 12


class DeviceKeyError(ValueError):
    """A key could not be derived, sealed or unsealed."""


class EnvelopeKeyUnavailableError(DeviceKeyError):
    """The device has no usable envelope key.

    Deliberately raised rather than returning `None`. A caller that received `None` would be one
    `or b""` away from signing with an empty key, and an empty HMAC key produces a signature the
    agent would happily verify against the same empty key.
    """


def _hkdf_sha256(*, secret: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF with SHA-256, extract-then-expand, from the standard library.

    Written out rather than taken from `cryptography.hazmat.primitives.kdf.hkdf` for one
    reason: this function must produce the same bytes for the same inputs forever, and the
    fifteen lines below are auditable against RFC 5869 §2.2–§2.3 line by line. It is HMAC and a
    counter; there is no cryptographic choice hidden in it. (The AEAD below is a different
    matter and is *not* hand-rolled — AES-GCM is where a hand-rolled implementation would be
    both wrong and slow.)
    """
    if length < 1 or length > 255 * hashlib.sha256().digest_size:
        raise DeviceKeyError(f"HKDF output length {length} is outside RFC 5869's range")
    prk = hmac.new(salt, secret, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def derive_key_encryption_key(pepper: str | bytes) -> bytes:
    """The AES-256-GCM key-encryption key, derived from `ENVELOPE_PEPPER` (D-62).

    The salt is the label and the `info` is the label: HKDF-Extract needs a salt, and using a
    fixed, published, domain-separating string is stronger than the all-zero default and weaker
    than a per-device random salt only in a way that does not matter here — the input keying
    material is a high-entropy configured secret, not a password, so the salt's job is domain
    separation rather than stretching.

    An empty pepper is refused. A deployment with no pepper would otherwise derive a
    well-known KEK from the empty string and seal every device key under it, which is
    indistinguishable from encryption while providing none.
    """
    material = pepper.encode("utf-8") if isinstance(pepper, str) else bytes(pepper)
    if not material:
        raise DeviceKeyError(
            "ENVELOPE_PEPPER is empty; the envelope key-encryption key is derived from it "
            "(design §13.1, D-62) and an empty pepper would give every deployment the same KEK"
        )
    return _hkdf_sha256(
        secret=material,
        salt=ENVELOPE_KEY_LABEL,
        info=ENVELOPE_KEY_LABEL,
        length=KEK_BYTES,
    )


def generate_envelope_key() -> bytes:
    """A fresh per-device envelope key from the OS CSPRNG."""
    return secrets.token_bytes(ENVELOPE_KEY_BYTES)


def _aad(device_id: uuid.UUID) -> bytes:
    """The additional authenticated data: the device id, as its 16 canonical bytes.

    `bytes` rather than the hyphenated string, because there is exactly one byte encoding of a
    UUID and several string spellings of it. A seal written under one spelling and opened under
    another would fail authentication for a reason that looks like tampering.
    """
    if not isinstance(device_id, uuid.UUID):
        raise DeviceKeyError(f"device_id must be a UUID, got {type(device_id).__name__}")
    return device_id.bytes


def seal_envelope_key(plaintext: bytes, *, device_id: uuid.UUID, kek: bytes) -> bytes:
    """`nonce || AES-256-GCM(kek, nonce, plaintext, aad=device_id.bytes)`.

    The nonce travels in front of the ciphertext rather than in a second column: one column, one
    value, and no way to update one without the other. A caller cannot supply the nonce, which
    is the point — the single most damaging mistake available here is a reused nonce, and the
    API simply does not offer it.
    """
    if len(kek) != KEK_BYTES:
        raise DeviceKeyError(f"the key-encryption key must be {KEK_BYTES} bytes, got {len(kek)}")
    if not plaintext:
        raise DeviceKeyError("refusing to seal an empty envelope key")
    nonce = secrets.token_bytes(SEAL_NONCE_BYTES)
    sealed = AESGCM(kek).encrypt(nonce, plaintext, _aad(device_id))
    return nonce + sealed


def unseal_envelope_key(sealed: bytes, *, device_id: uuid.UUID, kek: bytes) -> bytes:
    """Recover the plaintext, or raise.

    Any failure — wrong KEK, wrong device id, truncated column, flipped bit — raises
    `EnvelopeKeyUnavailableError` with the **same** message. The AEAD already refuses to tell
    the caller which of those it was, and re-deriving a distinction here would hand an attacker
    with database access an oracle for whether a transplanted ciphertext was sealed under the
    current KEK.
    """
    if len(kek) != KEK_BYTES:
        raise DeviceKeyError(f"the key-encryption key must be {KEK_BYTES} bytes, got {len(kek)}")
    material = bytes(sealed or b"")
    if len(material) <= SEAL_NONCE_BYTES:
        raise EnvelopeKeyUnavailableError("the sealed envelope key is missing or truncated")
    nonce, ciphertext = material[:SEAL_NONCE_BYTES], material[SEAL_NONCE_BYTES:]
    try:
        return AESGCM(kek).decrypt(nonce, ciphertext, _aad(device_id))
    except InvalidTag as exc:
        raise EnvelopeKeyUnavailableError(
            "the sealed envelope key did not authenticate under this device id and pepper; "
            "the row may have been transplanted from another device, or ENVELOPE_PEPPER changed"
        ) from exc


async def envelope_key(session: AsyncSession, *, device_id: uuid.UUID, pepper: str) -> SecretBytes:
    """Fetch and unseal one device's envelope key. **Governance-only caller** (§2.2.1).

    In §2.2.1's banned-api table by name, so a module that could forge a command by signing an
    envelope cannot even import this. §11.2: "a service that can fetch a signing key is a
    service that can forge a command."

    Returns `SecretBytes`, not `SecretStr` as §11.2's sketch writes it. The key is 32 random
    bytes; a `SecretStr` would need an encoding step, that step would have two plausible
    spellings (base64url and hex), and a wrapper whose contents must be decoded before use is a
    wrapper that gets unwrapped early. `SecretBytes` carries the same repr protection — the one
    property the annotation exists for — without the encoding.

    Reads the column with a narrow `SELECT` rather than loading the ORM row, so the key never
    becomes an attribute of a long-lived identity-mapped object that some later `repr` could
    print.
    """
    result = await session.execute(
        text("SELECT envelope_key_enc, status FROM agent_devices WHERE id = :id"),
        {"id": device_id},
    )
    row = result.first()
    if row is None:
        raise EnvelopeKeyUnavailableError(f"no device row for {device_id}")
    sealed, status = row[0], str(row[1])
    if status == DeviceStatus.REVOKED.value:
        # Checked here as well as at the chokepoint's admission stage. Defence in depth is the
        # wrong phrase for it: the two checks answer different questions. Admission asks "may
        # this transit proceed"; this asks "may this key be produced at all", and a revoked
        # device's key must never leave the database whatever the caller believes.
        raise EnvelopeKeyUnavailableError(f"device {device_id} is revoked; its envelope key is not available")
    if sealed is None:
        raise EnvelopeKeyUnavailableError(f"device {device_id} has no sealed envelope key")
    kek = derive_key_encryption_key(pepper)
    return SecretBytes(unseal_envelope_key(bytes(sealed), device_id=device_id, kek=kek))


@dataclass(frozen=True, slots=True)
class SealedEnvelopeKey:
    """A freshly generated key and the bytes to store for it.

    Returned as a pair so the caller cannot store the ciphertext without having had the
    plaintext, and cannot keep the plaintext by accident: the field is `SecretBytes`.
    """

    device_id: uuid.UUID
    key: SecretBytes
    sealed: bytes


class DeviceService:
    """Pairing codes, device tokens, certificates, revocation (§1.1, §3.1, §11.2).

    Phase 1 builds this service across three leaves. **This leaf (7.5) implements only the
    envelope-key custody the chokepoint cannot mint without**: provisioning a sealed key for a
    device, recovering it, and finding the one active device for a project. Pairing-code issue
    and exchange arrive with leaf 8.1, the internal CA and certificate rotation with 8.2, and
    Redis-authoritative per-message revocation with 8.4. Splitting it that way keeps this leaf
    to the custody decision (D-62) rather than smuggling in a pairing flow that has no route,
    no rate limit and no tests yet.

    Holds no session. Every method takes the caller's `AsyncSession`, for the reason §11.9 gives
    for `AuditWriter`: a service with its own session cannot join the caller's transaction, and
    provisioning a device key in a transaction that later rolls back must leave no key behind.
    """

    def __init__(self, *, pepper: str) -> None:
        if not pepper:
            raise DeviceKeyError(
                "DeviceService requires a non-empty ENVELOPE_PEPPER: it is the input keying "
                "material for the envelope key-encryption key (D-62)"
            )
        self._pepper = pepper

    async def envelope_key(self, session: AsyncSession, device_id: uuid.UUID) -> SecretBytes:
        """§11.2's method form. Delegates to the module-level, banned function."""
        return await envelope_key(session, device_id=device_id, pepper=self._pepper)

    async def provision_envelope_key(self, session: AsyncSession, device_id: uuid.UUID) -> SealedEnvelopeKey:
        """Generate, seal and store a fresh envelope key for an existing device row.

        Joins the caller's transaction and does not commit. Overwrites unconditionally: a device
        that is being given a key is a device whose previous key must stop working, and leaving
        the old ciphertext in place "just in case" would keep a revoked credential valid.
        """
        kek = derive_key_encryption_key(self._pepper)
        key = generate_envelope_key()
        sealed = seal_envelope_key(key, device_id=device_id, kek=kek)
        result = await session.execute(
            text("UPDATE agent_devices SET envelope_key_enc = :sealed WHERE id = :id"),
            {"sealed": sealed, "id": device_id},
        )
        if result.rowcount != 1:
            raise EnvelopeKeyUnavailableError(f"no device row for {device_id}; nothing to provision")
        return SealedEnvelopeKey(device_id=device_id, key=SecretBytes(key), sealed=sealed)

    async def active_device_for(self, session: AsyncSession, project_id: uuid.UUID) -> AgentDevice | None:
        """The one device that may receive commands for this project, or `None`.

        `ORDER BY last_seen DESC NULLS LAST` and `LIMIT 1`: §3.7 allows a project to accumulate
        rows in other states, and the tie-break has to be deterministic or two replicas would
        sign envelopes for different devices from the same request. Newest contact wins, which
        is the only ordering that means anything operationally.

        Returns the ORM object because the chokepoint needs several columns of it —
        `policy_bundle_digest`, `status`, `id` — and a tuple would put their order at a call
        site. The envelope key is deliberately **not** among them; it comes from
        `envelope_key()`, which is the banned surface.
        """
        result = await session.execute(
            text(
                "SELECT id FROM agent_devices WHERE project_id = :project AND status = :status "
                "ORDER BY last_seen DESC NULLS LAST, created_at DESC LIMIT 1"
            ),
            {"project": project_id, "status": DeviceStatus.ACTIVE.value},
        )
        row = result.first()
        if row is None:
            return None
        return await session.get(AgentDevice, row[0])
