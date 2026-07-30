# SPDX-License-Identifier: FSL-1.1-ALv2
"""The mint-only mutation capability (design.md §2.2.1 mechanism 1, §5.4, §11.6, Q-03).

Design intent is not enforcement
--------------------------------
Every mutation primitive takes a `MutationAuthority` as a **required** argument. The type
cannot be constructed outside this package, because `__post_init__` demands a module-private
sentinel that Ruff's banned-api rule forbids importing. Omitting the argument is therefore a
call-site error §0.4.2's conformance test catches in milliseconds, and forging one is a
`TypeError` at runtime — neither is a review obligation.

Why a sentinel and not a subclass, a private constructor or a token string
--------------------------------------------------------------------------
The alternatives were considered and each fails differently. A **private constructor** does
not exist in Python; `_mint()` beside a public `__init__` leaves the public one usable. A
**subclass check** (`if type(self) is not _Minted`) is defeated by subclassing, which needs no
import of anything private. A **secret string** is a credential: it would live in the source,
be visible in a traceback, and be indistinguishable from the real thing once copied. An
`object()` identity check can only be satisfied by code that can *name* the sentinel, and
naming it is exactly what the banned-api rule makes a lint failure everywhere but here.

What the fields are for
-----------------------
Each one records a stage the chokepoint completed, so an authority is also the evidence that
it was minted legitimately. `audit_seq` in particular means an authority cannot exist before
its audit record does: §11.9 writes the record inside the same transaction, and Q-04 asserts
exactly one row per transit.
"""

from __future__ import annotations

import uuid
from dataclasses import InitVar, dataclass
from typing import Final

from ..auth.principal import BlastRadius

#: Module-private, never exported and never re-exported. `__all__` below deliberately omits
#: it, and `src.governance.authority._MINT_SENTINEL` is in the Ruff banned-api table so an
#: import from anywhere — including elsewhere in this package — is a lint failure. The one
#: legitimate reader is `mint_authority` below.
_MINT_SENTINEL: Final[object] = object()

#: The message a forged construction raises with. Named so the tests assert the contract
#: rather than a string they also wrote.
FORGERY_MESSAGE: Final[str] = (
    "MutationAuthority may only be minted by governance.chokepoint; see design §2.2.1 and Q-03"
)


@dataclass(frozen=True, slots=True)
class MutationAuthority:
    """Proof that a mutation traversed the full chokepoint.

    Frozen and slotted. Frozen because an authority whose `blast_radius` a handler could
    widen after the fact would make every downstream check advisory; slotted because an
    attribute a caller could add is a place to smuggle state past the stages that produced it.
    """

    change_set_id: uuid.UUID
    approval_id: uuid.UUID
    policy_bundle_digest: str
    blast_radius: BlastRadius
    audit_seq: int
    envelope_digest: str
    _sentinel: InitVar[object]

    def __post_init__(self, _sentinel: object) -> None:
        if _sentinel is not _MINT_SENTINEL:
            raise TypeError(FORGERY_MESSAGE)


def mint_authority(
    *,
    change_set_id: uuid.UUID,
    approval_id: uuid.UUID,
    policy_bundle_digest: str,
    blast_radius: BlastRadius,
    audit_seq: int,
    envelope_digest: str,
) -> MutationAuthority:
    """Mint an authority. The only function that may, and it is not the chokepoint.

    Kept separate from `GovernanceChokepoint` on purpose: the chokepoint decides *whether* to
    mint after six stages, and this decides *how*. Splitting them means the sentinel has one
    reader, so "who can mint" is a one-line answer rather than a reading of the chokepoint's
    control flow.

    Every argument is keyword-only and required. A positional signature would let a caller
    that got the order wrong mint an authority claiming the wrong change set — and the fields
    are all opaque identifiers, so nothing downstream would notice.
    """
    if audit_seq < 1:
        # An authority whose audit sequence is zero or negative did not have a record written
        # for it, which is the one invariant §11.9 and Q-04 rest on. Refusing here means the
        # violation cannot be minted at all rather than being detected later by verification.
        raise ValueError("audit_seq must be a positive sequence number from a written audit record")
    if not policy_bundle_digest:
        raise ValueError("policy_bundle_digest is required: an authority must name the bundle that allowed it")
    return MutationAuthority(
        change_set_id=change_set_id,
        approval_id=approval_id,
        policy_bundle_digest=policy_bundle_digest,
        blast_radius=blast_radius,
        audit_seq=audit_seq,
        envelope_digest=envelope_digest,
        _sentinel=_MINT_SENTINEL,
    )


__all__ = ["FORGERY_MESSAGE", "MutationAuthority", "mint_authority"]
