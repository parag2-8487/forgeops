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
from datetime import datetime
from typing import Any, Final

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509 import load_pem_x509_csr
from pydantic import SecretBytes
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit.device_log import DeviceAuditEvent, DeviceAuditRecorder
from .ca import (
    CertificateBundle,
    CertificateIssuer,
    CertificateRejectedError,
    certificate_fingerprint,
)
from .device_models import AgentDevice, DeviceStatus
from .pairing_limits import PairingExchangeLimiter, PairingUnavailableError
from .principal import Principal

__all__ = [
    "CONSUME_SCRIPT",
    "DEVICE_TOKEN_BYTES",
    "ENVELOPE_KEY_BYTES",
    "ENVELOPE_KEY_LABEL",
    "ISSUE_SCRIPT",
    "KEK_BYTES",
    "PAIRING_KEY_PREFIX",
    "REVOCATION_CHANNEL",
    "REVOCATION_SET_KEY",
    "SEAL_NONCE_BYTES",
    "AgentMeta",
    "AuthenticatedDevice",
    "CertificateRejectedError",
    "CertificateRotationRefusedError",
    "CsrRejectedError",
    "DeviceAuthenticationError",
    "DeviceCredentials",
    "DeviceKeyError",
    "DeviceNotFoundError",
    "DeviceService",
    "EnvelopeKeyUnavailableError",
    "PairingCode",
    "PairingCodeInvalidError",
    "PairingRateLimitedError",
    "PairingUnavailableError",
    "RevocationUnavailableError",
    "csr_spki_fingerprint",
    "derive_key_encryption_key",
    "envelope_key",
    "generate_envelope_key",
    "seal_envelope_key",
    "unseal_envelope_key",
]

#: The Redis SET that decides whether a device may send its NEXT frame (§3.1, §11.10, Q-16).
#:
#: Redis-authoritative, exactly like the `seq` high-water mark, and for the same reason: the hub
#: runs on any replica, the revoking request arrives at another, and a per-process cache would let
#: the frame after a revocation through on every replica that had not been told. `agent_devices.
#: status` is the durable record; this set is the enforcement point.
REVOCATION_SET_KEY: Final[str] = "forgeops:devtok:revoked"

#: pub/sub channel carrying `device_id` after a revocation, for prompt socket closure.
#:
#: An optimisation and never the guarantee. §3.1 is explicit: the per-message `SISMEMBER` is what
#: makes revocation take effect on the next message, and a replica that missed this event still
#: refuses the next frame.
REVOCATION_CHANNEL: Final[str] = "forgeops:revocations"


class RevocationUnavailableError(Exception):
    """The revocation set could not be read.

    Raised rather than answered, so the caller must decide — and the hub's decision is to close
    the socket. Returning `False` here would be the single most damaging default in the system: a
    Redis outage would silently turn every revoked device back on, which is the inverse of Q-16.
    """


class DeviceAuthenticationError(Exception):
    """The presented certificate and token do not authenticate an active device.

    One exception for every branch — unknown fingerprint, wrong token, non-active device, revoked
    device — for the reason `PairingCodeInvalidError` gives: the caller is unauthenticated, so
    telling it *which* check failed is telling it something it has not earned. The branch reaches
    the log line, and the hub closes with one code.
    """


@dataclass(frozen=True, slots=True)
class AuthenticatedDevice:
    """What the WebSocket handshake established about the peer (§11.10).

    Frozen, and carrying no key material: the hub logs this, and the hub is explicitly not
    allowed to hold the envelope signing key (§2.2.2). `last_seq` is the mirror column, passed on
    so the handshake can answer `seq_base` without a second query.
    """

    device_id: uuid.UUID
    project_id: uuid.UUID
    tenant_id: uuid.UUID | None
    policy_bundle_digest: str
    last_seq: int
    agent_version: str
    platform: str


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

#: 32 bytes, per Appendix A.1's `token ← Random(32)`. Only its HMAC is ever stored.
DEVICE_TOKEN_BYTES: Final[int] = 32

#: The Redis key prefix §3.1 names verbatim: `pair:<hmac>`.
PAIRING_KEY_PREFIX: Final[str] = "pair:"

#: Issue: write the payload hash **and** its TTL in one atomic step.
#:
#: Two commands would leave a window in which a pairing code exists with no expiry, and a
#: pairing code that never expires is the one failure mode §14.6's arithmetic cannot survive —
#: every bound in it is a bound per five-minute window.
ISSUE_SCRIPT: Final[str] = """
local key = KEYS[1]
local ttl = tonumber(ARGV[1])
redis.call('HSET', key,
    'project', ARGV[2],
    'tenant', ARGV[3],
    'issuer', ARGV[4],
    'device', ARGV[5],
    'attempts', '0')
redis.call('EXPIRE', key, ttl)
return 1
"""

