# SPDX-License-Identifier: FSL-1.1-ALv2
"""The one writer of a device-lifecycle audit record (D-70, §2.2.1, §11.9, Appendix A.1).

Why this file is in `governance/` and holds no authority
-------------------------------------------------------
§2.2.1 mechanism 3 authorises a call to a `@mutation_primitive` in exactly two ways: the caller
holds a `MutationAuthority`, or the caller is lexically inside `src/governance/`. A pairing
exchange can satisfy neither the first way nor the spirit of it — there is no change set, no
approval and no blast radius to mint an authority over — so the write happens here, in the one
package whose position authorises it.

That is the *whole* extent of what this module borrows from `governance/`. It mints nothing,
signs nothing, and reaches none of the eight names §2.2.1's banned-api table confines; it does
not even import them. `check-chokepoint.sh` reports its `append` call as `[governance]`, which is
the accurate answer rather than a widened one: the call really is inside the package the design
authorises by position.

What stops that position becoming a general-purpose exemption
------------------------------------------------------------
The parameter type. `record` takes a `DeviceAuditEvent`, whose action vocabulary is disjoint from
`GovernanceAction` and whose `resource_kind` is a constant, so this module **cannot** write a
transit-shaped row even though its position would permit the call. A version of this file taking
an `AuditDraft` would have been a second door onto the entire audit vocabulary; the type is what
keeps it a door onto four actions.

Q-04 therefore stays falsifiable. "Exactly one `audit_events` row per chokepoint transit" is
still a claim about the only writer that can produce a transit-shaped row, and that writer is
still `chokepoint._transit`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..audit.device_log import DEVICE_RESOURCE_KIND, DeviceAuditEvent
from ..audit.writer import AuditDraft, AuditWriter

__all__ = ["GovernanceDeviceAuditRecorder"]


class GovernanceDeviceAuditRecorder:
    """Projects a `DeviceAuditEvent` onto an `AuditDraft` and appends it.

    Holds the writer rather than a session, so the record joins whatever transaction the caller
    is already in. `DeviceService.exchange` updates the device row and records the pairing in one
    transaction; a recorder with its own session could not do that, and the two halves would be
    able to disagree about whether a device is paired.
    """

    def __init__(self, *, writer: AuditWriter) -> None:
        self._writer = writer

    async def record(self, session: AsyncSession, event: DeviceAuditEvent) -> None:
        """Append one device-lifecycle record. Does not commit.

        `after_state` carries `event.details` when there is one and `None` otherwise. Note the
        direction: `details` is projected *into* the draft's generic field, never read back out
        of it — the closed key set is enforced by `DeviceAuditEvent.__post_init__`, which has
        already run by the time this method sees the event.
        """
        details: dict[str, Any] | None = dict(event.details) if event.details else None
        draft = AuditDraft(
            action=event.action,
            resource_kind=DEVICE_RESOURCE_KIND,
            resource_id=str(event.device_id) if event.device_id is not None else None,
            reason=event.reason,
            outcome=event.outcome,
            actor_kind=event.actor_kind,
            actor_user_id=event.actor_user_id,
            tenant_id=event.tenant_id,
            project_id=event.project_id,
            after_state=details,
            trace_id=event.trace_id,
        )
        await self._writer.append(session, draft)
