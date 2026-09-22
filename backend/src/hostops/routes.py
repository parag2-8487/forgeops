# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent operation proxy for Docker AND Kubernetes named operations. §2.4, §2.9.

ONE MODULE FOR TWO RESOURCE FAMILIES, which is what `phases.md` §2.4's combined box asks for and also the
only arrangement that keeps its promise. The box reads: "One whitelist, one signing path, one chokepoint
transit for every mutating call; satisfies both." A `docker/` module and a `kubernetes/` module would be two
of each within a week — and the second copy is where the drift lands.

WHAT THIS MODULE IS AND IS NOT. It is a translator: an HTTP request naming a project becomes a named agent
operation with validated arguments, and the agent's reply becomes a typed response. It is NOT a client of
Docker or Kubernetes, it holds no kubeconfig, and it has no branch that reaches either system directly. The
agent is the only component that touches a host, which is the property the whole mTLS-and-whitelist design
exists to preserve, and it is checkable here by the absence of any such import.

THE READ AND THE WRITE PATHS ARE DIFFERENT CALLS ON PURPOSE.

* A read goes through `GovernanceChokepoint.read_inventory`: admission, policy, a signed envelope with no
  approval, and the agent's own answer awaited so an HTTP GET can return it. No change set, because
  nothing changed.
* A write goes through `GovernanceChokepoint.transit_host_action`: the full six stages, a change-set row,
  a blast-radius score, an audit record and a rollback handle. An action started from a panel has exactly
  the authority an action started from anywhere else has.

WHY THE RESPONSES CARRY `reported_at` AND `partial_reasons` OUTWARD UNCHANGED. The agent measures on the
operator's host; only the agent knows when. A timestamp assigned here would be when the BACKEND answered,
which is a different fact and the one that makes a stale panel look fresh. Likewise, a family the agent
could not read is named rather than rendered as empty — "this cluster has no ingresses" and "this principal
may not list ingresses" are opposite facts and an empty array states the wrong one.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import problem
from ..environments.service import EnvironmentService
from ..governance.chokepoint import (
    DOCKER_CONTAINER_OPERATION,
    DOCKER_IMAGE_OPERATION,
    DOCKER_INVENTORY_OPERATION,
    DOCKER_LOGS_OPERATION,
    KUBERNETES_INVENTORY_OPERATION,
    KUBERNETES_POD_DETAIL_OPERATION,
    KUBERNETES_WORKLOAD_OPERATION,
    GovernanceChokepoint,
    Submission,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}",
    tags=["host-operations"],
    # DENY BY DEFAULT AT THE ROUTER, not per handler. A route added later inherits the principal
    # requirement rather than needing somebody to remember it.
    dependencies=[Depends(require_principal)],
)

#: How long a read may take before the caller is told the agent said nothing.
#:
#: 60 seconds because a `kubectl get` across every namespace on an unhealthy cluster genuinely takes tens
#: of seconds, and an unhealthy cluster is exactly when somebody is looking at this panel. Shorter would
#: turn "slow" into "unreachable" at the moment the difference matters most.
_READ_TIMEOUT_SECONDS = 60.0


def _chokepoint(request: Request) -> GovernanceChokepoint:
    return request.app.state.chokepoint  # type: ignore[no-any-return]


def _environments(request: Request) -> EnvironmentService:
    return request.app.state.environment_service  # type: ignore[no-any-return]


class ContainerActionRequest(BaseModel):
    """One action against one named container.

    The action is a `Literal`, so an unknown verb is a 422 from the schema rather than a refusal
    discovered three layers down after a signature was minted. The agent enforces the same closed set
    again — two layers, because this one is a convenience for the caller and that one is the boundary.
    """

    action: Literal["start", "stop", "restart", "remove"]
    #: A name or an ID. Never a pattern: one envelope acting on an unbounded set is the blast radius the
    #: change-set machinery exists to bound, and `min_length=1` refuses the empty name that would mean
    #: "all" to a naive wrapper.
    container: str = Field(min_length=1, max_length=255)
    reason: str = Field(default="", max_length=500)


class ImageActionRequest(BaseModel):
    """One action against one named image reference."""

    action: Literal["pull", "remove"]
    image: str = Field(min_length=1, max_length=512)
    reason: str = Field(default="", max_length=500)