#: Consume: fetch, increment attempts, burn on exceed, delete on success — one script, one
#: round trip, one serialisation point.
#:
#: **Atomicity is what makes single-use true.** Redis executes one `EVAL` to completion before
#: the next command on that key, so of N concurrent attempts on one code exactly one can observe
#: the key and delete it; the rest see `missing`. A read-then-delete pair in the application
#: would let two callers both read before either deleted, and both would be issued credentials
#: for one code. Q-17 generates concurrent attempts and requires at most one success, and
#: `mutations.toml`'s negative control for Q-17 splits this script in two.
#:
#: **Why the burn branch is reachable.** `attempts` counts every presentation of this digest
#: inside the code's window. It exceeds the cap only when the same digest is presented more than
#: `MAX_ATTEMPTS` times, which is exactly §3.7's `issued --> burned : 5 failed attempts`: after
#: the cap the key is deleted, so a code under attack stops being usable even by its owner.
#: Request-shaped validation (the CSR) deliberately happens **before** this script runs, so a
#: broken agent retrying with a malformed CSR cannot burn a code it legitimately holds.
#:
#: Every failure returns a bare status and no payload. A caller cannot learn whether a code
#: existed from what comes back, which is the response half of Q-17's indistinguishability.
CONSUME_SCRIPT: Final[str] = """
local key = KEYS[1]
local max_attempts = tonumber(ARGV[1])
if redis.call('EXISTS', key) == 0 then
    return {'missing'}
end
local attempts = tonumber(redis.call('HINCRBY', key, 'attempts', 1))
if attempts > max_attempts then
    redis.call('DEL', key)
    return {'burned'}
end
local fields = redis.call('HMGET', key, 'project', 'tenant', 'issuer', 'device')
redis.call('DEL', key)
return {'ok', fields[1], fields[2], fields[3], fields[4], tostring(attempts)}
"""


class DeviceKeyError(ValueError):
    """A key could not be derived, sealed or unsealed."""


class PairingCodeInvalidError(Exception):
    """Unknown, expired, burned or already-consumed code — one error for all four.

    One exception type on purpose, carrying no discriminator. §14.6 and Q-17 both require that
    the four cases be **indistinguishable in the response**, and the cheapest way to leak the
    difference is to raise four exception types and let a route render them separately. The
    reason the internal code took a particular branch reaches the audit row's `failure_kind`
    and stops there.
    """


class PairingRateLimitedError(Exception):
    """A per-IP or global exchange bucket is exhausted (§14.6)."""

    def __init__(self, *, retry_after_seconds: int) -> None:
        super().__init__("pairing exchange rate limit exhausted")
        self.retry_after_seconds = retry_after_seconds


class CsrRejectedError(Exception):
    """The submitted CSR is not a usable P-256 certificate request.

    Checked **before** the pairing code is consumed, so a malformed request cannot spend a
    code's single use or advance its attempt counter (§3.7's `burned` transition).
    """


class DeviceNotFoundError(Exception):
    """No device row with that id, so there is nothing to revoke."""


class CertificateRotationRefusedError(Exception):
    """The device exists but is not in a state that may receive a new certificate.

    Distinct from `DeviceNotFoundError`, because the two need different answers: "no such device"
    is a 404 an admin can act on, and "this device is revoked" is a refusal the *agent* must read
    as "stop and wipe" rather than as "retry later".
    """


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


@dataclass(frozen=True, slots=True)
class PairingCode:
    """What `issue_pairing_code` returns, and the only time the code exists in the clear.

    The code is a plain `str` rather than a `SecretStr`, and that is deliberate: it is displayed
    to a human in a browser and read aloud, so wrapping it would add an `.get_secret_value()`
    call at the one call site that must not have one — the response body — while doing nothing
    about the actual exposure, which is the screen. What protects it is the five-minute window,
    not the type.
    """

    code: str
    device_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AgentMeta:
    """What the agent tells the backend about itself during the exchange (§3.1's request).

    `fingerprint` is the SHA-256 of the CSR's SubjectPublicKeyInfo DER, lowercase hex. §3.1 lists
    the field without defining it; this is the definition, and `exchange` **checks** it against
    the CSR rather than storing it. A field the server accepts and ignores is worse than no field
    at all — it reads like a bound and is not one.
    """

    agent_version: str
    platform: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class DeviceCredentials:
    """What a successful exchange issues (§3.1's `201` body).

    `policy_bundle` and `policy_bundle_digest` are still absent: they come from
    `PolicyBundleService.publish`, which leaf 9.3 builds. Absent rather than empty, because an
    agent handed a zero-byte bundle would evaluate policy against nothing and D-30 makes
    `ErrNoBundle` a **deny** — so the honest intermediate state is "no field" rather than "a field
    that means deny and looks like a bundle".
    """

    device_id: uuid.UUID
    project_id: uuid.UUID
    device_token: SecretBytes
    envelope_key: SecretBytes
    csr_spki_sha256: str
    client_cert_pem: bytes
    ca_bundle_pem: bytes
    cert_serial: str
    cert_fingerprint: str
    cert_not_after: datetime
    renew_after: datetime


