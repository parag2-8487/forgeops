# SPDX-License-Identifier: FSL-1.1-ALv2
"""Promotion, rollback and the release timeline. §2.1's last box and §2.3.

All three are DEPLOYMENTS, and that is the whole design. A promotion deploys the source environment's
stable manifest set to the next environment; a rollback deploys the target environment's last stable
manifest set again. Neither gets its own agent operation, neither gets its own authority, and both travel
`deploy_manifests` exactly as a fresh deployment does — so the target environment's approval requirement
applies to a promotion into production whether or not anybody remembered to think about it.

The timeline and the diff are reads over `deployments` rows. The diff compares manifest SETS and the
recorded reports, not cluster state: what this platform knows is what it deployed.
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
from ..deployments.service import DeploymentRecord, DeploymentService
from ..environments.service import EnvironmentService

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/releases",
    tags=["releases"],
    dependencies=[Depends(require_principal)],
)


def _service(request: Request) -> DeploymentService:
    return request.app.state.deployment_service  # type: ignore[no-any-return]


def _environments(request: Request) -> EnvironmentService:
    return request.app.state.environment_service  # type: ignore[no-any-return]


def _as_json(record: DeploymentRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "environment_id": str(record.environment_id),
        "change_set_id": str(record.change_set_id) if record.change_set_id else None,
        "status": record.status,
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


class PromoteRequest(BaseModel):
    """Promote one environment's stable state to the next in the pipeline."""

    source_environment_id: uuid.UUID
    health_timeout_seconds: int = Field(default=0, ge=0, le=600)
    reason: str = Field(default="", max_length=500)


class RollbackRequest(BaseModel):
    """Roll an environment back to a previous stable deployment."""

    environment_id: uuid.UUID
    #: Absent means the newest stable deployment. Naming one lets an operator go further back than one
    #: step, which is the case §2.3's timeline exists to make visible.
    deployment_id: uuid.UUID | None = None
    health_timeout_seconds: int = Field(default=0, ge=0, le=600)
    reason: str = Field(default="", max_length=500)


async def _deploy(
    request: Request,
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    principal: Principal,
    environment: Any,
    manifests: list[str],
    health_timeout_seconds: int,
    reason: str,
) -> dict[str, Any]:
    """Record a deployment and put it through the chokepoint.

    Shared by promote and rollback so neither can acquire a path the other lacks.
    """
    service = _service(request)
    record = await service.create(
        session,
        project_id=project_id,
        environment_id=environment.id,
        tenant_id=principal.tenant_id,
        manifests=manifests,
        cluster_context=environment.k8s_context,
        namespace=None,
        requested_by=principal.user_id,
    )
    submission = await request.app.state.governance_chokepoint.deploy_manifests(
        session,
        project_id=project_id,
        principal=principal,
        deployment_id=record.id,
        environment_name=environment.name,
        environment_requires_approval=environment.requires_approval,
        manifests=list(record.manifests),
        cluster_context=record.cluster_context,
        namespace=record.namespace,
        health_timeout_seconds=health_timeout_seconds,
        reason=reason,
    )
    await service.attach_change_set(
        session,
        deployment_id=record.id,
        change_set_id=submission.change_set_id,
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
        "requires_approval": submission.status == "pending_approval",
        "environment": environment.name,
    }


