# SPDX-License-Identifier: FSL-1.1-ALv2
"""Command-envelope canonicalisation and signing (design.md §7.6, §11.6, Appendix A.2).

This is the backend half of a contract with two implementations. The other half is
`agent/internal/envelope` (D-59). They must agree on **every byte**, because the signature
covers canonical bytes and nothing else: a one-byte disagreement is a rejected command that
looks exactly like a tampered one, and the operator has no way to tell those apart.

The four things this module owns
-------------------------------
1. **Canonical bytes.** RFC 8785 (JCS) over the envelope with `signature` absent, through
   `core.canonical` — the single canonicaliser the audit chain also uses (§11.9), so the two
   subsystems cannot drift.
2. **Domain separation.** `signing_input = prefix || 0x00 || canonical_bytes`. The prefix is
   why a signature over a command envelope can never be replayed as a signature over an
   `approval.response`, even though the same per-device key signs both.
3. **The MAC.** `base64url(HMAC-SHA256(envelope_key, signing_input))`, unpadded, one spelling.
4. **Custody of the key.** `_SIGNING_KEY` holds the per-device envelope key for the duration
   of one mint, and `sign_envelope` reads it from there. Both names are in §2.2.1's banned-api
   table, so nothing outside `governance/` can name either.

Why the key arrives through a ContextVar rather than as a parameter (D-60)
-------------------------------------------------------------------------
§2.2.1 fixes the name `_SIGNING_KEY` as a module-level constant of this module, and a
banned-api entry naming a symbol that does not exist bans nothing while looking exactly like
an entry that does — the vacuity trap §0.4.5 exists to close. So the symbol has to be real and
load-bearing.

A module-level dict of device keys would be a process-wide cache that outlives a revocation,
which Q-16 requires to take effect on the *next message*. A `ContextVar` is scoped to one
task: `signing_key_scope` sets it, restores the previous token on exit, and there is no state
between requests to go stale. The cost is that `sign_envelope`'s key is implicit at the call
site; it is paid down by refusing to sign at all when the scope is absent, with an error that
names the context manager.

What this module deliberately does not do
-----------------------------------------
It does not fetch a key, decrypt `agent_devices.envelope_key_enc`, allocate a `seq`, mint a
nonce, or send anything. Those belong to the device service (§11.2) and the chokepoint
(§11.6). This module is pure functions plus one scoped key holder, which is what makes it
testable against a committed fixture corpus without a database.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final

from ..core.canonical import CanonicalisationError, canonical_bytes

__all__ = [
    "APPROVAL_DOMAIN_PREFIX",
    "CANONICAL_MEMBERS",
    "ENVELOPE_DOMAIN_PREFIX",
    "ENVELOPE_VERSION",
    "MAX_SAFE_INTEGER",
    "CommandEnvelope",
    "EnvelopeError",
    "EnvelopeSchemaError",
    "PolicyContextPayload",
    "SigningKeyUnavailableError",
    "canonical_envelope_bytes",
    "decode_signature",
    "encode_signature",
    "envelope_digest",
    "sign_envelope",
    "signing_input",
    "signing_key_scope",
    "verify_envelope_signature",
]

#: The only accepted value of an envelope's `v` member. A version member that is never
#: checked cannot be used to change anything later, so it is validated rather than carried.
ENVELOPE_VERSION: Final[str] = "1"

#: §7.6's domain-separation strings. `forgeops-approval-v1` covers the one thing the agent
#: signs rather than verifies (an `approval.response`), under the same per-device key.
ENVELOPE_DOMAIN_PREFIX: Final[str] = "forgeops-envelope-v1"
APPROVAL_DOMAIN_PREFIX: Final[str] = "forgeops-approval-v1"

#: The signed member set, in the order §7.6 lists it. Held as data rather than derived from
#: the dataclass's field order, for the reason `agent/internal/envelope` states: derived from
#: fields, a rename for tidiness would change the meaning of every signature ever produced.
CANONICAL_MEMBERS: Final[tuple[str, ...]] = (
    "v",
    "command_id",
    "device_id",
    "operation",
    "args",
    "approval_id",
    "policy_context",
    "nonce",
    "seq",
    "not_after",
)

#: The largest integer RFC 8785 can serialise exactly, because the scheme defines numbers via
#: ES6 `Number` — an IEEE-754 double. `2**53` is already unrepresentable.
#:
#: This bound is enforced HERE as well as by `rfc8785`, and the Go side enforces it too, for a
#: reason found while building the fixture corpus: `rfc8785` raises `IntegerDomainError` above
#: it while a verbatim-decimal serialiser happily emits the digits. Two runtimes that disagree
#: about whether a document is canonicalisable at all are worse than two that disagree about
#: its bytes, because one side reports "malformed" and the other reports "signature invalid".
MAX_SAFE_INTEGER: Final[int] = 2**53 - 1


class EnvelopeError(ValueError):
    """An envelope cannot be canonicalised, signed or verified."""


class EnvelopeSchemaError(EnvelopeError):
    """The envelope's shape or a member's type is not what §7.6 fixes."""


class SigningKeyUnavailableError(EnvelopeError):
    """`sign_envelope` was called outside a `signing_key_scope`.

    Deliberately a hard error rather than a fallback to a default key. A default signing key
    is a key that signs commands nobody authorised.
    """


#: Module-private. The per-device envelope key, for the duration of one mint.
#:
#: In §2.2.1's banned-api table, so no module outside `governance/` can name it — which is the
#: whole of "the control plane is the sole holder of the per-device envelope signing key"
#: (§2.2.2), expressed as a lint failure instead of a convention.
_SIGNING_KEY: Final[ContextVar[bytes | None]] = ContextVar("forgeops_envelope_signing_key", default=None)


@contextmanager
def signing_key_scope(key: bytes) -> Iterator[None]:
    """Install `key` as the envelope signing key for the duration of the block.

    Resets to the *previous* token rather than to `None`, so a nested scope restores its
    parent instead of clearing it. Nesting is not expected, but "not expected" is how a
    surrounding mint ends up signing with no key at all.
    """
    if not isinstance(key, bytes | bytearray) or len(key) == 0:
        raise EnvelopeError("an envelope signing key must be non-empty bytes")
    token = _SIGNING_KEY.set(bytes(key))
    try:
        yield
    finally:
        _SIGNING_KEY.reset(token)


@dataclass(frozen=True, slots=True)
class PolicyContextPayload:
    """The bundle binding Q-07 compares against the agent's loaded bundle.

    `bundle_digest` is required. An envelope that names no bundle cannot be checked against
    one, and "no digest" must never be readable as "any digest".
    """

    bundle_digest: str
    decision: str

    def as_canonical(self) -> dict[str, str]:
        if not self.bundle_digest:
            raise EnvelopeSchemaError("policy_context.bundle_digest is required (§7.6, Q-07)")
        if not self.decision:
            raise EnvelopeSchemaError("policy_context.decision is required (§7.6)")
        return {"bundle_digest": self.bundle_digest, "decision": self.decision}


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    """§7.6's wire shape, exactly and in full.

    Frozen because an envelope whose `args` a later stage could edit would be signed over
    bytes that no longer describe what runs. `args` is a `Mapping`, and every value in it is
    walked before canonicalisation: §7.6 permits objects, arrays, strings, integers, booleans
    and null, and nothing else.
    """

    command_id: str
    device_id: str
    operation: str
    args: Mapping[str, Any]
    approval_id: str
    policy_context: PolicyContextPayload
    nonce: str
    seq: int
    not_after: int
    v: str = ENVELOPE_VERSION

    def as_canonical_mapping(self) -> dict[str, Any]:
        """The member map that is canonicalised — `signature` absent by construction.

        There is no code path that removes `signature` from a mapping, because there is no
        path that puts it in one. Removal is a step that can be forgotten; absence cannot.
        """
        if self.v != ENVELOPE_VERSION:
            raise EnvelopeSchemaError(f"envelope v must be {ENVELOPE_VERSION!r}, got {self.v!r}")
        for name, value in (("command_id", self.command_id), ("device_id", self.device_id)):
            _require_uuid(name, value)
        if not self.operation:
            raise EnvelopeSchemaError("operation is required (§7.7's catalogue is closed)")
        if not self.nonce:
            raise EnvelopeSchemaError("nonce is required (§7.6 uniqueness)")
        _require_safe_integer("seq", self.seq)
        _require_safe_integer("not_after", self.not_after)
        if self.seq < 1:
            raise EnvelopeSchemaError("seq must be a positive per-device sequence number (§7.6 ordering)")

        args = _normalise_args(self.args)
        body: dict[str, Any] = {
            "v": self.v,
            "command_id": self.command_id,
            "device_id": self.device_id,
            "operation": self.operation,
            "args": args,
            "approval_id": self.approval_id,
            "policy_context": self.policy_context.as_canonical(),
            "nonce": self.nonce,
            "seq": self.seq,
            "not_after": self.not_after,
        }
        # The member set is the contract. If this map and CANONICAL_MEMBERS ever disagree,
        # every signature this module produces changes meaning, so it is an error rather than
        # a silent difference — the same assertion the Go side makes, for the same reason.
        if tuple(sorted(body)) != tuple(sorted(CANONICAL_MEMBERS)):
            raise EnvelopeSchemaError(
                f"canonical body members {sorted(body)} do not match §7.6's list {sorted(CANONICAL_MEMBERS)}"
            )
        return body

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> CommandEnvelope:
        """Build an envelope from a parsed JSON object, rejecting unknown members.

        Strict on purpose (Appendix A.2 `ParseStrict`): an unknown member is either a typo in
        something load-bearing or a field one side signs and the other ignores.
        """
        unknown = sorted(set(payload) - set(CANONICAL_MEMBERS) - {"signature"})
        if unknown:
            raise EnvelopeSchemaError(f"unknown envelope members: {unknown}")
        missing = sorted(set(CANONICAL_MEMBERS) - set(payload))
        if missing:
            raise EnvelopeSchemaError(f"missing envelope members: {missing}")
        policy = payload["policy_context"]
        if not isinstance(policy, Mapping):
            raise EnvelopeSchemaError("policy_context must be an object")
        policy_unknown = sorted(set(policy) - {"bundle_digest", "decision"})
        if policy_unknown:
            raise EnvelopeSchemaError(f"unknown policy_context members: {policy_unknown}")
        args = payload["args"]
        if not isinstance(args, Mapping):
            raise EnvelopeSchemaError("args must be an object (§7.7's operations all take one)")
        for name in ("seq", "not_after"):
            if isinstance(payload[name], bool) or not isinstance(payload[name], int):
                raise EnvelopeSchemaError(f"{name} must be an integer, got {type(payload[name]).__name__}")
        return cls(
            v=str(payload["v"]),
            command_id=str(payload["command_id"]),
            device_id=str(payload["device_id"]),
            operation=str(payload["operation"]),
            args=args,
            approval_id=str(payload["approval_id"]),
            policy_context=PolicyContextPayload(
                bundle_digest=str(policy.get("bundle_digest", "")),
                decision=str(policy.get("decision", "")),
            ),
            nonce=str(payload["nonce"]),
            seq=int(payload["seq"]),
            not_after=int(payload["not_after"]),
        )


def _require_uuid(name: str, value: str) -> None:
    """`command_id` and `device_id` are UUIDs on the wire; a free-form string is not one.

    Checked because both values reach audit rows and log lines, and a value that is sometimes
    a UUID and sometimes a label makes every downstream join conditional.
    """
    if not value:
        raise EnvelopeSchemaError(f"{name} is required")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise EnvelopeSchemaError(f"{name} must be a UUID string, got {value!r}") from exc


def _require_safe_integer(path: str, value: object) -> None:
    """Refuse a non-integer, and refuse an integer RFC 8785 cannot serialise exactly.

    The second half is the cross-runtime half. `rfc8785` raises above `2**53 - 1`; a
    serialiser that writes the decimal text verbatim does not. Enforcing the bound in both
    runtimes means a document is either canonicalisable in both or refused by both.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnvelopeSchemaError(f"{path} must be an integer, got {type(value).__name__}")
    if abs(value) > MAX_SAFE_INTEGER:
        raise EnvelopeSchemaError(
            f"{path}={value} is outside RFC 8785's exact integer domain "
            f"(±{MAX_SAFE_INTEGER}); the scheme defines numbers as IEEE-754 doubles, so a "
            f"larger value cannot round-trip and the two runtimes would disagree"
        )