class WorkloadActionRequest(BaseModel):
    """Scale, restart or roll back one named workload.

    `environment_id` is optional and its presence changes the governance outcome, which is worth stating:
    an action against a recorded environment inherits that environment's approval requirement, and one
    without an environment reaches the policy with the environment absent — which the bundle already
    treats as "ask a human". Omitting it therefore cannot be a way to need less approval.
    """

    action: Literal["scale", "restart", "rollback"]
    kind: Literal["deployment", "statefulset", "daemonset"]
    name: str = Field(min_length=1, max_length=255)
    namespace: str = Field(min_length=1, max_length=255)
    #: Applies to `scale`. The agent bounds it and names the bound in its refusal; the ceiling here is the
    #: same number so a caller learns it from the schema rather than from a failure.
    replicas: int = Field(default=0, ge=0, le=100)
    #: Applies to `rollback`. Zero means the previous revision.
    to_revision: int = Field(default=0, ge=0)
    environment_id: uuid.UUID | None = None
    cluster_context: str | None = None
    health_timeout_seconds: int = Field(default=0, ge=0, le=600)
    reason: str = Field(default="", max_length=500)


class ActionAccepted(BaseModel):
    """What a mutating call returns.

    The SAME shape a deployment returns, deliberately: a caller handling one handles both, and the three
    outcomes that matter — delivered, waiting for a human, blocked — are named rather than inferred from a
    status code.
    """

    change_set_id: uuid.UUID
    status: str
    outcome: str
    blast_radius_score: int | None = None
    blast_radius_verdict: str | None = None


def _accepted(submission: Submission) -> ActionAccepted:
    return ActionAccepted(
        change_set_id=submission.change_set_id,
        status=submission.status,
        outcome=submission.outcome,
        blast_radius_score=submission.blast_radius_score,
        blast_radius_verdict=submission.blast_radius_verdict,
    )


