"""The deployment pipeline as a durable workflow. Phase 2 §2.2 and §2.4a.

WHAT THIS FILE IS ALLOWED TO KNOW. Nothing about Inngest, Temporal or any engine. It registers four plain
async handlers with `@register_task` and one `WorkflowDefinition` describing their order and which of them
waits for a human. `core/tasks.py` alone turns that description into engine objects. That is the
"thin wrapper" pattern §2.4a asks for, and it is what makes the engine decision reversible: the same four
handlers and the same description would drive a Temporal translator.

WHY THE PIPELINE IS DURABLE AND NOT A LOOP. `build → push → apply → verify` is four operations against three
different systems, each of which can fail on its own, and the whole thing outlives any request. Two
properties matter and neither is available from a background task:

  1. A RUN THAT FAILS AT APPLY RESUMES AT APPLY. Each step is a checkpoint, so a retry does not rebuild and
     re-push an image that is already in the registry — which costs minutes and, on a metered registry,
     money. This is the property that makes the engine worth operating.

  2. AN APPROVAL GATE IS A SUSPENDED RUN, not a poll. The apply step waits for an event; the run occupies
     nothing while a human thinks. A gate built from a sleep loop would hold a worker for hours and would
     lose its place if the process restarted.

WHAT THIS PIPELINE IS NOT ALLOWED TO BE: A WAY AROUND THE CHOKEPOINT. Every mutating step here calls the
same `transit_host_action` or `deploy_manifests` a human clicking a button calls, so each one produces a
change set, a blast-radius score, an audit row and a rollback handle. The engine decides WHEN a step runs.
It has no say in whether the step is allowed to, and `check-chokepoint` enforces that structurally: this
module cannot reach `send_command` at all.

WHY THE GATE IS ON APPLY AND NOT ON BUILD OR PUSH. A build writes nothing outside the workspace and a push
writes an immutable tag to a registry; both are reversible and neither changes what is running. An apply
changes what is serving traffic. Gating the cheap steps would train operators to click through the gate,
which is how a gate stops being read.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.core.tasks import WorkflowDefinition, WorkflowStep, register_task, register_workflow

#: The workflow's name, which derives its trigger event (`forgeops/deployment.pipeline`) and its approval
#: events. Named for what it does rather than for the engine running it.
PIPELINE_NAME = "deployment.pipeline"

STEP_BUILD = "deployment.pipeline.build"
STEP_PUSH = "deployment.pipeline.push"
STEP_APPLY = "deployment.pipeline.apply"
STEP_VERIFY = "deployment.pipeline.verify"


class PipelineContext(Protocol):
    """What a step needs from the running application.

    A PROTOCOL AND NOT A GLOBAL. The handlers below are module-level functions because `@register_task`
    registers by name, so they cannot take constructor arguments — and reaching for `app.state` inside them
    would make them untestable without a running app and would tie a pipeline step to FastAPI. The context
    is installed once at composition and the Protocol is what the tests implement.
    """

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        """A session factory. A step gets its OWN session, because a durable step may run minutes or hours
        after the one before it and a session held across that is a connection leaked for the duration."""
        ...

    @property
    def chokepoint(self) -> Any:
        """The governance chokepoint. `Any` because `deployments/` may not import `governance/` types
        beyond what it already does, and this Protocol exists to describe a need rather than a type."""
        ...

    async def principal_for(self, session: AsyncSession, user_id: uuid.UUID) -> Any:
        """The principal a step acts as.

        RE-READ PER STEP AND NOT CARRIED IN THE PAYLOAD. A principal serialised into an event would be an
        authorisation snapshot that outlived the human's access: a run suspended overnight would apply in
        the morning with permissions revoked at midnight. Re-reading means a revoked user's suspended
        pipeline fails at the gate, which is the correct outcome.
        """
        ...


class _NotComposed:
    """The context before composition. Every access raises with the reason.

    A `None` DEFAULT WOULD PRODUCE `AttributeError: 'NoneType' has no attribute 'sessions'` at the first
    step of a real deployment. This says what is actually wrong.
    """

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(
            "the deployment pipeline has no context: call `compose_pipeline` during app startup. "
            "Without it a durable step has no database session and no chokepoint, so it cannot run."
        )


_context: Any = _NotComposed()


def compose_pipeline(context: PipelineContext) -> None:
    """Install the context the steps use. Called once, from the lifespan."""
    global _context  # noqa: PLW0603 - one module-level seam, set once at startup
    _context = context


@register_task(STEP_BUILD)
async def build_image(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the image, through the chokepoint.

    Returns `stop: True` when the build fails. A FAILED BUILD IS A RESULT everywhere else in this codebase
    — the agent reports it with the compiler output rather than raising — and a pipeline that raised here
    would discard that output, which is the only thing that makes the failure actionable.
    """
    image = _required(payload, "image")
    submission = await _transit_image(payload, action="build", image=image, extra=_build_arguments(payload))
    return {
        "build_change_set_id": str(submission["change_set_id"]),
        "build_outcome": submission["outcome"],
        # A build that was blocked or is waiting for a human has not produced an image, so the push must
        # not run. `blocked` and `pending_approval` are both "not built yet", and treating them as success
        # would push whatever tag happened to exist locally from a previous run.
        "stop": submission["outcome"] not in {"applying", "applied"},
    }