def _normalise_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Copy `args` into plain JSON types, refusing anything §7.6 does not permit.

    An absent or empty `args` becomes `{}` rather than `null`: §7.7's operations all take an
    object, and leaving the choice to a caller would give one logical envelope two canonical
    forms.
    """
    if args is None:
        return {}
    if not isinstance(args, Mapping):
        raise EnvelopeSchemaError(f"args must be an object, got {type(args).__name__}")
    return {str(key): _normalise_value(value, f"args.{key}") for key, value in args.items()}


def _normalise_value(value: Any, path: str) -> Any:
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int):
        _require_safe_integer(path, value)
        return value
    if isinstance(value, float):
        raise EnvelopeSchemaError(
            f"{path} is a float; §7.6 forbids a float anywhere in an envelope, because the "
            f"shortest round-trip form of a double is where two runtimes disagree"
        )
    if isinstance(value, Mapping):
        return {str(key): _normalise_value(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_normalise_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise EnvelopeSchemaError(f"{path} is a {type(value).__name__}, which is not a JSON value")


def canonical_envelope_bytes(envelope: CommandEnvelope) -> bytes:
    """The RFC 8785 canonical serialisation of `envelope`, with `signature` absent.

    Exported for the reason §7.6 gives: the cross-runtime fixture corpus. `Q-14` asserts these
    bytes equal `envelope.CanonicalBytes`'s output in Go for the same logical envelope.
    """
    try:
        return canonical_bytes(envelope.as_canonical_mapping())
    except CanonicalisationError as exc:
        raise EnvelopeSchemaError(str(exc)) from exc


def signing_input(prefix: str, envelope: CommandEnvelope) -> bytes:
    """`prefix || 0x00 || canonical_envelope_bytes(envelope)`.

    The concatenation lives in one place so the order is fixed once. Two call sites that
    concatenated differently would each verify their own signatures happily and reject the
    other's — a failure that presents as an intermittent signature error.
    """
    if prefix not in (ENVELOPE_DOMAIN_PREFIX, APPROVAL_DOMAIN_PREFIX):
        raise EnvelopeError(
            f"unknown domain-separation prefix {prefix!r}; §7.6 defines "
            f"{ENVELOPE_DOMAIN_PREFIX!r} and {APPROVAL_DOMAIN_PREFIX!r}"
        )
    return prefix.encode("utf-8") + b"\x00" + canonical_envelope_bytes(envelope)


def envelope_digest(envelope: CommandEnvelope) -> str:
    """Hex SHA-256 of the envelope's signing input under the command prefix.

    This is the value `MutationAuthority.envelope_digest` carries, so an audit row names the
    exact bytes that were signed rather than a re-serialisation of them.
    """
    return hashlib.sha256(signing_input(ENVELOPE_DOMAIN_PREFIX, envelope)).hexdigest()


def encode_signature(mac: bytes) -> str:
    """Render a raw MAC as §7.6's base64url form: unpadded, one spelling."""
    return urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def decode_signature(encoded: str) -> bytes:
    """Parse §7.6's base64url form, accepting padding on the way in but never emitting it.

    Rejects **non-canonical** base64, which is the sharp edge D-59 surfaced on the Go side and
    which Python shares: a 32-byte MAC is 43 base64url characters carrying 258 bits, so the
    final character has four bits that decode to nothing and are ignored. Four distinct
    43-character strings therefore decode to the same MAC. Without the round-trip check below,
    "every single-byte mutation of the signature is rejected" would simply be false.
    """
    if not encoded:
        raise EnvelopeSchemaError("signature is empty")
    trimmed = encoded.rstrip("=")
    padded = trimmed + "=" * (-len(trimmed) % 4)
    try:
        decoded = urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise EnvelopeSchemaError(f"signature is not base64url: {exc}") from exc
    if encode_signature(decoded) != trimmed:
        raise EnvelopeSchemaError(
            "signature is not canonical base64url; its trailing bits are non-zero, which "
            "would give one MAC several valid spellings"
        )
    return decoded


