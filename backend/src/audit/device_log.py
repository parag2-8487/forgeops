# SPDX-License-Identifier: FSL-1.1-ALv2
"""The device-lifecycle audit record, and the only shape it may take (D-70, §11.9, Q-04, Q-17).

The problem this module exists to solve
--------------------------------------
`AuditWriter.append` is a `@mutation_primitive`, so `scripts/check-chokepoint.sh --python`
requires every caller to be lexically inside `src/governance/` or to hold a `MutationAuthority`.
Appendix A.1 nevertheless requires an audit record on **both** branches of pairing —
`pairing_code_issued` on issue, `pairing_failed` on a refused exchange, `device_paired` on a
successful one — and `src/auth/devices.py` is neither in `governance/` nor able to hold an
authority: minting one needs a change set, an approval id, a blast radius and an audit sequence,
and a pairing has none of those. It is not a mutation that traversed the chokepoint, and no
amount of plumbing can make it into one.

So `audit_events` carries two kinds of row, and D-70 makes the distinction a **type** rather
than a convention:

* a **transit** record, written only by the chokepoint through `AuditWriter.append`, whose
  `AuditDraft` can name any `action`, any `resource_kind` and a before/after pair;
* a **device-lifecycle** record, written through `DeviceAuditEvent`, which cannot express a
  transit at all.

Why the restriction is on the shape and not only on the location
---------------------------------------------------------------
Moving the pairing audit write into `governance/` would satisfy the checker and nothing else: a
governance-positioned helper taking an unrestricted `AuditDraft` is a **second entry point to
the whole audit vocabulary**, and Q-04 — "exactly one `audit_events` row per chokepoint transit"
— quantifies only over transits it drives itself. It cannot see a transit-shaped row written by
another writer, so it would keep passing while the property it names quietly stopped holding.
That is chapter 5's defect in a new location: a check that can no longer fail.

`DeviceAuditEvent` closes that by construction:

* `ACTIONS` is a closed tuple, asserted **disjoint** from `GovernanceAction`'s values by
  `tests/meta/test_device_audit_shape.py`, so no device record can carry a transit's `action`;
* `RESOURCE_KIND` is a constant, not a field, so no device record can name `change_set`;
* there is no `before_state` and no `after_state` field — the transit's evidence pair is
  unreachable from here;
* `details` is restricted to a closed **key** set, which is what keeps Q-17's "the code value
  appears in no audit row" structural rather than reviewed: there is no key a pairing code
  could be smuggled under.

The transit-shaped fields therefore remain reachable only through `AuditWriter.append`, whose
callers remain governance-only — `check-chokepoint.sh` still reports exactly one call site.

Why the recorder is a Protocol here and an implementation in `governance/`
-------------------------------------------------------------------------
The write itself still has to happen somewhere the checker authorises, and the only positional
authorisation §2.2.1 offers is `src/governance/`. So the *contract* lives here, next to the type
it carries, and `governance/device_audit.py` holds the one implementation that actually appends.
`DeviceService` depends on this Protocol and never imports `governance` — the domain that owns
identity does not learn about the domain that owns authority, and the import direction stays
`governance → auth` as it already is.

The cost, stated rather than implied
------------------------------------
"Every row in `audit_events` came through the chokepoint" is no longer true; what is true is
"every *transit-shaped* row did". That is a genuine narrowing of a sentence people quote, and it
is the price of Appendix A.1's requirement that a pairing be auditable. The compensation is that
the narrowing is mechanical: the two shapes are disjoint by a test, not by discipline.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DEVICE_AUDIT_ACTIONS",
    "DEVICE_AUDIT_DETAIL_KEYS",
    "DEVICE_AUDIT_OUTCOMES",
    "DEVICE_RESOURCE_KIND",
    "DeviceAuditEvent",
    "DeviceAuditRecorder",
    "InvalidDeviceAuditEventError",
]

#: The closed action vocabulary for a device-lifecycle record.
#:
#: Appendix A.1 names the first three; §3.1's revocation sequence names the fourth. `device_abandoned`
#: is §3.7's `abandoned` state becoming reachable on purpose: an agent that was issued a credential
#: and could not persist it gives the device back, so the row is not left `active` with its token
#: held by nobody. Closed and asserted disjoint from `GovernanceAction` in
#: `tests/meta/test_device_audit_shape.py`, because the disjointness is what stops this type being
#: usable to write a transit record.
DEVICE_AUDIT_ACTIONS: Final[tuple[str, ...]] = (
    "pairing_code_issued",
    "pairing_failed",
    "device_paired",
    "device_revoked",
    "device_abandoned",
)

#: The single resource kind these rows describe. A **constant, not a field** — a field would let
#: a caller write `resource_kind="change_set"` and produce a row indistinguishable from a
#: transit's.
DEVICE_RESOURCE_KIND: Final[str] = "agent_device"

#: The subset of `AuditWriter.OUTCOMES` a device record may carry.
#:
#: `blocked` and `pending` are deliberately absent: both are *chokepoint* verdicts (a blast-radius
#: block, an approval gate) and neither has a meaning in a pairing exchange. Leaving them out
#: keeps "an outcome that only a transit can produce" true.
DEVICE_AUDIT_OUTCOMES: Final[tuple[str, ...]] = ("allowed", "denied", "failed")

#: The only keys `details` may carry, and therefore the only shape the row's `after_state` can
#: take.
#:
#: A closed key set rather than a free mapping, because Q-17 requires that the pairing code value
#: appear in no audit row and a free mapping makes that a review obligation forever. Every key
#: here names something safe to record: identifiers, versions and public fingerprints. There is
#: deliberately no `code`, no `token`, no `secret` and no `detail` catch-all.
DEVICE_AUDIT_DETAIL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "device_id",
        "project_id",
        "agent_version",
        "platform",
        "csr_spki_sha256",
        "cert_serial",
        "cert_fingerprint",
        "failure_kind",
        "attempts",
        "revoked_at",
        # Who gave the device back. Only ever "agent": a self-report has no user behind it, and
        # this key exists so the row states that rather than leaving `actor_user_id` NULL and
        # letting a reader guess whether the actor was simply not recorded.
        "surrendered_by",
    }
)


class InvalidDeviceAuditEventError(ValueError):
    """A device-lifecycle event cannot be written.

    Raised at construction, before any database work, so a malformed event is a call-site error
    rather than a row that says nothing.
    """


@dataclass(frozen=True, slots=True)
class DeviceAuditEvent:
    """One device-lifecycle audit record, in the only shape this module permits.

    Frozen for the reason `AuditDraft` is: an event a later stage could edit after validation
    would be an event validated in one shape and written in another.
    """

    action: str
    reason: str
    outcome: str
    project_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    #: The user who is responsible, when there is one. On the exchange path that is the operator
    #: who *issued* the code (Appendix A.1: `Audit(r.issuer, "device_paired", …)`), not the
    #: unauthenticated caller — the exchange has no principal by construction (§4.4).
    actor_user_id: uuid.UUID | None = None
    details: Mapping[str, str] | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if self.action not in DEVICE_AUDIT_ACTIONS:
            raise InvalidDeviceAuditEventError(
                f"action must be one of {DEVICE_AUDIT_ACTIONS}, got {self.action!r}. A device record "
                "cannot carry a chokepoint transit's action (D-70)"
            )
        if self.outcome not in DEVICE_AUDIT_OUTCOMES:
            raise InvalidDeviceAuditEventError(f"outcome must be one of {DEVICE_AUDIT_OUTCOMES}, got {self.outcome!r}")
        if not self.reason.strip():
            raise InvalidDeviceAuditEventError(
                "reason is required and must be non-empty (NFR-14, §11.9): a device-lifecycle "
                "record with no stated reason is exactly the record that is useless later"
            )
        if self.details is None:
            return
        unknown = sorted(set(self.details) - DEVICE_AUDIT_DETAIL_KEYS)
        if unknown:
            raise InvalidDeviceAuditEventError(
                f"details carries unregistered key(s) {unknown}; permitted keys are "
                f"{sorted(DEVICE_AUDIT_DETAIL_KEYS)}. The key set is closed so a pairing code "
                "cannot be recorded under a plausible-looking name (Q-17)"
            )
        for key, value in self.details.items():
            if not isinstance(value, str):
                raise InvalidDeviceAuditEventError(f"details[{key!r}] must be a string, got {type(value).__name__}")

    @property
    def actor_kind(self) -> str:
        """`user` when an operator is named, otherwise `system`.

        Derived rather than passed, because `AuditDraft` refuses the two inconsistent
        combinations (`user` with no id, `system` with one) and a caller that supplied both
        fields independently would be one typo away from that refusal.
        """
        return "user" if self.actor_user_id is not None else "system"


@runtime_checkable
class DeviceAuditRecorder(Protocol):
    """Writes one device-lifecycle record inside the caller's transaction.

    Takes the caller's `AsyncSession` and must not commit, for the reason §11.9 gives for
    `AuditWriter`: the record and the row it describes have to commit or roll back together. A
    `device_paired` record that survived a rolled-back device update would name a device that is
    not active.

    `runtime_checkable` so `tests/unit/test_contract_conformance.py`'s signature-enforcing
    doubles can be asserted against it (§0.4.3).
    """

    async def record(self, session: AsyncSession, event: DeviceAuditEvent) -> None: ...