@router.get("/docker/inventory", summary="Containers, images, volumes and networks on the agent's host")
async def docker_inventory(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    stats: Annotated[
        bool,
        Query(
            description=(
                "Sample CPU, memory and network per running container. Costs about a second of wall "
                "time, so a list view can omit it; every measured field is null when it is omitted, "
                "never zero."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Read the host's container inventory through the agent.

    Returns the agent's own payload, unaltered. Nothing here recomputes, defaults or fills a measurement:
    `stats_sampled` and the nullable figures are the contract that lets a panel distinguish "not measured"
    from "idle", and a backend that helpfully substituted zeros would destroy exactly that.
    """
    return dict(
        await _chokepoint(request).read_inventory(
            session,
            project_id=project_id,
            principal=principal,
            operation=DOCKER_INVENTORY_OPERATION,
            args={"stats": stats},
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
    )


@router.get("/kubernetes/inventory", summary="Namespaces, nodes, pods, workloads, services and config")
async def kubernetes_inventory(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    namespace: Annotated[
        str | None,
        Query(description="A single namespace. Absent means every namespace the operator can see."),
    ] = None,
    cluster_context: Annotated[str | None, Query(description="A kubectl context name.")] = None,
) -> dict[str, Any]:
    """Read the cluster inventory through the agent.

    Returned unaltered, for the reason the Docker read gives, plus one specific to clusters:
    `partial_reasons` names each family that could not be read and why. Flattening that into an empty list
    would tell an operator their cluster has no ingresses when the truth is that they may not list them.
    """
    args: dict[str, Any] = {}
    if namespace:
        args["namespace"] = namespace
    if cluster_context:
        args["context"] = cluster_context
    return dict(
        await _chokepoint(request).read_inventory(
            session,
            project_id=project_id,
            principal=principal,
            operation=KUBERNETES_INVENTORY_OPERATION,
            args=args,
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
    )


@router.post(
    "/docker/containers/actions",
    status_code=202,
    summary="Start, stop, restart or remove one container (through the chokepoint)",
)
async def container_action(
    project_id: uuid.UUID,
    body: ContainerActionRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ActionAccepted:
    """202, because the answer is a governance decision and not the action's outcome.

    A container action can be blocked, held for a human, or delivered. Returning 200 with the container's
    new state would be a lie in two of those three cases.

    NO ENVIRONMENT. A Docker action is against the operator's own machine, which no `environments` row
    describes. `environment_name=None` reaches the policy as an absent environment — the branch the bundle
    treats as "ask a human" — so the absence cannot be a route to less approval.
    """
    submission = await _chokepoint(request).transit_host_action(
        session,
        project_id=project_id,
        principal=principal,
        operation=DOCKER_CONTAINER_OPERATION,
        target=body.container,
        args={"action": body.action, "container": body.container},
        environment_name=None,
        environment_requires_approval=False,
        reason=body.reason or f"{body.action} container {body.container}",
    )
    return _accepted(submission)


@router.post(
    "/docker/images/actions",
    status_code=202,
    summary="Pull or remove one image (through the chokepoint)",
)
async def image_action(
    project_id: uuid.UUID,
    body: ImageActionRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ActionAccepted:
    """202, for the reason above."""
    submission = await _chokepoint(request).transit_host_action(
        session,
        project_id=project_id,
        principal=principal,
        operation=DOCKER_IMAGE_OPERATION,
        target=body.image,
        args={"action": body.action, "image": body.image},
        environment_name=None,
        environment_requires_approval=False,
        reason=body.reason or f"{body.action} image {body.image}",
    )
    return _accepted(submission)


@router.post(
    "/kubernetes/workloads/actions",
    status_code=202,
    summary="Scale, restart or roll back one workload (through the chokepoint)",
)
async def workload_action(
    project_id: uuid.UUID,
    body: WorkloadActionRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ActionAccepted:
    """202, for the reason above.

    THE ENVIRONMENT IS READ HERE AND HANDED DOWN, exactly as the deployment route does it and for the same
    reason: the transit lives in `governance/`, TID251 forbids it importing `environments/`, and the
    transit's job is to ENFORCE an approval requirement rather than to discover one. An unknown
    environment id is a 404 naming it rather than a silent fall back to "no approval needed".
    """
    environment_name: str | None = None
    requires_approval = False
    cluster_context = body.cluster_context

    if body.environment_id is not None:
        # READ THROUGH THE PROJECT, not by id alone, which is the same scoping the deployment route uses:
        # an environment id belonging to another project must be a 404 here rather than a successful read
        # of somebody else's approval requirement.
        environments = {
            record.id: record
            for record in await _environments(request).list_for_project(session, project_id=project_id)
        }
        environment = environments.get(body.environment_id)
        if environment is None:
            raise problem(
                "environment-absent",
                detail=(
                    f"no environment {body.environment_id} belongs to project {project_id}, so the "
                    "approval requirement this action would inherit cannot be read. Refused rather "
                    "than treated as an action with no environment, which would need less approval."
                ),
            )
        environment_name = environment.name
        requires_approval = environment.requires_approval
        cluster_context = cluster_context or environment.cluster_context

    args: dict[str, Any] = {
        "action": body.action,
        "kind": body.kind,
        "name": body.name,
        "namespace": body.namespace,
        "health_timeout_seconds": body.health_timeout_seconds,
    }
    if body.action == "scale":
        args["replicas"] = body.replicas
    if body.action == "rollback" and body.to_revision:
        args["to_revision"] = body.to_revision
    if cluster_context:
        args["context"] = cluster_context

    submission = await _chokepoint(request).transit_host_action(
        session,
        project_id=project_id,
        principal=principal,
        operation=KUBERNETES_WORKLOAD_OPERATION,
        target=f"{body.kind}/{body.name}",
        args=args,
        environment_name=environment_name,
        environment_requires_approval=requires_approval,
        reason=body.reason or f"{body.action} {body.kind}/{body.name} in {body.namespace}",
    )
    return _accepted(submission)


@router.get("/docker/containers/{container}/logs", summary="One container's recent output")
async def container_logs(
    project_id: uuid.UUID,
    container: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    tail_lines: Annotated[int, Query(ge=1, le=2000)] = 200,
    since_seconds: Annotated[int, Query(ge=0, le=86400)] = 0,
) -> dict[str, Any]:
    """Returned unaltered, including `truncated`.

    A tail that is not marked as a tail lets a reader conclude an error never happened when it fell off the
    top, so the applied bounds and the truncation flag travel outward as the agent reported them.
    """
    return dict(
        await _chokepoint(request).read_inventory(
            session,
            project_id=project_id,
            principal=principal,
            operation=DOCKER_LOGS_OPERATION,
            args={"container": container, "tail_lines": tail_lines, "since_seconds": since_seconds},
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
    )


@router.get("/kubernetes/namespaces/{namespace}/pods/{pod}", summary="One pod's logs AND its events")
async def pod_detail(
    project_id: uuid.UUID,
    namespace: str,
    pod: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    container_name: Annotated[str | None, Query()] = None,
    tail_lines: Annotated[int, Query(ge=1, le=2000)] = 200,
    since_seconds: Annotated[int, Query(ge=0, le=86400)] = 0,
    cluster_context: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Logs and events together, because for a pod that never scheduled the events are the whole answer.

    An empty log list with populated events is a meaningful result rather than a failure, which is why one
    operation returns both instead of this route asking twice and choosing which absence to believe.
    """
    args: dict[str, Any] = {
        "namespace": namespace,
        "pod": pod,
        "tail_lines": tail_lines,
        "since_seconds": since_seconds,
    }
    if container_name:
        args["container_name"] = container_name
    if cluster_context:
        args["context"] = cluster_context
    return dict(
        await _chokepoint(request).read_inventory(
            session,
            project_id=project_id,
            principal=principal,
            operation=KUBERNETES_POD_DETAIL_OPERATION,
            args=args,
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
    )
