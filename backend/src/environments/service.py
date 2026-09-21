# SPDX-License-Identifier: FSL-1.1-ALv2
"""Environment CRUD, variables, and the promotion rule. §2.1.

WHAT THIS MODULE IS ANSWERABLE FOR, and what it deliberately is not.

It owns the ROWS: environments, their ordering, their variables and whether a mutation targeting one
needs a human. It does not deploy. A promotion here produces a DECISION — "staging may be promoted to
production, and that will need approval" — and the mutation that acts on it goes through the governance
chokepoint like every other. Putting an apply in this module would give the platform a second path to
the user's machine, which §2.2.1 exists to prevent.

THE APPROVAL REQUIREMENT IS THE REASON THIS TABLE IS FIRST.

`GovernanceChokepoint._evaluate_policy` has accepted an `environment` argument since Phase 1 and no
caller ever supplied one — a parameter with the right name that nothing filled, which is why a clone's
policy evaluation fell through to "requires approval because `environment` is absent". `requirement_for`
below is what gives it a value derived from an operator's recorded intent rather than from a constant.

PRODUCTION CANNOT BE RELAXED. `create` and `update` refuse to set `requires_approval=False` on a
production environment. That is a deliberate asymmetry: the whole value of an approval gate is that the
person in a hurry cannot remove it, and an environment kind named `production` is the clearest statement
of intent the system has. An operator who genuinely wants an unattended target can make one of kind
`custom`, which says what it is.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import problem
from .models import (
    ENVIRONMENT_KINDS,
    PROTECTED_KINDS,
    derive_secret_key,
    seal_secret,
    unseal_secret,
)


@dataclass(frozen=True, slots=True)
class EnvironmentRecord:
    """One environment, as every read surface reports it."""

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    kind: str
    k8s_context: str | None
    requires_approval: bool
    position: int


@dataclass(frozen=True, slots=True)
class VariableRecord:
    """One variable. `value` is `None` for a secret, and that is not an error.

    WITHHELD, NOT BLANKED. A secret reports `is_secret=True` and `value=None`, so a reader can tell
    "this environment has a DATABASE_URL and I am not being shown it" from "this environment has no
    DATABASE_URL". Returning an empty string for both would make a missing variable look configured,
    and a deployment would fail somewhere far from the cause.
    """

    key: str
    value: str | None
    is_secret: bool


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """Whether a promotion is allowed, to where, and whether it needs a human."""

    allowed: bool
    source: str
    target: str | None
    requires_approval: bool
    reason: str


class EnvironmentService:
    """Reads and writes environments. Every mutation returns enough to audit."""

    def __init__(self, *, pepper: str) -> None:
        # Derived once at composition rather than per call: HKDF per request would be wasted work, and
        # a key that arrives as a parameter is a key a caller can get wrong.
        self._key = derive_secret_key(pepper)

    async def list_for_project(self, session: AsyncSession, *, project_id: uuid.UUID) -> list[EnvironmentRecord]:
        """Every environment of a project, IN PROMOTION ORDER.

        Ordered by `position` rather than by name or creation time, because the order is the pipeline
        and a UI that displayed them alphabetically would show `production` before `staging`.
        """
        result = await session.execute(
            text(
                "SELECT id, project_id, name, kind, k8s_context, requires_approval, position "
                "FROM environments WHERE project_id = :project ORDER BY position"
            ),
            {"project": project_id},
        )
        return [
            EnvironmentRecord(
                id=row["id"],
                project_id=row["project_id"],
                name=str(row["name"]),
                kind=str(row["kind"]),
                k8s_context=row["k8s_context"],
                requires_approval=bool(row["requires_approval"]),
                position=int(row["position"]),
            )
            for row in result.mappings()
        ]

    async def create(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        name: str,
        kind: str,
        k8s_context: str | None = None,
        requires_approval: bool | None = None,
    ) -> EnvironmentRecord:
        """Add an environment at the end of the project's pipeline."""
        cleaned = name.strip()
        if not cleaned:
            raise problem("environment-invalid", detail="an environment needs a name")
        if kind not in ENVIRONMENT_KINDS:
            raise problem(
                "environment-invalid",
                detail=f"{kind!r} is not an environment kind; one of {', '.join(ENVIRONMENT_KINDS)}",
            )
        gate = self._resolve_requirement(kind=kind, requested=requires_approval)

        # APPENDED, NOT INSERTED. A new environment goes last, because inserting into the middle would
        # renumber existing rows and change what "promote" means for a pipeline somebody is mid-way
        # through. Reordering is a separate, explicit action.
        next_position = await session.scalar(
            text("SELECT COALESCE(MAX(position), -1) + 1 FROM environments WHERE project_id = :project"),
            {"project": project_id},
        )
        environment_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO environments (id, project_id, tenant_id, name, kind, k8s_context, "
                "requires_approval, position) "
                "VALUES (:id, :project, :tenant, :name, :kind, :context, :gate, :position)"
            ),
            {
                "id": environment_id,
                "project": project_id,
                "tenant": tenant_id,
                "name": cleaned,
                "kind": kind,
                "context": k8s_context,
                "gate": gate,
                "position": int(next_position or 0),
            },
        )
        return EnvironmentRecord(
            id=environment_id,
            project_id=project_id,
            name=cleaned,
            kind=kind,
            k8s_context=k8s_context,
            requires_approval=gate,
            position=int(next_position or 0),
        )

    async def update(
        self,
        session: AsyncSession,
        *,
        environment_id: uuid.UUID,
        k8s_context: str | None = None,
        requires_approval: bool | None = None,
    ) -> EnvironmentRecord:
        """Change what can be changed. Name, kind and position are not among them here.

        A rename would break every recorded reference to the environment by name — the Command Center's
        "deploy to staging", a promotion history, an audit reason — so it is not offered as an
        incidental field on an update.
        """
        current = await self._read(session, environment_id=environment_id)
        gate = (
            current.requires_approval
            if requires_approval is None
            else self._resolve_requirement(kind=current.kind, requested=requires_approval)
        )
        context = current.k8s_context if k8s_context is None else (k8s_context.strip() or None)
        await session.execute(
            text(
                "UPDATE environments SET k8s_context = :context, requires_approval = :gate, "
                "updated_at = now() WHERE id = :id"
            ),
            {"context": context, "gate": gate, "id": environment_id},
        )
        return EnvironmentRecord(
            id=current.id,
            project_id=current.project_id,
            name=current.name,
            kind=current.kind,
            k8s_context=context,
            requires_approval=gate,
            position=current.position,
        )

    async def delete(self, session: AsyncSession, *, environment_id: uuid.UUID) -> EnvironmentRecord:
        """Remove an environment and CLOSE THE GAP its position leaves.

        Leaving a hole would make `promote_from` skip a number, and "the next environment" would depend
        on whether anything had ever been deleted. The remaining rows are renumbered in one statement so
        the sequence stays contiguous.
        """
        current = await self._read(session, environment_id=environment_id)
        await session.execute(text("DELETE FROM environments WHERE id = :id"), {"id": environment_id})
        await session.execute(
            text(
                "UPDATE environments SET position = position - 1 WHERE project_id = :project AND position > :position"
            ),
            {"project": current.project_id, "position": current.position},
        )
        return current

    async def set_variable(
        self,
        session: AsyncSession,
        *,
        environment_id: uuid.UUID,
        key: str,
        value: str,
        is_secret: bool,
    ) -> VariableRecord:
        """Write one variable, sealing it when it is a secret.

        UPSERT on (environment, key), because a variable is identified by its name within its
        environment and a second POST of the same key is an operator correcting a value, not creating a
        duplicate one the next read would have to choose between.
        """
        cleaned = key.strip()
        if not cleaned:
            raise problem("environment-invalid", detail="a variable needs a key")
        # Confirms the environment exists before writing a child row, so a bad id is a 404 about the
        # environment rather than a foreign-key error about a column.
        await self._read(session, environment_id=environment_id)
        sealed = seal_secret(value, environment_id=environment_id, key=self._key) if is_secret else None
        await session.execute(
            text(
                "INSERT INTO environment_variables (id, environment_id, key, value, value_sealed, is_secret) "
                "VALUES (:id, :env, :key, :value, :sealed, :is_secret) "
                "ON CONFLICT (environment_id, key) DO UPDATE SET "
                "value = EXCLUDED.value, value_sealed = EXCLUDED.value_sealed, "
                "is_secret = EXCLUDED.is_secret"
            ),
            {
                "id": uuid.uuid4(),
                "env": environment_id,
                "key": cleaned,
                "value": None if is_secret else value,
                "sealed": sealed,
                "is_secret": is_secret,
            },
        )
        return VariableRecord(key=cleaned, value=None if is_secret else value, is_secret=is_secret)

    async def variables(self, session: AsyncSession, *, environment_id: uuid.UUID) -> list[VariableRecord]:
        """Every variable, with secrets present but withheld. See `VariableRecord`."""
        result = await session.execute(
            text("SELECT key, value, is_secret FROM environment_variables WHERE environment_id = :env ORDER BY key"),
            {"env": environment_id},
        )
        return [
            VariableRecord(
                key=str(row["key"]),
                value=None if row["is_secret"] else row["value"],
                is_secret=bool(row["is_secret"]),
            )
            for row in result.mappings()
        ]

    async def resolved_variables(self, session: AsyncSession, *, environment_id: uuid.UUID) -> dict[str, str]:
        """Every variable WITH its secrets opened, for a deployment to carry.

        DELIBERATELY NOT AN HTTP SHAPE. Nothing in `routes.py` returns this; it exists so a deployment
        transit can build the environment a container runs with. Keeping it off the read surface is what
        stops "show me the environment" from becoming "print the secrets".
        """
        result = await session.execute(
            text(
                "SELECT key, value, value_sealed, is_secret FROM environment_variables "
                "WHERE environment_id = :env ORDER BY key"
            ),
            {"env": environment_id},
        )
        resolved: dict[str, str] = {}
        for row in result.mappings():
            if row["is_secret"]:
                resolved[str(row["key"])] = unseal_secret(
                    row["value_sealed"], environment_id=environment_id, key=self._key
                )
            else:
                resolved[str(row["key"])] = str(row["value"] or "")
        return resolved

    async def promote_from(self, session: AsyncSession, *, environment_id: uuid.UUID) -> PromotionDecision:
        """Where this environment promotes to, and whether that needs a human.

        A DECISION, NOT AN ACTION, and the split is the design. This says "staging promotes to
        production and that requires approval"; the deployment it authorises goes through the chokepoint
        like every other mutation. A promote that applied here would be a second path to the user's
        machine.

        The LAST environment promotes to nothing, and that is reported as a refusal with a reason rather
        than as an empty success — an operator clicking promote on production needs to be told why
        nothing happened.
        """
        current = await self._read(session, environment_id=environment_id)
        result = await session.execute(
            text(
                "SELECT name, requires_approval FROM environments "
                "WHERE project_id = :project AND position > :position "
                "ORDER BY position LIMIT 1"
            ),
            {"project": current.project_id, "position": current.position},
        )
        row = result.mappings().first()
        if row is None:
            return PromotionDecision(
                allowed=False,
                source=current.name,
                target=None,
                requires_approval=False,
                reason=(
                    f"{current.name} is the last environment in this project's pipeline, so there is "
                    "nothing to promote to"
                ),
            )
        target = str(row["name"])
        gate = bool(row["requires_approval"])
        return PromotionDecision(
            allowed=True,
            source=current.name,
            target=target,
            requires_approval=gate,
            reason=(
                f"{current.name} promotes to {target}, which "
                + ("requires human approval" if gate else "is configured for unattended deployment")
            ),
        )

    async def requirement_for(self, session: AsyncSession, *, project_id: uuid.UUID, name: str) -> bool:
        """Whether a mutation targeting this named environment needs a human.

        AN UNKNOWN NAME REQUIRES APPROVAL. That is the fail-safe direction and it is the important line
        in this module: a typo in "staging", a deleted environment, a Command Center instruction naming
        something that does not exist — every one of them must land on "ask a human", never on
        "proceed". A `KeyError` would be worse, because a caller would have to remember to handle it.
        """
        found = await session.scalar(
            text("SELECT requires_approval FROM environments WHERE project_id = :project AND name = :name"),
            {"project": project_id, "name": name.strip()},
        )
        return True if found is None else bool(found)

    @staticmethod
    def _resolve_requirement(*, kind: str, requested: bool | None) -> bool:
        """The approval requirement, with production's floor enforced.

        `None` means "not stated", which resolves to True. An explicit False on a production environment
        is refused rather than silently corrected: an operator who asked for something the platform will
        not do deserves to be told, not to discover later that their setting did not take.
        """
        if kind in PROTECTED_KINDS and requested is False:
            raise problem(
                "environment-invalid",
                detail=(
                    f"an environment of kind {kind!r} cannot waive human approval. Create an "
                    "environment of kind 'custom' if an unattended target is genuinely intended, so "
                    "that what it is is visible in its kind."
                ),
            )
        return True if requested is None else bool(requested)

    async def _read(self, session: AsyncSession, *, environment_id: uuid.UUID) -> EnvironmentRecord:
        result = await session.execute(
            text(
                "SELECT id, project_id, name, kind, k8s_context, requires_approval, position "
                "FROM environments WHERE id = :id"
            ),
            {"id": environment_id},
        )
        row = result.mappings().first()
        if row is None:
            raise problem("environment-absent", detail=f"no environment {environment_id}")
        return EnvironmentRecord(
            id=row["id"],
            project_id=row["project_id"],
            name=str(row["name"]),
            kind=str(row["kind"]),
            k8s_context=row["k8s_context"],
            requires_approval=bool(row["requires_approval"]),
            position=int(row["position"]),
        )
