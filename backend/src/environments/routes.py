# SPDX-License-Identifier: FSL-1.1-ALv2
"""The environment HTTP surface. §2.1.

DENY-BY-DEFAULT AT THE ROUTER, like every other domain here: `require_principal` is a router-level
dependency, so a route added later is authenticated the moment it is declared rather than when somebody
remembers to decorate it.

EVERY MUTATION IS AUDITED, AND NONE OF THEM GOES THROUGH THE CHOKEPOINT — and the distinction is worth
being exact about, because "every mutation through the chokepoint" is a rule this codebase enforces with
a gate. The chokepoint governs mutations of the USER'S MACHINE: they compile into a change set, mint a
signed envelope and reach a paired agent. Creating an environment row changes this platform's own
configuration and sends nothing anywhere. Routing it through the chokepoint would mean inventing a
change item for a row that is not a file, which is precisely the synthetic-item defect the clone transit
refused to commit.

What environments DO is decide whether a later mutation needs a human — `requires_approval`, read by
`EnvironmentService.requirement_for`. So the audit trail matters for a different reason than usual: the
row that relaxes an approval gate is itself the interesting event, and §1.9 wants it recorded with who
did it.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit.writer import AuditDraft, AuditWriter
from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from .models import ENVIRONMENT_KINDS
from .service import EnvironmentService

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/environments",
    tags=["environments"],
    dependencies=[Depends(require_principal)],
)


class EnvironmentCreate(BaseModel):
    """A new environment. `requires_approval` omitted means "required"."""

    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(description="one of " + ", ".join(ENVIRONMENT_KINDS))
    k8s_context: str | None = Field(default=None, max_length=253)
    #: OPTIONAL WITH NO DEFAULT IN THE SCHEMA, deliberately. `None` means "the operator did not say",
    #: which the service resolves to True. A schema default of `True` would look identical in the
    #: OpenAPI document and lose the difference between "asked for the safe thing" and "said nothing".
    requires_approval: bool | None = None


class EnvironmentUpdate(BaseModel):
    k8s_context: str | None = None
    requires_approval: bool | None = None


class VariableWrite(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str
    #: Sealed on the way in and withheld on the way out when true. See `VariableRecord`.
    is_secret: bool = False


def _writer(request: Request) -> AuditWriter:
    """The composed audit writer, ANNOTATED so `check-chokepoint.sh` can decide the boundary.

    `request.app.state.audit_writer` is untypeable by the gate, which reports an unresolved receiver and
    fails — correctly: a mutation primitive reached through an untyped attribute is exactly how a call
    outside the boundary would hide. Naming the type is the fix; an exemption would not be.

    It refuses an uncomposed writer rather than returning `None`, because a mutation that silently goes
    unlogged is worse than a 500. §1.9 makes auditability the property availability yields to.
    """
    writer = getattr(request.app.state, "audit_writer", None)
    if writer is None:
        raise RuntimeError("app.state.audit_writer is not composed; every environment mutation must be auditable")
    return writer


def _service(request: Request) -> EnvironmentService:
    return request.app.state.environment_service  # type: ignore[no-any-return]


def _as_json(record: Any) -> dict[str, Any]:
    """The wire shape of an environment. Built field by field so a future column is not leaked by
    accident — `asdict` on a record that later grows a sealed field would publish it."""
    return {
        "id": str(record.id),
        "name": record.name,
        "kind": record.kind,
        "k8s_context": record.k8s_context,
        "requires_approval": record.requires_approval,
        "position": record.position,
    }


@router.get("")
async def list_environments(
    project_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Every environment of the project, in promotion order."""
    records = await _service(request).list_for_project(session, project_id=project_id)
    return {"environments": [_as_json(record) for record in records]}