@router.post("/promote", status_code=202, summary="Promote an environment's stable state to the next")
async def promote(
    project_id: uuid.UUID,
    body: PromoteRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Deploy the source's last stable manifest set to the next environment in the pipeline.

    The TARGET's approval requirement governs, not the source's — promoting into production must ask a
    human even when staging did not. That is enforced by handing the target environment to
    `deploy_manifests`, which is the same path a direct deployment takes.

    Refuses, with the reason, when the source is last in the pipeline or has nothing stable to promote:
    promoting an unverified deployment would carry a broken state forward under the word "promote".
    """
    environments = {
        record.id: record for record in await _environments(request).list_for_project(session, project_id=project_id)
    }
    source = environments.get(body.source_environment_id)
    if source is None:
        raise problem(
            "environment-absent",
            detail=f"environment {body.source_environment_id} does not belong to project {project_id}",
        )

    decision = await _environments(request).promote_from(session, environment_id=source.id)
    if not decision.allowed or decision.target is None:
        raise problem("environment-invalid", detail=decision.reason)

    target = next((row for row in environments.values() if row.name == decision.target), None)
    if target is None:
        # The rule named a target the list does not contain, which can only happen if the pipeline changed
        # under this request. Refused rather than guessed.
        raise problem(
            "environment-absent",
            detail=f"the pipeline names {decision.target} as the next environment and it is no longer there",
        )

    stable = await _service(request).rollback_target(session, environment_id=source.id)
    if stable is None:
        raise problem(
            "deployment-invalid",
            detail=(
                f"{source.name} has no stable deployment to promote. A deployment becomes stable when its "
                "workloads converge, so promoting without one would carry an unverified state forward."
            ),
        )

    return await _deploy(
        request,
        session,
        project_id=project_id,
        principal=principal,
        environment=target,
        manifests=list(stable.manifests),
        health_timeout_seconds=body.health_timeout_seconds,
        reason=body.reason or f"promotion from {source.name} to {target.name}",
    )


@router.post("/rollback", status_code=202, summary="Roll an environment back to a stable deployment")
async def rollback(
    project_id: uuid.UUID,
    body: RollbackRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Re-deploy a previous stable manifest set.

    A rollback is a deployment, so it travels the chokepoint and the environment's approval requirement
    applies. Only a STABLE deployment may be a target: rolling back to a degraded one would restore a
    broken state while reporting success.
    """
    environments = {
        record.id: record for record in await _environments(request).list_for_project(session, project_id=project_id)
    }
    environment = environments.get(body.environment_id)
    if environment is None:
        raise problem(
            "environment-absent",
            detail=f"environment {body.environment_id} does not belong to project {project_id}",
        )

    service = _service(request)
    if body.deployment_id is None:
        target = await service.rollback_target(session, environment_id=environment.id)
        if target is None:
            raise problem(
                "deployment-invalid",
                detail=(
                    f"{environment.name} has no stable deployment to roll back to. `stable` is set on "
                    "health, not on apply, so an environment whose deployments never converged has "
                    "nothing to restore."
                ),
            )
    else:
        target = await service.read(session, deployment_id=body.deployment_id)
        if target.environment_id != environment.id:
            raise problem(
                "deployment-invalid",
                detail=f"deployment {body.deployment_id} does not belong to {environment.name}",
            )
        if not target.stable:
            raise problem(
                "deployment-invalid",
                detail=(
                    f"deployment {body.deployment_id} is not stable, so rolling back to it would restore "
                    "a state whose workloads never converged"
                ),
            )

    return await _deploy(
        request,
        session,
        project_id=project_id,
        principal=principal,
        environment=environment,
        manifests=list(target.manifests),
        health_timeout_seconds=body.health_timeout_seconds,
        reason=body.reason or f"rollback of {environment.name} to deployment {target.id}",
    )


@router.get("/timeline", summary="Deployment history with version metadata")
async def timeline(
    project_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> dict[str, Any]:
    """Every deployment of this project, newest first, with what a timeline marker needs.

    `healthy` stays nullable on the wire: null means nothing verified it, false means the workloads were
    checked and were not ready. A timeline that collapsed those would mark an in-flight rollout as failed.
    """
    history = await _service(request).history(session, project_id=project_id, limit=limit)
    environments = {
        record.id: record.name
        for record in await _environments(request).list_for_project(session, project_id=project_id)
    }
    return {
        "deployments": [
            {**_as_json(record), "environment": environments.get(record.environment_id)} for record in history
        ],
        # So a client can tell "this project has never deployed" from "the list was truncated".
        "count": len(history),
        "limit": limit,
    }


@router.get("/diff", summary="Diff between two deployments")
async def diff(
    project_id: uuid.UUID,
    left_id: uuid.UUID,
    right_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """What changed between two deployments: manifest set, digest, cluster target and health.

    Compares what this platform RECORDED, not live cluster state — a diff against the cluster would be a
    different feature and would need a read per side. `identical` is computed from the manifest digest,
    which is order-independent, so two deployments of the same set in a different order are not reported
    as a change.
    """
    service = _service(request)
    left = await service.read(session, deployment_id=left_id)
    right = await service.read(session, deployment_id=right_id)
    for record in (left, right):
        if record.project_id != project_id:
            raise problem(
                "deployment-invalid",
                detail=f"deployment {record.id} does not belong to project {project_id}",
            )

    left_set, right_set = set(left.manifests), set(right.manifests)
    return {
        "left": _as_json(left),
        "right": _as_json(right),
        "manifests_added": sorted(right_set - left_set),
        "manifests_removed": sorted(left_set - right_set),
        "manifests_unchanged": sorted(left_set & right_set),
        "identical_manifests": left.manifest_digest == right.manifest_digest,
        "cluster_context_changed": left.cluster_context != right.cluster_context,
        "namespace_changed": left.namespace != right.namespace,
        # Both sides' health, so a diff can say "this one converged and that one did not" — the fact an
        # operator comparing two releases is usually after.
        "health_changed": left.healthy != right.healthy,
    }