def sign_envelope(envelope: CommandEnvelope, *, prefix: str = ENVELOPE_DOMAIN_PREFIX) -> str:
    """`base64url(HMAC-SHA256(envelope_key, signing_input(prefix, envelope)))`.

    The key comes from `_SIGNING_KEY`, installed by `signing_key_scope`. Banned outside
    `governance/` by §2.2.1's table: a module that can call this can issue a command the agent
    will execute, which is the whole of the trust boundary in one function.
    """
    key = _SIGNING_KEY.get()
    if key is None:
        raise SigningKeyUnavailableError(
            "no envelope signing key is in scope; governance.chokepoint must wrap the mint in "
            "governance.envelope.signing_key_scope(key) (design §2.2.1, §2.2.2)"
        )
    mac = hmac.new(key, signing_input(prefix, envelope), hashlib.sha256).digest()
    return encode_signature(mac)


def verify_envelope_signature(
    envelope: CommandEnvelope,
    signature: str,
    key: bytes,
    *,
    prefix: str = ENVELOPE_DOMAIN_PREFIX,
) -> bool:
    """Constant-time check of `signature` against `envelope` under `key`.

    Takes the key as a parameter rather than reading `_SIGNING_KEY`, and is not banned,
    because verification is not a capability: being able to check a signature grants nothing.
    Signing does, which is why only that half is confined.

    Returns `False` for a malformed signature rather than raising, so a caller cannot
    distinguish "wrong key" from "wrong encoding" by exception type — the agent's Appendix A.2
    order makes the same choice.
    """
    try:
        provided = decode_signature(signature)
        expected = hmac.new(key, signing_input(prefix, envelope), hashlib.sha256).digest()
    except EnvelopeError:
        return False
    return hmac.compare_digest(expected, provided)