@router.post("", status_code=201)
async def create_environment(
    project_id: uuid.UUID,
    body: EnvironmentCreate,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    record = await _service(request).create(
        session,
        project_id=project_id,
        tenant_id=principal.tenant_id,
        name=body.name,
        kind=body.kind,
        k8s_context=body.k8s_context,
        requires_approval=body.requires_approval,
    )
    await _audit(
        request,
        session,
        principal=principal,
        action="environment.created",
        resource_id=str(record.id),
        reason=(
            f"created environment {record.name!r} of kind {record.kind} at position {record.position}; "
            f"approval {'required' if record.requires_approval else 'waived'}"
        ),
        after_state=_as_json(record),
    )
    await session.commit()
    return _as_json(record)


@router.patch("/{environment_id}")
async def update_environment(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    body: EnvironmentUpdate,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    service = _service(request)
    before = await service.list_for_project(session, project_id=project_id)
    previous = next((record for record in before if record.id == environment_id), None)
    record = await service.update(
        session,
        environment_id=environment_id,
        k8s_context=body.k8s_context,
        requires_approval=body.requires_approval,
    )
    await _audit(
        request,
        session,
        principal=principal,
        action="environment.updated",
        resource_id=str(record.id),
        # THE APPROVAL FLIP IS NAMED IN THE REASON, because it is the one change here that alters what
        # the platform will do without a human, and a reader of the audit chain should not have to diff
        # two JSON blobs to notice it.
        reason=(
            f"updated environment {record.name!r}; approval {'required' if record.requires_approval else 'waived'}"
        ),
        before_state=_as_json(previous) if previous is not None else None,
        after_state=_as_json(record),
    )
    await session.commit()
    return _as_json(record)


@router.delete("/{environment_id}")
async def delete_environment(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    record = await _service(request).delete(session, environment_id=environment_id)
    await _audit(
        request,
        session,
        principal=principal,
        action="environment.deleted",
        resource_id=str(record.id),
        reason=f"deleted environment {record.name!r}; later environments moved up one position",
        before_state=_as_json(record),
    )
    await session.commit()
    return {"deleted": str(record.id), "name": record.name}


@router.get("/{environment_id}/variables")
async def list_variables(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Variables, with secrets present and their values withheld.

    `value: null` with `is_secret: true` is the honest shape: a reader can tell "this environment has a
    DATABASE_URL that I am not being shown" from "this environment has no DATABASE_URL". An empty string
    for both would make an unset variable look configured.
    """
    records = await _service(request).variables(session, environment_id=environment_id)
    return {
        "variables": [{"key": record.key, "value": record.value, "is_secret": record.is_secret} for record in records]
    }


@router.put("/{environment_id}/variables")
async def set_variable(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    body: VariableWrite,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    record = await _service(request).set_variable(
        session,
        environment_id=environment_id,
        key=body.key,
        value=body.value,
        is_secret=body.is_secret,
    )
    await _audit(
        request,
        session,
        principal=principal,
        action="environment.variable_set",
        resource_id=str(environment_id),
        # THE KEY, NEVER THE VALUE. An audit row naming a secret's value would defeat the sealing it
        # was written through, and the audit chain is the longest-lived table in this system.
        reason=f"set variable {record.key!r} ({'secret' if record.is_secret else 'plain'})",
        after_state={"key": record.key, "is_secret": record.is_secret},
    )
    await session.commit()
    return {"key": record.key, "value": record.value, "is_secret": record.is_secret}


@router.get("/{environment_id}/promotion")
async def promotion(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Where this environment promotes to, and whether that will need a human.

    A READ. It tells a caller what a promotion would involve; the deployment that carries it out is a
    mutation and goes through the governance chokepoint. Returning 200 with `allowed: false` rather
    than an error for the last environment: "production promotes to nothing" is a true answer to a
    reasonable question, not a failure.
    """
    decision = await _service(request).promote_from(session, environment_id=environment_id)
    return {
        "allowed": decision.allowed,
        "source": decision.source,
        "target": decision.target,
        "requires_approval": decision.requires_approval,
        "reason": decision.reason,
    }


async def _audit(
    request: Request,
    session: AsyncSession,
    *,
    principal: Principal,
    action: str,
    resource_id: str,
    reason: str,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
) -> None:
    """One audit row per mutation, through the same writer every other domain uses.

    Not the chokepoint — see the module docstring — but not unlogged either. A row that waives an
    approval requirement is exactly the kind of change §1.9 exists to make attributable.
    """
    await _writer(request).append(
        session,
        AuditDraft(
            actor_user_id=principal.user_id,
            actor_kind=principal.kind,
            tenant_id=principal.tenant_id,
            action=action,
            outcome="allowed",
            resource_kind="environment",
            resource_id=resource_id,
            reason=reason,
            before_state=before_state,
            after_state=after_state,
        ),
    )