def csr_spki_fingerprint(csr_pem: bytes) -> str:
    """Validate a P-256 CSR and return the SHA-256 of its SubjectPublicKeyInfo, lowercase hex.

    Three checks, and each excludes a different failure:

    * **it parses as a PEM CSR** — otherwise there is nothing to sign in leaf 8.2;
    * **its self-signature verifies** — proof that the requester holds the private key for the
      public key it submitted. Without this check an attacker who intercepted a CSR could pair a
      device whose key it does not have, and every later mTLS handshake would be made by someone
      else;
    * **the key is EC P-256** — §3.1 fixes the curve. Accepting anything else would let a caller
      choose a 512-bit RSA key and the certificate the CA issues in 8.2 would be worthless.

    Raises `CsrRejectedError` for all three, with a message that names the check but never echoes
    the submitted bytes.
    """
    try:
        csr = load_pem_x509_csr(csr_pem)
    except Exception as exc:  # noqa: BLE001 - `cryptography` raises several unrelated types here
        raise CsrRejectedError("the certificate request is not a readable PEM CSR") from exc
    try:
        valid = csr.is_signature_valid
    except InvalidSignature as exc:  # pragma: no cover - defensive; the property is a bool
        raise CsrRejectedError("the certificate request's self-signature does not verify") from exc
    if not valid:
        raise CsrRejectedError(
            "the certificate request's self-signature does not verify; the requester has not "
            "proved possession of the private key"
        )
    public_key = csr.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(public_key.curve, ec.SECP256R1):
        raise CsrRejectedError("the certificate request must carry an EC P-256 public key (§3.1)")
    spki = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(spki).hexdigest()


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

    def __init__(
        self,
        *,
        pepper: str,
        recorder: DeviceAuditRecorder | None = None,
        redis: Any | None = None,
        limiter: PairingExchangeLimiter | None = None,
        ca: CertificateIssuer | None = None,
        code_ttl_seconds: int = 300,
        max_attempts: int = 5,
        alphabet: str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ",
        code_length: int = 6,
    ) -> None:
        if not pepper:
            raise DeviceKeyError(
                "DeviceService requires a non-empty ENVELOPE_PEPPER: it is the input keying "
                "material for the envelope key-encryption key (D-62)"
            )
        if code_ttl_seconds < 1:
            raise DeviceKeyError("the pairing-code TTL must be positive (Appendix A.1: `TTL > 0`)")
        if max_attempts < 1:
            raise DeviceKeyError("MAX_ATTEMPTS must be at least 1 (Appendix A.1's precondition)")
        if len(set(alphabet)) != len(alphabet) or len(alphabet) < 16:
            raise DeviceKeyError(
                "the pairing alphabet must have no duplicates and at least 16 symbols; "
                "core.config validates the configured value against Crockford base32"
            )
        self._pepper = pepper
        self._recorder = recorder
        self._redis = redis
        self._limiter = limiter
        self._ca = ca
        self._ttl = code_ttl_seconds
        self._max_attempts = max_attempts
        self._alphabet = alphabet
        self._code_length = code_length
        # All four pairing collaborators, or none of them. The custody half of this service
        # (leaf 7.5) genuinely needs no Redis, no limiter, no CA and no audit recorder, so they
        # cannot be unconditionally required — `make_fixture` and the chokepoint's own use
        # construct the custody-only form. What must not exist is the HALF-wired form: a service
        # with Redis but no recorder would consume a code and record nothing, and one with no CA
        # would consume a code and issue no certificate. So a partial combination is refused at
        # construction rather than discovered at the first exchange.
        #
        # `ca` is satisfied by `UnavailableCertificateAuthority`, which is present-but-refusing
        # rather than absent — the distinction that lets a deployment without
        # `INTERNAL_CA_CERT_PEM` start, report 503 on the exchange, and still serve everything
        # else (§11.1's `Unavailable*` pattern).
        collaborators = (
            ("recorder", recorder),
            ("redis", redis),
            ("limiter", limiter),
            ("ca", ca),
        )
        supplied = [name for name, value in collaborators if value is not None]
        if supplied and len(supplied) != len(collaborators):
            missing = sorted({name for name, _ in collaborators} - set(supplied))
            raise DeviceKeyError(
                f"DeviceService was given {sorted(supplied)} but not {missing}: the pairing flow "
                "needs all four (Appendix A.1 requires an audit record on both branches, §14.6 "
                "requires both rate limits, the consume script is a Redis EVAL, and §3.1's "
                "response carries a certificate). Pass all four for the pairing form, or none "
                "for the envelope-key custody form"
            )

    # ── envelope-key custody (leaf 7.5) ───────────────────────────────────────────────────

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

    # ── pairing: issue, exchange, revoke (leaf 8.1, Appendix A.1, §3.1, §14.6) ─────────────

    def _pairing_digest(self, code: str) -> bytes:
        """`HMAC-SHA256(pepper, code)` — the only representation of a code that is ever stored.

        Keyed rather than a bare hash: a 6-character code from a 32-symbol alphabet is a space of
        1.07 × 10⁹, which a plain SHA-256 rainbow table covers in seconds on a laptop. The pepper
        is what makes the stored digest useless to someone holding a database dump, and it is the
        same pepper D-62 derives the envelope KEK from, domain-separated by construction: this is
        HMAC over the code, that is HKDF under a label.
        """
        return hmac.new(self._pepper.encode("utf-8"), code.encode("utf-8"), hashlib.sha256).digest()

    def _generate_code(self) -> str:
        """A code from the OS CSPRNG, uniformly over the configured alphabet.

        `secrets.choice` rather than `random.choice`: the latter is a Mersenne Twister whose
        internal state is recoverable from a few hundred outputs, which for a pairing code means
        an attacker who has seen a handful of codes can predict the next one. Appendix A.1 says
        "from a CSPRNG, never a PRNG" and this is the line that obeys it.

        `secrets.choice` is also free of the modulo bias `token_bytes[i] % 32` would introduce for
        an alphabet whose length does not divide 256 — it happens that 32 does, but a configured
        alphabet of a different length would silently skew, and a skewed alphabet shrinks the
        search space §14.6's arithmetic assumes.
        """
        return "".join(secrets.choice(self._alphabet) for _ in range(self._code_length))

    def _redis_or_raise(self) -> Any:
        if self._redis is None or self._limiter is None or self._recorder is None:
            raise DeviceKeyError(
                "this DeviceService was constructed for envelope-key custody only; the pairing "
                "flow needs `recorder`, `redis` and `limiter` (see __init__)"
            )
        return self._redis

    async def issue_pairing_code(
        self, session: AsyncSession, *, project_id: uuid.UUID, actor: Principal
    ) -> PairingCode:
        """Appendix A.1's `IssuePairingCode`. Joins the caller's transaction and does not commit.

        Ordering matters twice here.

        **Live codes are revoked first.** A.1's `RevokeLiveCodesFor(project)` is what makes "one
        live code per project" true, and it is what §14.6's arithmetic counts on: the worst case
        it computes is "10 live codes across the deployment", which is a statement about projects,
        not about how many times an operator pressed the button.

        **Redis is written last.** The row and the audit record are in the caller's transaction;
        the Redis key is not, and cannot be. Writing Redis last means a transaction that rolls
        back afterwards leaves a live Redis key pointing at a device row that does not exist — and
        the exchange handles that as `pairing-code-invalid`, because its `UPDATE` matches no row.
        The other order would leave a committed `pending` device with no consumable code, which
        looks to an operator like a code that never worked.
        """
        redis = self._redis_or_raise()
        assert self._recorder is not None  # noqa: S101 - narrowed by _redis_or_raise
        stale = await session.execute(
            text(
                "SELECT id, pairing_token_hmac FROM agent_devices "
                "WHERE project_id = :project AND status = :pending AND pairing_token_hmac IS NOT NULL"
            ),
            {"project": project_id, "pending": DeviceStatus.PENDING.value},
        )
        stale_rows = list(stale.all())
        if stale_rows:
            await session.execute(
                text(
                    "UPDATE agent_devices SET status = :abandoned, pairing_token_hmac = NULL, "
                    "pairing_expires_at = NULL WHERE id = ANY(:ids)"
                ),
                {"abandoned": DeviceStatus.ABANDONED.value, "ids": [row[0] for row in stale_rows]},
            )

        code = self._generate_code()
        digest = self._pairing_digest(code)
        device_id = uuid.uuid4()
        inserted = await session.execute(
            text(
                "INSERT INTO agent_devices (id, project_id, tenant_id, status, pairing_token_hmac, "
                "pairing_expires_at, agent_version, platform, last_seq) "
                "VALUES (:id, :project, :tenant, :status, :digest, "
                "now() + make_interval(secs => :ttl), '', '', 0) "
                "RETURNING pairing_expires_at"
            ),
            {
                "id": device_id,
                "project": project_id,
                "tenant": actor.tenant_id,
                "status": DeviceStatus.PENDING.value,
                "digest": digest,
                "ttl": self._ttl,
            },
        )
        expires_at: datetime = inserted.one()[0]

        await self._recorder.record(
            session,
            DeviceAuditEvent(
                action="pairing_code_issued",
                reason="operator initiated pairing",
                outcome="allowed",
                project_id=project_id,
                device_id=device_id,
                tenant_id=actor.tenant_id,
                actor_user_id=actor.user_id,
                details={"device_id": str(device_id), "project_id": str(project_id)},
            ),
        )

        # The stale keys go first: a code being replaced must stop working before its successor
        # starts, or a window exists in which two codes are live for one project.
        for row in stale_rows:
            if row[1] is not None:
                await redis.delete(PAIRING_KEY_PREFIX + bytes(row[1]).hex())
        await redis.eval(
            ISSUE_SCRIPT,
            1,
            PAIRING_KEY_PREFIX + digest.hex(),
            str(self._ttl),
            str(project_id),
            str(actor.tenant_id or ""),
            str(actor.user_id),
            str(device_id),
        )
        return PairingCode(code=code, device_id=device_id, expires_at=expires_at)

    async def exchange(
        self, session: AsyncSession, *, code: str, csr_pem: bytes, meta: AgentMeta, client_ip: str
    ) -> DeviceCredentials:
        """Appendix A.1's `ExchangePairingCode`. The one unauthenticated entry point (§4.4).

        The order below is the algorithm's, and two positions in it are load-bearing.

        **Both rate limits precede everything.** §14.6 sizes the per-IP bucket for a single
        attacker and the global bucket for a distributed one, and neither bound means anything if
        an unbounded number of requests can reach the consume script first.

        **The CSR is validated before the code is consumed.** A.1 signs the CSR *after* the
        consume, which is correct for the CA call but would mean a malformed CSR spends a valid
        code's single use. Since validating a CSR is pure and cheap, it moves in front — and the
        consequence is worth naming: a caller who holds a real code but sends a broken CSR gets
        `csr-invalid` and keeps the code, while a caller who holds no code gets
        `pairing-code-invalid` whatever it sends. Neither response tells an attacker anything
        about a code it does not have.

        Every failure after that point raises `PairingCodeInvalidError` — unknown, expired, burned,
        consumed, and "the device row is no longer pairable" all produce one response (Q-17).
        """
        redis = self._redis_or_raise()
        assert self._recorder is not None and self._limiter is not None and self._ca is not None  # noqa: S101
        for verdict in (await self._limiter.check_per_ip(client_ip), await self._limiter.check_global()):
            if not verdict.allowed:
                raise PairingRateLimitedError(retry_after_seconds=verdict.retry_after_seconds)

        # CA availability is checked HERE, before the code is consumed, although the signing call
        # itself stays where Appendix A.1 puts it. Reading `ca_bundle` is the cheapest question
        # that distinguishes a configured CA from `UnavailableCertificateAuthority`, and asking it
        # now means a deployment with no `INTERNAL_CA_CERT_PEM` answers 503 without spending a
        # code that the operator would then have to reissue.
        ca_bundle = self._ca.ca_bundle

        spki = csr_spki_fingerprint(csr_pem)
        # `compare_digest` on two hex strings rather than `==`. The value is not a secret, but the
        # comparison is on a path an attacker can time, and there is no reason to hand out a
        # prefix-length oracle for free.
        if not hmac.compare_digest(spki, meta.fingerprint.strip().lower()):
            raise CsrRejectedError("the declared fingerprint does not match the CSR's SubjectPublicKeyInfo SHA-256")

        digest = self._pairing_digest(code)
        raw = await redis.eval(CONSUME_SCRIPT, 1, PAIRING_KEY_PREFIX + digest.hex(), str(self._max_attempts))
        outcome = list(raw or [])
        status = _as_text(outcome[0]) if outcome else "missing"
        if status != "ok":
            await self._record_failure(session, failure_kind=status)
            raise PairingCodeInvalidError()

        project_id = uuid.UUID(_as_text(outcome[1]))
        tenant_raw = _as_text(outcome[2])
        tenant_id = uuid.UUID(tenant_raw) if tenant_raw else None
        issuer_id = uuid.UUID(_as_text(outcome[3]))
        device_id = uuid.UUID(_as_text(outcome[4]))
        attempts = _as_text(outcome[5])
        # A.1's `ASSERT r.attempts ≤ MAX_ATTEMPTS`, kept as a real check rather than a comment:
        # a script that returned `ok` above the cap would be a burn branch that had stopped
        # working, and that is precisely the kind of silent weakening §0.4.5 exists to catch.
        if int(attempts) > self._max_attempts:
            raise DeviceKeyError(
                f"the consume script returned ok at attempt {attempts} with a cap of "
                f"{self._max_attempts}; the burn branch is not firing"
            )

        token = secrets.token_bytes(DEVICE_TOKEN_BYTES)
        updated = await session.execute(
            text(
                "UPDATE agent_devices SET status = :active, device_token_hmac = :token_hmac, "
                "pairing_token_hmac = NULL, pairing_expires_at = NULL, agent_version = :version, "
                "platform = :platform, last_seen = now() "
                "WHERE id = :id AND status = :pending AND pairing_token_hmac = :digest "
                "AND (pairing_expires_at IS NULL OR pairing_expires_at > now())"
            ),
            {
                "active": DeviceStatus.ACTIVE.value,
                "token_hmac": hmac.new(self._pepper.encode("utf-8"), token, hashlib.sha256).digest(),
                "version": meta.agent_version[:64],
                "platform": meta.platform[:64],
                "id": device_id,
                "pending": DeviceStatus.PENDING.value,
                "digest": digest,
            },
        )
        if updated.rowcount != 1:
            # The code was consumable but the device row is not pairable: the row was abandoned,
            # revoked, already active, or its DB-side expiry has passed. Indistinguishable in the
            # response from an unknown code, deliberately.
            await self._record_failure(session, failure_kind="device-not-pairable", project_id=project_id)
            raise PairingCodeInvalidError()

        # Through `provision_envelope_key`, never by sealing here. One sealing path means D-62's
        # AAD binding cannot be bypassed by a second one (tasks.md 8.1's own constraint).
        sealed = await self.provision_envelope_key(session, device_id)

        # The CA call is the one step Appendix A.1 keeps after the consume, and it has to stay
        # there: it issues a credential, so it cannot precede the decision to issue one. What
        # protects the code is that CA *availability* was checked before the consume — a
        # deployment with no CA refuses without spending anything.
        issued = self._ca.sign(csr_pem, device_id=device_id)
        await session.execute(
            text(
                "UPDATE agent_devices SET cert_serial = :serial, cert_fingerprint = :fingerprint, "
                "cert_not_after = :not_after WHERE id = :id"
            ),
            {
                "serial": issued.serial,
                "fingerprint": issued.fingerprint,
                "not_after": issued.not_after,
                "id": device_id,
            },
        )

        await self._recorder.record(
            session,
            DeviceAuditEvent(
                action="device_paired",
                reason="pairing code exchanged",
                outcome="allowed",
                project_id=project_id,
                device_id=device_id,
                tenant_id=tenant_id,
                actor_user_id=issuer_id,
                details={
                    "device_id": str(device_id),
                    "csr_spki_sha256": spki,
                    "cert_serial": issued.serial,
                    "cert_fingerprint": issued.fingerprint,
                    "agent_version": meta.agent_version[:64],
                    "platform": meta.platform[:64],
                    "attempts": attempts,
                },
            ),
        )
        return DeviceCredentials(
            device_id=device_id,
            project_id=project_id,
            device_token=SecretBytes(token),
            envelope_key=sealed.key,
            csr_spki_sha256=spki,
            client_cert_pem=issued.certificate_pem,
            ca_bundle_pem=ca_bundle,
            cert_serial=issued.serial,
            cert_fingerprint=issued.fingerprint,
            cert_not_after=issued.not_after,
            renew_after=issued.renew_after,
        )

    async def rotate_certificate(
        self, session: AsyncSession, *, device_id: uuid.UUID, csr_pem: bytes
    ) -> CertificateBundle:
        """§11.2's `rotate_certificate`: a replacement certificate for a live device.

        **`csr_pem` is an addition to §11.2's sketch, and it is not optional.** Rotation exists
        because the certificate is short-lived, and a short-lived certificate whose *key* never
        changes gives up most of what short-lived buys — a key stolen once stays useful for as long
        as the device does. Reissuing over the same key would also require the CA to keep every
        device's public key, which is a store this design does not have and does not want. So the
        agent generates a fresh P-256 pair and sends a new CSR, exactly as it did at pairing.

        Runs over the **live session** (§3.1): the hub calls this in response to the agent's
        rotation request before `renew_after`, so the new certificate arrives without a reconnect.
        There is deliberately no REST route — a device certificate handed out over a route
        authenticated by something other than the device's current certificate would be a second,
        weaker enrolment path.

        Refuses a device that is not `active`. A revoked or pending device asking for a fresh
        certificate is either a bug or an attacker holding a certificate that is about to expire,
        and in both cases the answer is no.
        """
        if self._ca is None:
            raise DeviceKeyError(
                "this DeviceService was constructed for envelope-key custody only; certificate "
                "rotation needs a `ca` (see __init__)"
            )
        result = await session.execute(text("SELECT status FROM agent_devices WHERE id = :id"), {"id": device_id})
        row = result.first()
        if row is None:
            raise DeviceNotFoundError(str(device_id))
        if str(row[0]) != DeviceStatus.ACTIVE.value:
            raise CertificateRotationRefusedError(
                f"device {device_id} is {row[0]}, not active; no certificate is issued"
            )
        issued = self._ca.sign(csr_pem, device_id=device_id)
        # Serial and fingerprint are overwritten, not appended to. The previous certificate stops
        # being the one the hub accepts the moment this commits, which is what makes rotation a
        # replacement rather than an accumulation of valid credentials.
        await session.execute(
            text(
                "UPDATE agent_devices SET cert_serial = :serial, cert_fingerprint = :fingerprint, "
                "cert_not_after = :not_after WHERE id = :id"
            ),
            {
                "serial": issued.serial,
                "fingerprint": issued.fingerprint,
                "not_after": issued.not_after,
                "id": device_id,
            },
        )
        return CertificateBundle(
            device_id=device_id,
            certificate_pem=issued.certificate_pem,
            ca_bundle_pem=self._ca.ca_bundle,
            serial=issued.serial,
            fingerprint=issued.fingerprint,
            not_after=issued.not_after,
            renew_after=issued.renew_after,
        )

    # ── the WebSocket handshake and per-message revocation (leaf 8.4, §11.10, Q-16) ────────

    async def authenticate_session(
        self, session: AsyncSession, *, certificate_pem: bytes, device_token: str
    ) -> AuthenticatedDevice:
        """Authenticate a WebSocket peer from its client certificate and bearer token (§3.1).

        Four checks, in this order, and the order is the point:

        1. **the certificate chains to the internal CA and is inside its validity window.** Done
           first because it is the only check that needs no database row, so an unrelated
           certificate costs one signature verification rather than a query;
        2. **its fingerprint names an `active` device row.** D-73 keeps the chain check as a
           precondition and `agent_devices.cert_fingerprint` as the authorisation input; this is
           where the second half happens;
        3. **the bearer token matches that row's HMAC, in constant time.** The certificate proves
           possession of a key; the token proves possession of the secret the exchange issued.
           Both are required, because a certificate is presented by the TLS stack and could be
           replayed by anything holding the file, while the token is what the agent keeps in its
           keychain;
        4. **the device is not in the Redis revocation set.** Checked here *and* per message: this
           call closes the door on a new session, and `is_revoked` closes it on the next frame.

        Two-secret verification is why this method is not simply a token lookup: a token lookup
        would authenticate a device whose certificate had been revoked or replaced by rotation,
        and the whole reason certificates are short-lived is that presenting an old one must stop
        working.
        """
        if self._ca is None:
            raise DeviceKeyError(
                "this DeviceService was constructed for envelope-key custody only; the hub "
                "handshake needs a `ca` to verify a client certificate (see __init__)"
            )
        try:
            certificate = self._ca.verify_chain(certificate_pem)
        except CertificateRejectedError as exc:
            raise DeviceAuthenticationError(f"client certificate rejected: {exc}") from exc

        fingerprint = certificate_fingerprint(certificate)
        result = await session.execute(
            text(
                "SELECT id, project_id, tenant_id, device_token_hmac, policy_bundle_digest, "
                "last_seq, agent_version, platform FROM agent_devices "
                "WHERE cert_fingerprint = :fingerprint AND status = :status"
            ),
            {"fingerprint": fingerprint, "status": DeviceStatus.ACTIVE.value},
        )
        row = result.mappings().first()

        # The token is decoded and compared even when no row was found, against a throwaway
        # digest. Returning early would make "unknown certificate" measurably faster than "wrong
        # token", and the hub's one close code would then leak which of the two it was through
        # timing alone.
        try:
            presented = bytes.fromhex(device_token.strip())
        except ValueError:
            presented = b""
        expected = bytes(row["device_token_hmac"] or b"") if row is not None else b""
        candidate = hmac.new(self._pepper.encode("utf-8"), presented, hashlib.sha256).digest()
        matches = hmac.compare_digest(candidate, expected) if expected else False
        if row is None or not matches:
            raise DeviceAuthenticationError("no active device matches the presented certificate and token")

        device_id = row["id"] if isinstance(row["id"], uuid.UUID) else uuid.UUID(str(row["id"]))
        if await self.is_revoked(device_id):
            raise DeviceAuthenticationError(f"device {device_id} is revoked")

        return AuthenticatedDevice(
            device_id=device_id,
            project_id=row["project_id"],
            tenant_id=row["tenant_id"],
            policy_bundle_digest=str(row["policy_bundle_digest"] or ""),
            last_seq=int(row["last_seq"] or 0),
            agent_version=str(row["agent_version"] or ""),
            platform=str(row["platform"] or ""),
        )

    async def is_revoked(self, device_id: uuid.UUID) -> bool:
        """`SISMEMBER devtok:revoked <device_id>` — the per-message guarantee (Q-16).

        **Fails closed.** A Redis error raises `RevocationUnavailableError` rather than returning
        `False`, and the hub turns that into a closed socket. The alternative — treating an
        unreachable revocation set as "nobody is revoked" — would mean a Redis outage silently
        re-enabled every revoked device, which is precisely the failure this check exists to
        prevent. An agent that cannot be checked does not get to act.

        No caching, deliberately. A one-second cache would be a one-second window in which a
        revoked device still executes a mutation, and §3.1 makes the guarantee *per message*.
        """
        if self._redis is None:
            raise RevocationUnavailableError(
                "no Redis client is configured, so revocation cannot be checked; refusing rather "
                "than assuming this device is live (§11.10, Q-16)"
            )
        try:
            member = await self._redis.sismember(REVOCATION_SET_KEY, str(device_id))
        except Exception as exc:  # noqa: BLE001 - any client failure is the same outage
            raise RevocationUnavailableError(
                f"could not read the revocation set for device {device_id}: {exc}"
            ) from exc
        return bool(member)

    async def _publish_revocation(self, device_id: uuid.UUID) -> None:
        """Add the device to the enforcement set, then announce it (§3.1's two Redis calls).

        Order matters and is not interchangeable. The `SADD` is the guarantee, the `PUBLISH` is
        the optimisation, so the set is written first: a subscriber that acted on the event before
        the set contained the id could close a socket the next handshake would happily reopen.

        A publish failure is swallowed, a `SADD` failure is not. Losing the announcement costs
        promptness — the socket closes on the device's next frame instead of immediately — while
        losing the set membership costs the guarantee.
        """
        if self._redis is None:
            raise RevocationUnavailableError(
                "no Redis client is configured, so a revocation cannot be enforced per message"
            )
        try:
            await self._redis.sadd(REVOCATION_SET_KEY, str(device_id))
        except Exception as exc:  # noqa: BLE001 - any client failure is the same outage
            raise RevocationUnavailableError(f"could not add device {device_id} to the revocation set: {exc}") from exc
        try:
            await self._redis.publish(REVOCATION_CHANNEL, str(device_id))
        except Exception:  # noqa: BLE001 - promptness is best-effort; the set is the guarantee
            pass

    async def _record_failure(
        self, session: AsyncSession, *, failure_kind: str, project_id: uuid.UUID | None = None
    ) -> None:
        """A.1's `Audit(system, "pairing_failed", …)`.

        `actor_kind` resolves to `system` because there is no principal on this route and none can
        be invented; attributing a failed exchange to the operator who issued *some* code would be
        a record that blames the wrong actor, which `AuditDraft.validate` already refuses.

        `failure_kind` names the branch — `missing`, `burned`, `device-not-pairable` — and that is
        internal-only: it reaches the audit row and never the response. The response is one
        `pairing-code-invalid` for every branch (Q-17). The code value is not a parameter of this
        method, so there is no argument through which it could reach a row.
        """
        assert self._recorder is not None  # noqa: S101
        await self._recorder.record(
            session,
            DeviceAuditEvent(
                action="pairing_failed",
                reason=f"pairing exchange refused: {failure_kind}",
                outcome="denied",
                project_id=project_id,
                details={"failure_kind": failure_kind},
            ),
        )

    async def revoke(self, session: AsyncSession, *, device_id: uuid.UUID, actor: Principal, reason: str) -> None:
        """Mark a device revoked in Postgres, in the Redis enforcement set, and on the record.

        Three effects, and the split between them is §3.1's: `agent_devices.status` is the durable
        record, `REVOCATION_SET_KEY` is what the hub consults per inbound frame (Q-16), and
        `REVOCATION_CHANNEL` is the announcement that closes an open socket promptly rather than
        on its next message. Leaf 8.1 owned only the first; the second and third land here with
        the hub that reads them.

        Idempotent by predicate rather than by check-then-act: the `UPDATE` excludes rows already
        revoked, so a second call writes no second audit row and reports success.

        **The Redis write happens before the caller's commit, and that ordering is deliberate.**
        The two stores can disagree in exactly one direction: a rolled-back transaction leaves a
        device denied in Redis while its row still says `active`. That device refuses every frame
        and every new handshake, which is the safe half of the disagreement — the inverse ordering
        would leave a revoked row whose sockets keep working until somebody noticed. The cost is
        real and is not hidden: recovering such a device means re-running the revocation (which is
        idempotent) or removing the id from the set deliberately.
        """
        assert_recorder = self._recorder
        if assert_recorder is None:
            raise DeviceKeyError("revocation writes an audit record; this DeviceService has no recorder")
        result = await session.execute(
            text(
                "UPDATE agent_devices SET status = :revoked, revoked_at = now(), "
                "pairing_token_hmac = NULL, pairing_expires_at = NULL "
                "WHERE id = :id AND status <> :revoked "
                "RETURNING project_id, tenant_id, revoked_at"
            ),
            {"revoked": DeviceStatus.REVOKED.value, "id": device_id},
        )
        row = result.first()
        if row is None:
            exists = await session.execute(
                text("SELECT project_id FROM agent_devices WHERE id = :id"), {"id": device_id}
            )
            if exists.first() is None:
                raise DeviceNotFoundError(str(device_id))
            # Already revoked in Postgres. The enforcement set is written again anyway: a device
            # revoked before this leaf existed has a `revoked` row and no set membership, and a
            # second revocation is the only occasion anything would notice.
            await self._publish_revocation(device_id)
            return  # already revoked; no second record
        project_id, tenant_id, revoked_at = row[0], row[1], row[2]
        await self._publish_revocation(device_id)
        await assert_recorder.record(
            session,
            DeviceAuditEvent(
                action="device_revoked",
                reason=reason,
                outcome="allowed",
                project_id=project_id,
                device_id=device_id,
                tenant_id=tenant_id,
                actor_user_id=actor.user_id,
                details={"device_id": str(device_id), "revoked_at": revoked_at.isoformat()},
            ),
        )


def _as_text(value: Any) -> str:
    """Redis replies arrive as `bytes` or `str` depending on `decode_responses`.

    Normalised in one place rather than at five call sites, because a client configured either way
    must produce the same behaviour — and the difference is invisible until a `uuid.UUID(b'...')`
    raises in production against a client the tests did not use.
    """
    if isinstance(value, bytes | bytearray):
        return value.decode("utf-8")
    return "" if value is None else str(value)