@register_task(STEP_PUSH)
async def push_image(payload: dict[str, Any]) -> dict[str, Any]:
    """Push the image, through the chokepoint. The credential is resolved at delivery, not here."""
    image = _required(payload, "image")
    extra: dict[str, Any] = {}
    if payload.get("registry"):
        extra["registry"] = str(payload["registry"])
    submission = await _transit_image(payload, action="push", image=image, extra=extra)
    return {
        "push_change_set_id": str(submission["change_set_id"]),
        "push_outcome": submission["outcome"],
        "stop": submission["outcome"] not in {"applying", "applied"},
    }


@register_task(STEP_APPLY)
async def apply_manifests(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the manifests, through the chokepoint's deployment transit.

    THE ENVIRONMENT'S OWN APPROVAL REQUIREMENT STILL APPLIES HERE, on top of the workflow's gate. They are
    two different questions — "has a human released this pipeline stage" and "does this environment require
    an approval for any deployment" — and the chokepoint answers the second whatever the engine did about
    the first. A pipeline that satisfied its own gate and then bypassed the environment's would be a way
    around governance built out of a scheduler.
    """
    project_id = uuid.UUID(_required(payload, "project_id"))
    environment_id = uuid.UUID(_required(payload, "environment_id"))
    manifests = payload.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise ValueError("the pipeline's apply step was given no manifests, so there is nothing to deploy")

    async with _context.sessions() as session:
        principal = await _context.principal_for(session, uuid.UUID(_required(payload, "requested_by")))
        environment = (
            (
                await session.execute(
                    text(
                        "SELECT name, requires_approval, k8s_context FROM environments "
                        "WHERE id = :id AND project_id = :project_id"
                    ),
                    {"id": str(environment_id), "project_id": str(project_id)},
                )
            )
            .mappings()
            .first()
        )
        if environment is None:
            raise ValueError(f"environment {environment_id} does not belong to project {project_id}")

        deployment_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO deployments (id, project_id, environment_id, status, manifests, "
                " cluster_context, namespace, requested_by, created_at) "
                "VALUES (:id, :project_id, :environment_id, 'pending', CAST(:manifests AS jsonb), "
                " :cluster_context, :namespace, :requested_by, now())"
            ),
            {
                "id": str(deployment_id),
                "project_id": str(project_id),
                "environment_id": str(environment_id),
                "manifests": _json(manifests),
                "cluster_context": payload.get("cluster_context") or environment["k8s_context"],
                "namespace": payload.get("namespace") or "default",
                "requested_by": _required(payload, "requested_by"),
            },
        )
        await session.commit()

        submission = await _context.chokepoint.deploy_manifests(
            session,
            project_id=project_id,
            principal=principal,
            deployment_id=deployment_id,
            environment_name=str(environment["name"]),
            environment_requires_approval=bool(environment["requires_approval"]),
            manifests=list(manifests),
            cluster_context=payload.get("cluster_context") or environment["k8s_context"],
            namespace=payload.get("namespace") or "default",
            health_timeout_seconds=int(payload.get("health_timeout_seconds") or 300),
            reason=f"durable pipeline deployment to {environment['name']}",
        )
        await session.commit()

    return {
        "deployment_id": str(deployment_id),
        "apply_change_set_id": str(submission.change_set_id),
        "apply_outcome": submission.outcome,
        "stop": submission.outcome not in {"applying", "applied"},
    }


@register_task(STEP_VERIFY)
async def verify_deployment(payload: dict[str, Any]) -> dict[str, Any]:
    """Read the deployment row back and report what it says.

    READ BACK, NOT ASSUMED. This step exists because the apply step's return value is the chokepoint's
    opinion that a command was delivered, and the question the pipeline is asked is whether the workloads
    are running. The row is written by `DeploymentSettler` from the agent's own report.

    `healthy: null` IS REPORTED AS UNVERIFIED AND NOT AS FAILURE. A report silent about workloads leaves
    the column null, and absence of health is not health — nor is it ill health.
    """
    deployment_id = payload.get("deployment_id")
    if not deployment_id:
        # Reached when the apply step stopped the pipeline. Not an error: there is genuinely nothing to
        # verify, and saying so is better than raising about a missing key.
        return {"verified": False, "verdict": "no deployment was created, so there is nothing to verify"}

    async with _context.sessions() as session:
        row = (
            (
                await session.execute(
                    text("SELECT status, healthy, stable FROM deployments WHERE id = :id"),
                    {"id": str(deployment_id)},
                )
            )
            .mappings()
            .first()
        )

    if row is None:
        return {"verified": False, "verdict": f"deployment {deployment_id} is not in the database"}

    healthy = row["healthy"]
    if healthy is None:
        verdict = (
            f"the deployment is {row['status']} and no workload health has been reported, so whether it "
            "is serving is unverified"
        )
    elif healthy:
        verdict = f"the deployment is {row['status']} and every workload became ready"
    else:
        verdict = f"the deployment is {row['status']} and at least one workload did not become ready"

    return {
        "verified": healthy is True,
        "deployment_status": str(row["status"]),
        "deployment_healthy": healthy,
        "deployment_stable": row["stable"],
        "verdict": verdict,
    }


#: The pipeline. Registered at import, so a module that never imports this file never gets the workflow —
#: which is what keeps `registered_workflows()` an honest statement of what this build can run.
DEPLOYMENT_PIPELINE = register_workflow(
    WorkflowDefinition(
        name=PIPELINE_NAME,
        steps=(
            WorkflowStep(name=STEP_BUILD),
            WorkflowStep(name=STEP_PUSH),
            # THE ONE GATE. See the module docstring for why it is here and not on the cheap steps.
            WorkflowStep(name=STEP_APPLY, requires_approval=True),
            WorkflowStep(name=STEP_VERIFY),
        ),
    )
)


async def _transit_image(payload: dict[str, Any], *, action: str, image: str, extra: dict[str, Any]) -> dict[str, Any]:
    """Run one image action through the chokepoint and report the submission flatly.

    A DICT RATHER THAN THE `Submission` OBJECT, because this return value is serialised into the durable
    run's step output. An engine stores step results as JSON, so anything that is not JSON-representable
    fails at the boundary with a serialisation error rather than where the mistake is.
    """
    from src.governance.chokepoint import DOCKER_IMAGE_OPERATION

    project_id = uuid.UUID(_required(payload, "project_id"))
    async with _context.sessions() as session:
        principal = await _context.principal_for(session, uuid.UUID(_required(payload, "requested_by")))
        submission = await _context.chokepoint.transit_host_action(
            session,
            project_id=project_id,
            principal=principal,
            operation=DOCKER_IMAGE_OPERATION,
            target=image,
            args={"action": action, "image": image, **extra},
            environment_name=None,
            environment_requires_approval=False,
            reason=f"durable pipeline: {action} {image}",
        )
        await session.commit()
    return {"change_set_id": str(submission.change_set_id), "outcome": submission.outcome}


def _build_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if payload.get("build_context"):
        extra["build_context"] = str(payload["build_context"])
    if payload.get("dockerfile"):
        extra["dockerfile"] = str(payload["dockerfile"])
    if isinstance(payload.get("build_args"), dict):
        extra["build_args"] = {str(k): str(v) for k, v in payload["build_args"].items()}
    return extra


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or not str(value).strip():
        raise ValueError(
            f"the deployment pipeline needs {key!r} in its payload and it is missing; "
            "a run that guessed one would act on the wrong project"
        )
    return str(value)


def _json(value: Any) -> str:
    import json

    return json.dumps(value)
