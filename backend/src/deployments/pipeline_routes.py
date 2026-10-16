# SPDX-License-Identifier: FSL-1.1-ALv2
"""Starting a durable deployment pipeline and releasing its gate. Phase 2 §2.2 and §2.4a.

TWO ROUTES, AND THE SECOND IS THE INTERESTING ONE.

`POST .../pipelines/deployment` enqueues a run through the task dispatcher — the same seam every other piece
of background work uses, so this route cannot tell which engine is configured and does not try.

`POST .../pipelines/deployment/release` sends the approval event that un-suspends a gated step. What it is
NOT is a second approval mechanism: the apply step still goes through `deploy_manifests`, so the target
environment's own `requires_approval` is evaluated when the step runs, whatever happened here. These are two
different questions — "has a human released this pipeline stage" and "does this environment require an
approval for any deployment" — and answering the first does not answer the second.

WHY RELEASING IS NOT A GET AND CARRIES A REASON. It is an action a human takes that causes a production
deployment to proceed, so it needs a principal, a body, and a record of who did it. The releasing user's
identity travels in the event data and is written into the change set's reason by the apply step, which is
what makes "who let this through" answerable afterwards.
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
from .pipeline import DEPLOYMENT_PIPELINE, PIPELINE_NAME, STEP_APPLY

router = APIRouter(prefix="/projects/{project_id}", tags=["deployments"])


class StartPipelineRequest(BaseModel):
    """What a durable deployment run needs to know."""

    environment_id: uuid.UUID
    #: The image tag the pipeline builds and pushes. Required, because a pipeline that guessed one would
    #: push over whatever tag happened to exist.
    image: str = Field(min_length=1, max_length=512)
    manifests: list[dict[str, Any]] = Field(min_length=1)
    build_context: str | None = None
    dockerfile: str | None = None
    build_args: dict[str, str] | None = None
    registry: str | None = None
    namespace: str | None = None
    cluster_context: str | None = None
    health_timeout_seconds: int = Field(default=300, ge=10, le=3600)


class ReleasePipelineRequest(BaseModel):
    """Releasing one gated step."""

    #: Which gate. Named rather than assumed, so adding a second gate later cannot make an existing client
    #: release the wrong one.
    step: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


class PipelineAccepted(BaseModel):
    """202: the run was accepted, not completed."""

    run_id: str
    workflow: str
    dispatcher: str
    #: What happens next, in words. A client that only had an id would have to know the pipeline's shape to
    #: tell an operator anything.
    next_step: str


class ReleaseAccepted(BaseModel):
    step: str
    released_by: str
    detail: str


@router.post("/pipelines/deployment", status_code=202, summary="Start the durable deployment pipeline")
async def start_pipeline(
    project_id: uuid.UUID,
    body: StartPipelineRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PipelineAccepted:
    """202, because a durable run outlives this request by design.

    The environment is resolved HERE rather than inside the pipeline so a request naming another project's
    environment fails now, with a 404 a caller can act on, instead of three steps later after an image has
    already been built and pushed.
    """
    environment = await EnvironmentService().get_for_project(
        session, project_id=project_id, environment_id=body.environment_id
    )
    if environment is None:
        raise problem(
            "environment-absent",
            detail=(
                f"environment {body.environment_id} does not belong to project {project_id}, so there "
                "is nothing to deploy to"
            ),
        )

    dispatcher = getattr(request.app.state, "task_dispatcher", None)
    if dispatcher is None:
        raise problem(
            "pipeline-engine-absent",
            detail=(
                "no task dispatcher is composed, so a durable run cannot be enqueued. Set TASK_DISPATCHER "
                "to inngest and restart the application."
            ),
        )

    handle = await dispatcher.enqueue(
        PIPELINE_NAME,
        {
            "project_id": str(project_id),
            "environment_id": str(body.environment_id),
            "requested_by": str(principal.user_id),
            "image": body.image,
            "manifests": body.manifests,
            "build_context": body.build_context,
            "dockerfile": body.dockerfile,
            "build_args": body.build_args,
            "registry": body.registry,
            "namespace": body.namespace,
            "cluster_context": body.cluster_context,
            "health_timeout_seconds": body.health_timeout_seconds,
        },
    )

    return PipelineAccepted(
        run_id=handle.id,
        workflow=PIPELINE_NAME,
        dispatcher=handle.dispatcher,
        next_step=(
            f"the image will be built and pushed, then the run waits for a human to release "
            f"{STEP_APPLY} before anything is applied to {environment.name}"
        ),
    )


@router.post(
    "/pipelines/deployment/release",
    status_code=202,
    summary="Release a gated pipeline step (does not replace the environment's own approval)",
)
async def release_pipeline_step(
    project_id: uuid.UUID,
    body: ReleasePipelineRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
) -> ReleaseAccepted:
    """Send the approval event that un-suspends a gated step.

    THE STEP NAME IS CHECKED AGAINST THE WORKFLOW'S GATED STEPS. Sending an arbitrary event name would
    silently do nothing — the run would stay suspended and the operator would believe they had released it,
    which is the worst available outcome for an approval mechanism.
    """
    gated = {step.name for step in DEPLOYMENT_PIPELINE.steps if step.requires_approval}
    if body.step not in gated:
        raise problem(
            "pipeline-step-not-gated",
            detail=(
                f"{body.step!r} is not a gated step of {PIPELINE_NAME}. The gated steps are "
                f"{', '.join(sorted(gated))}; releasing anything else would do nothing while appearing to "
                "succeed."
            ),
        )

    client = getattr(request.app.state, "inngest_client", None)
    if client is None:
        raise problem(
            "pipeline-engine-absent",
            detail=(
                "the durable engine is not composed, so there is no suspended run to release. Set "
                "TASK_DISPATCHER to inngest and restart the application."
            ),
        )

    await _send_release(
        client,
        event_name=DEPLOYMENT_PIPELINE.approval_event(body.step),
        data={
            # WHO RELEASED IT travels with the event and is recorded by the step. Without this the pipeline
            # would resume with no record of the human who allowed it to.
            "approved_by": principal.email or principal.subject,
            "approved_by_user_id": str(principal.user_id),
            "approval_reason": body.reason,
            "project_id": str(project_id),
        },
    )

    return ReleaseAccepted(
        step=body.step,
        released_by=principal.email or principal.subject,
        detail=(
            "the suspended run will resume. The environment's own approval requirement still applies when "
            "the apply runs, so this release is not the last word on whether it deploys."
        ),
    )


async def _send_release(client: Any, *, event_name: str, data: dict[str, Any]) -> None:
    """Send one event.

    Isolated so this module holds no engine import of its own: `core/tasks.py` is the only module permitted
    to import the SDK, and this reaches the client object it built rather than constructing anything.
    """
    from ..core.tasks import send_engine_event

    await send_engine_event(client, name=event_name, data=data)
