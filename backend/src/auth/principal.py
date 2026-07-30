# SPDX-License-Identifier: FSL-1.1-ALv2
"""Who is acting (design.md §11.2, §14.1, §17.1 D-39).

A `Principal` is constructed **only** by a verifier, never from request data. That is
the property the governance chokepoint's admission stage leans on: it checks for a
principal first, so an unauthenticated mutation is impossible before any policy is
consulted.

`blast_radius` is the part that resolves OQ-20. Phase 0 read it from
`MCP_AGENT_BLAST_RADIUS`, an environment variable — which means the blast radius of a
request was a property of the *server's configuration* rather than of the caller. Two
callers with different authority got the same ceiling, and widening it for one widened
it for all. Phase 1 derives it from the attested identity (D-39): from the role for a
user, and from the device's project grant and attestation kind for a device. The env
var survives only as a dev default when there is no principal at all, and `Settings`
refuses it outright when `APP_ENV=production`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal

from .models import UserRole

#: The three blast-radius levels, widest last. Ordered so a comparison is possible
#: without a second mapping.
BlastRadius = Literal["read_only", "workspace", "infrastructure"]

BLAST_RADIUS_ORDER: Final[tuple[BlastRadius, ...]] = (
    "read_only",
    "workspace",
    "infrastructure",
)

#: Role → blast radius for a USER principal (D-39).
#:
#: A viewer can read; a developer can act within a workspace; only an admin reaches
#: infrastructure. This is data rather than a chain of `if`s so the mapping is one
#: reviewable line and Q-30 can enumerate it.
ROLE_BLAST_RADIUS: Final[dict[UserRole, BlastRadius]] = {
    UserRole.VIEWER: "read_only",
    UserRole.DEVELOPER: "workspace",
    UserRole.ADMIN: "infrastructure",
}

#: Attestation kind → the widest blast radius a DEVICE principal may reach.
#:
#: A device paired by a code and holding a short-lived certificate is `workspace`: it
#: may write inside the project it is paired to. `infrastructure` is deliberately
#: absent — no attestation kind Phase 1 ships can reach it, because Phase 1 has no
#: hardware-rooted device attestation (§14.3 states that gap plainly). A device that
#: needs infrastructure authority gets it from an approved change-set carrying a minted
#: authority, never from its own identity.
DEVICE_ATTESTATION_BLAST_RADIUS: Final[dict[str, BlastRadius]] = {
    "paired_device": "workspace",
    "spiffe": "workspace",
    "unattested": "read_only",
}


def widest(*radii: BlastRadius) -> BlastRadius:
    """The widest of the given radii."""
    return max(radii, key=BLAST_RADIUS_ORDER.index)


def narrowest(*radii: BlastRadius) -> BlastRadius:
    """The narrowest of the given radii.

    Used wherever two authorities meet: the effective radius is the *minimum*, so a
    widely-scoped device on a narrowly-scoped project cannot exceed the project.
    """
    return min(radii, key=BLAST_RADIUS_ORDER.index)


def blast_radius_for_role(role: UserRole) -> BlastRadius:
    """Derive a user principal's blast radius from its verified role."""
    return ROLE_BLAST_RADIUS[role]


def blast_radius_for_device(*, attestation: str, project_grant: BlastRadius = "workspace") -> BlastRadius:
    """Derive a device principal's blast radius from its attestation and grant.

    The result is the NARROWEST of the two, never the widest. An unknown attestation
    kind resolves to `read_only` rather than raising: a device presenting an
    attestation this build does not recognise is exactly the case that must not be
    granted write authority, and failing closed here is cheaper than a 500 that a
    caller might retry.
    """
    from_attestation = DEVICE_ATTESTATION_BLAST_RADIUS.get(attestation, "read_only")
    return narrowest(from_attestation, project_grant)


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting. Constructed only by a verifier — never from request data.

    Frozen and slotted: a handler that could mutate `role` or widen `blast_radius`
    after verification would make every downstream authorisation check advisory.
    """

    user_id: uuid.UUID
    subject: str  # IdP `sub`
    email: str
    role: UserRole
    tenant_id: uuid.UUID | None
    session_id: uuid.UUID | None
    kind: Literal["user", "device", "service"]
    device_id: uuid.UUID | None = None
    blast_radius: BlastRadius = "read_only"

    @classmethod
    def for_user(
        cls,
        *,
        user_id: uuid.UUID,
        subject: str,
        email: str,
        role: UserRole,
        tenant_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
    ) -> Principal:
        """Build a user principal with its blast radius DERIVED, never passed.

        There is deliberately no `blast_radius` parameter. A constructor that accepted
        one would let a caller widen its own authority, which is the failure D-39
        exists to close — and it would do so at a call site that looks entirely
        reasonable in review.
        """
        return cls(
            user_id=user_id,
            subject=subject,
            email=email,
            role=role,
            tenant_id=tenant_id,
            session_id=session_id,
            kind="user",
            device_id=None,
            blast_radius=blast_radius_for_role(role),
        )

    @classmethod
    def for_device(
        cls,
        *,
        device_id: uuid.UUID,
        subject: str,
        attestation: str,
        project_grant: BlastRadius = "workspace",
        tenant_id: uuid.UUID | None = None,
    ) -> Principal:
        """Build a device principal. Its radius is derived the same way, for the same
        reason.

        `user_id` is the device id: a device acts on its own behalf, and inventing a
        synthetic user would make the audit log claim a person did it.
        """
        return cls(
            user_id=device_id,
            subject=subject,
            email="",
            role=UserRole.DEVELOPER,
            tenant_id=tenant_id,
            session_id=None,
            kind="device",
            device_id=device_id,
            blast_radius=blast_radius_for_device(attestation=attestation, project_grant=project_grant),
        )
