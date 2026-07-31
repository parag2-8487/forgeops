# SPDX-License-Identifier: FSL-1.1-ALv2
"""JCS canonicalisation — one implementation, two subsystems (§7.6, §11.9, §16.2).

Why this module exists at all
----------------------------
Two subsystems must agree byte-for-byte on the canonical form of a JSON document:

* **envelope signing** (§7.6, Appendix A.2): the HMAC covers the canonical bytes of the
  envelope without its `signature`, and the Go agent recomputes those bytes
  independently. A one-byte disagreement is a rejected command that looks like a
  tampered one.
* **the audit hash chain** (§11.9, Appendix A.8): `hash = sha256(JCS(semantic fields) ||
  prev_hash)`, and verification recomputes the chain from stored rows. A disagreement
  makes a genuine chain look tampered with, which is worse than useless — it destroys
  trust in the one artifact that is supposed to be trustworthy.

If each subsystem canonicalised for itself, the two could diverge silently and only the
cross-runtime fixture corpus (Q-14) would notice, and only for envelopes. One wrapper
means a divergence is impossible rather than caught.

Why floats are refused
----------------------
§7.6 states that no envelope or audit payload contains a float. RFC 8785 does define a
serialisation for IEEE 754 doubles, so this is stricter than the standard, deliberately:

* the shortest round-trip representation of a double is exactly the kind of thing two
  language runtimes implement subtly differently, and the whole purpose here is
  cross-runtime byte equality;
* `0.1 + 0.2` is not `0.3`, so a float that arrives through arithmetic rather than as a
  literal can change the hash of a payload that is semantically identical;
* nothing in an envelope or an audit record needs one. Scores are integers, timestamps
  are RFC 3339 strings, and sizes are integers.

So a float is rejected at the boundary, with the offending path named, rather than
silently producing bytes that might not reproduce. `bool` is deliberately still allowed:
it is a JSON primitive with one spelling.
"""

from __future__ import annotations

from typing import Any

import rfc8785

__all__ = ["CanonicalisationError", "canonical_bytes", "canonical_hash", "canonical_json"]


class CanonicalisationError(ValueError):
    """The payload cannot be canonicalised deterministically."""


def _reject_floats(value: Any, path: str = "$") -> None:
    """Walk the payload and refuse any float, naming where it was found.

    A whole-document "contains a float" error would send the reader hunting. The path is
    the difference between a five-second fix and a bisect.
    """
    # `bool` is a subclass of `int`, so it must be excluded before the float check to
    # avoid a confusing message about a value that is not a float at all.
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        raise CanonicalisationError(
            f"{path}: float values are not permitted in a canonical payload "
            f"(got {value!r}). Design §7.6: no envelope or audit payload contains a "
            f"float, because the shortest round-trip form of a double is where two "
            f"runtimes disagree. Use an integer, or a string for a decimal quantity."
        )
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalisationError(f"{path}: object keys must be strings, got {type(key).__name__} {key!r}")
            _reject_floats(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")


def canonical_bytes(payload: Any) -> bytes:
    """The RFC 8785 canonical serialisation of `payload`.

    The single source of canonical bytes for envelope signing and the audit chain.
    """
    _reject_floats(payload)
    try:
        return rfc8785.dumps(payload)
    except CanonicalisationError:
        raise
    except Exception as exc:  # rfc8785 raises its own type hierarchy
        raise CanonicalisationError(f"payload is not canonicalisable: {exc}") from exc


def canonical_json(payload: Any) -> str:
    """`canonical_bytes` decoded as UTF-8, for logging and fixture files.

    Never use this to compute a signature or a hash: encoding it again is a second
    chance to disagree, and the whole point is that there is one set of bytes.
    """
    return canonical_bytes(payload).decode("utf-8")


def canonical_hash(payload: Any, *, prefix: bytes = b"", suffix: bytes = b"") -> bytes:
    """`sha256(prefix || canonical_bytes(payload) || suffix)`.

    Both positions exist because the two subsystems genuinely need different ones, and
    keeping both here is what stops a second concatenation site appearing.

    `prefix` carries a domain separator, so bytes signed for one purpose cannot be replayed
    as another (§7.6's `"forgeops-envelope-v1" || 0x00`).

    `suffix` carries the audit chain's previous hash, because Appendix A.8 fixes that order
    as `SHA256(payload ‖ prev_hash)` — the opposite way round from the envelope. Passing
    both here rather than concatenating at each call site means the order is fixed in one
    place: the audit chain and the envelope signer cannot end up disagreeing about which
    side the extra bytes go on, which is a difference no test would notice until the two
    were compared.
    """
    import hashlib

    return hashlib.sha256(prefix + canonical_bytes(payload) + suffix).digest()
