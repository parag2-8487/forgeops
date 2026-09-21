# SPDX-License-Identifier: FSL-1.1-ALv2
"""The deployment HTTP surface. §2.2.

THE ONE ROUTE THAT MUTATES GOES THROUGH THE CHOKEPOINT, and this module's shape is chosen to make that
unavoidable rather than merely intended. `POST .../deployments` never touches an agent: it writes the
record, asks `GovernanceChokepoint.deploy_manifests` for a decision, and reports what came back. There is
no branch here that sends a command, and §2.2.1's gate asserts mechanically that there could not be.

WHAT THE ROUTE DOES THAT THE TRANSIT CANNOT. It reads the ENVIRONMENT — its approval requirement and its
cluster context — and hands them to the transit. The transit lives in `governance/` and TID251 forbids it
importing `environments/`, so the composition happens here, at the edge, where both are already in scope.
That is also why the approval requirement travels as a boolean argument rather than as a lookup inside the
transit: the transit's job is to enforce it, not to discover it.

THE ORDER IS ROW, THEN TRANSIT, THEN LINK. A deployment blocked at the gate still leaves a record saying
so, because the row is written before the decision is asked for. The alternative — writing it only on
success — is how "nothing happened and nothing says why" gets built.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import problem
from ..environments.service import EnvironmentService
from .service import DeploymentRecord, DeploymentService

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/deployments",
    tags=["deployments"],
    dependencies=[Depends(require_principal)],
)


class DeploymentRequest(BaseModel):
    """What an operator asks for."""

    environment_id: uuid.UUID
    #: Workspace-relative manifest paths, applied in the order given.
    manifests: list[str] = Field(min_length=1)
    #: Overrides the environment's recorded context. Absent means the environment's, then the operator's
    #: current one — resolved by the agent and reported back, so "which cluster" is never a guess.
    cluster_context: str | None = None
    namespace: str | None = None
    #: Per-workload health wait. The agent clamps it; naming it here lets a slow image pull be allowed
    #: for without changing a default that suits everything else.
    health_timeout_seconds: int = Field(default=0, ge=0, le=600)
    reason: str = Field(default="", max_length=500)


def _service(request: Request) -> DeploymentService:
    return request.app.state.deployment_service  # type: ignore[no-any-return]


def _environments(request: Request) -> EnvironmentService:
    return request.app.state.environment_service  # type: ignore[no-any-return]


def _as_json(record: DeploymentRecord) -> dict[str, Any]:
    """The wire shape, built field by field so a future column is not published by accident."""
    return {
        "id": str(record.id),
        "environment_id": str(record.environment_id),
        "change_set_id": str(record.change_set_id) if record.change_set_id else None,
        "status": record.status,
        # `healthy: null` is a real value and means "nothing verified it" — a failed apply, or one still
        # in flight. A client must be able to tell that from `false`, which asserts the workloads were
        # checked and were not ready.
        "healthy": record.healthy,
        "stable": record.stable,
        "manifests": list(record.manifests),
        "manifest_digest": record.manifest_digest,
        "cluster_context": record.cluster_context,
        "namespace": record.namespace,
        "report": record.report,
        "created_at": record.created_at,
        "completed_at": record.completed_at,
    }


@router.get("")
async def list_deployments(
    project_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """§2.3's timeline source: this project's deployments, newest first."""
    records = await _service(request).history(session, project_id=project_id)
    return {"deployments": [_as_json(record) for record in records]}


@router.post("", status_code=202)
async def request_deployment(
    project_id: uuid.UUID,
    body: DeploymentRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Record the intent, then ask the chokepoint.

    202 rather than 201: what this returns is a decision, and in the common case the decision is "a human
    must approve this first". A 201 would imply the deployment is happening.
    """
    environments = {
        record.id: record for record in await _environments(request).list_for_project(session, project_id=project_id)
    }
    environment = environments.get(body.environment_id)
    if environment is None:
        # A 404 about the ENVIRONMENT, not a foreign-key error about a column. The caller named something
        # that is not in this project, and the message says which project it looked in.
        raise problem(
            "environment-absent",
            detail=(
                f"environment {body.environment_id} does not belong to project {project_id}, so there "
                "is nothing to deploy to"
            ),
        )

    service = _service(request)
    record = await service.create(
        session,
        project_id=project_id,
        environment_id=environment.id,
        tenant_id=principal.tenant_id,
        manifests=body.manifests,
        # THE ENVIRONMENT'S CONTEXT IS THE DEFAULT. An operator who wired a cluster to an environment
        # should not have to repeat it per deployment, and a request that omits it must not silently go
        # to whatever context the agent's shell happens to have selected.
        cluster_context=body.cluster_context or environment.k8s_context,
        namespace=body.namespace,
        requested_by=principal.user_id,
    )

    submission = await request.app.state.governance_chokepoint.deploy_manifests(
        session,
        project_id=project_id,
        principal=principal,
        deployment_id=record.id,
        environment_name=environment.name,
        # The value the whole of §2.1 exists to produce. Passed explicitly because `governance/` may not
        # import `environments/`; enforced there, discovered here.
        environment_requires_approval=environment.requires_approval,
        manifests=list(record.manifests),
        cluster_context=record.cluster_context,
        namespace=record.namespace,
        health_timeout_seconds=body.health_timeout_seconds,
        reason=body.reason or f"deployment to {environment.name}",
    )

    await service.attach_change_set(
        session,
        deployment_id=record.id,
        change_set_id=submission.change_set_id,
        # THE DEPLOYMENT'S STATUS FOLLOWS THE CHANGE SET'S, rather than being decided again here. Two
        # places deciding the same thing is how they come to disagree.
        status="applying" if submission.status == "applying" else "pending_approval",
    )
    await session.commit()

    settled = await service.read(session, deployment_id=record.id)
    return {
        "deployment": _as_json(settled),
        "change_set_id": str(submission.change_set_id),
        "outcome": submission.outcome,
        "blast_radius_score": submission.blast_radius_score,
        "blast_radius_verdict": submission.blast_radius_verdict,
        # STATED EXPLICITLY, so a client does not infer it from the outcome string. Three mechanisms can
        # demand a human and the audit reason names which; this is the fact a UI needs.
        "requires_approval": submission.status == "pending_approval",
        "environment": environment.name,
    }


@router.get("/rollback-target")
async def rollback_target(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """The newest STABLE deployment of an environment — what a rollback would restore.

    Returns `{"target": null}` with a reason rather than a 404 when there is not one. An environment whose
    only deployment was degraded genuinely has nothing to roll back to, and that is an answer; a 404 would
    read as "this endpoint is wrong".

    `stable` is set on HEALTH, not on apply, which is what makes this trustworthy: a deployment whose
    workloads never converged is not a state to return to.
    """
    target = await _service(request).rollback_target(session, environment_id=environment_id)
    if target is None:
        return {
            "target": None,
            "reason": (
                "this environment has no healthy deployment on record, so there is no stable state to "
                "roll back to. A deployment is marked stable only when its workloads converged."
            ),
        }
    return {"target": _as_json(target), "reason": "the newest deployment whose workloads converged"}


@router.get("/{deployment_id}")
async def read_deployment(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    return _as_json(await _service(request).read(session, deployment_id=deployment_id))
