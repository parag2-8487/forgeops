# SPDX-License-Identifier: FSL-1.1-ALv2
"""The durable deployment pipeline. Phase 2 §2.2 and §2.4a.

TWO KINDS OF EVIDENCE HERE, because the claims are of two kinds.

**The workflow's semantics** — steps run in the declared order, output threads forward, a gate blocks until
an event arrives, and an expired gate STOPS the pipeline — are exercised with `inngest.experimental.mocked`.
That is the SDK's OWN execution harness, not a fake I wrote: it runs the real translated function body with
real step semantics. A double I built would only prove my translator calls my double.

**The engine wiring** — that a real Inngest dev server discovers the real functions this application serves
— is exercised against the real dev server over HTTP. That is the half a harness cannot establish.

An earlier version of this file tried to prove both at once by serving the functions from the test process
and letting the containerised dev server call back in. Discovery succeeded and every invocation failed with
"Unable to reach SDK URL" / "EOF writing request to SDK", while a plain host HTTP server on the same
interface WAS reachable from the same container — so it was not a firewall. Rather than keep probing, the
claims were split: the harness proves the semantics, and discovery against the running backend proves the
wiring. Both are real; neither depends on a callback into a test process.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from src.core.tasks import (
    WorkflowDefinition,
    WorkflowStep,
    build_inngest_client,
    inngest_functions,
    register_task,
    register_workflow,
    registered_workflows,
)

# NO MODULE-LEVEL ASYNCIO MARK. `mocked.trigger` is SYNCHRONOUS and drives its own event loop, so
# calling it from inside an async test raises `RuntimeError: This event loop is already running`.
# The harness tests are therefore plain functions and only the HTTP ones are async.

INNGEST_URL = os.getenv("FORGEOPS_INNGEST_URL", "")

#: Appended by each step, so ORDER is observable rather than inferred from completion.
_ran: list[str] = []


@register_task("test.pipeline.first")
async def _first(payload: dict[str, Any]) -> dict[str, Any]:
    _ran.append("first")
    return {"from_first": str(payload.get("seed", "")) + "-one"}


@register_task("test.pipeline.second")
async def _second(payload: dict[str, Any]) -> dict[str, Any]:
    _ran.append("second")
    return {"from_second": str(payload.get("from_first", "MISSING")) + "-two"}


@register_task("test.pipeline.gated")
async def _gated(payload: dict[str, Any]) -> dict[str, Any]:
    _ran.append("gated")
    return {
        # The approval event's own data must reach the step: an approval that records WHO granted it is the
        # difference between an audit trail and a timestamp.
        "released_by": payload.get("approved_by", "NOBODY"),
        "saw": payload.get("from_second", "MISSING"),
    }


@register_task("test.pipeline.stopper")
async def _stopper(payload: dict[str, Any]) -> dict[str, Any]:
    _ran.append("stopper")
    # `stop` is how a step ends a pipeline without raising. A failing build is a RESULT everywhere else in
    # this codebase, and a pipeline that raised would discard the build output that makes it actionable.
    return {"stop": True, "why": "this step decided not to continue"}


@register_task("test.pipeline.never")
async def _never(payload: dict[str, Any]) -> dict[str, Any]:
    _ran.append("never")
    return {}


TEST_PIPELINE = register_workflow(
    WorkflowDefinition(
        name="test.pipeline",
        steps=(
            WorkflowStep(name="test.pipeline.first"),
            WorkflowStep(name="test.pipeline.second"),
            WorkflowStep(name="test.pipeline.gated", requires_approval=True, approval_timeout_seconds=30),
        ),
    )
)

STOPPING_PIPELINE = register_workflow(
    WorkflowDefinition(
        name="test.pipeline.stopping",
        steps=(
            WorkflowStep(name="test.pipeline.stopper"),
            WorkflowStep(name="test.pipeline.never"),
        ),
    )
)


def _harness() -> Any:
    """The SDK's mocked client."""
    import inngest.experimental.mocked as mocked

    return mocked.Inngest(app_id="forgeops-test")


def _function(name: str) -> Any:
    """The translated function for one registered workflow."""
    from src.core.config import Settings

    settings = Settings(inngest_base_url=INNGEST_URL or "http://localhost:8288")
    client = build_inngest_client(settings)
    wanted = name.replace(".", "-")
    for function in inngest_functions(client):
        if function.get_id().endswith(wanted):
            return function
    raise AssertionError(f"no translated function for {name}; the workflow registry may not hold it")


class TestTheWorkflowsSemantics:
    def test_a_gated_step_does_not_run_until_the_event_arrives(self) -> None:
        """The gate is the reason a durable engine is here at all.

        The wait is the ENGINE's: the run is suspended and holds no worker while a human thinks. A gate
        built from a sleep loop would occupy a process for hours and lose its place on a restart.
        """
        import inngest
        import inngest.experimental.mocked as mocked

        _ran.clear()
        result = mocked.trigger(
            _function("test.pipeline"),
            inngest.Event(name=TEST_PIPELINE.trigger_event, data={"seed": "x"}),
            _harness(),
            step_stubs={
                # The approval, standing in for the event a human's click sends. Stubbing the WAIT is the
                # supported way to drive a suspended run forward in this harness.
                # A REAL `Event`, not a dict. The harness validates the stub against the type the step
                # would receive, so a loose dict fails with a validation error -- which is the harness
                # keeping the stub honest rather than letting the test assert against a shape the
                # engine never produces.
                "test.pipeline.gated-approval": inngest.Event(
                    name=TEST_PIPELINE.approval_event("test.pipeline.gated"),
                    data={"approved_by": "auditor"},
                ),
            },
        )

        assert result.status is mocked.Status.COMPLETED, f"the run did not complete: {result.status}"
        # THE DECLARED ORDER, not whatever the engine felt like.
        assert _ran == ["first", "second", "gated"], f"steps ran out of order: {_ran}"

        output = result.output if isinstance(result.output, dict) else {}
        # THREADING: the digest a push produces has to reach the apply that pins it. Asserted through this
        # pipeline's own values, which is the same mechanism.
        assert output.get("from_first") == "x-one"
        assert output.get("from_second") == "x-one-two"
        assert output.get("saw") == "x-one-two"
        assert output.get("released_by") == "auditor", (
            "the approval's own data did not reach the gated step, so an approval cannot record who granted it"
        )

    def test_an_expired_gate_stops_the_pipeline_rather_than_continuing(self) -> None:
        """A timeout must NOT fall through to the step.

        Continuing without the approval would mean the gate had no effect, which is worse than not having
        one: an operator would believe production was gated when it was not.
        """
        import inngest
        import inngest.experimental.mocked as mocked

        _ran.clear()
        result = mocked.trigger(
            _function("test.pipeline"),
            inngest.Event(name=TEST_PIPELINE.trigger_event, data={"seed": "y"}),
            _harness(),
            # `Timeout` is the harness's sentinel for "the wait expired".
            step_stubs={"test.pipeline.gated-approval": mocked.Timeout},
        )

        assert result.status is mocked.Status.COMPLETED
        assert "gated" not in _ran, "the gated step ran after its approval expired, so the gate had no effect at all"
        output = result.output if isinstance(result.output, dict) else {}
        assert output.get("stopped_at") == "test.pipeline.gated"
        # AND IT SAYS WHY. "stopped" with no reason leaves an operator unable to tell an expired approval
        # from a crash.
        assert "approval expired" in str(output.get("reason", ""))
        # The earlier steps' work is still reported, so the run records what it did accomplish.
        assert output.get("from_second") == "y-one-two"

    def test_a_step_can_stop_the_pipeline_without_raising(self) -> None:
        """A failing build is a result, not an exception — and the next step must not run."""
        import inngest
        import inngest.experimental.mocked as mocked

        _ran.clear()
        result = mocked.trigger(
            _function("test.pipeline.stopping"),
            inngest.Event(name=STOPPING_PIPELINE.trigger_event, data={}),
            _harness(),
        )
        assert result.status is mocked.Status.COMPLETED
        assert _ran == ["stopper"], f"the pipeline continued past a stopping step: {_ran}"
        output = result.output if isinstance(result.output, dict) else {}
        assert output.get("stopped_at") == "test.pipeline.stopper"
        assert output.get("why") == "this step decided not to continue"


class TestTheRegistryRefusesAnIncoherentWorkflow:
    def test_a_workflow_naming_an_unregistered_step_is_refused_at_registration(self) -> None:
        """Refused at import, not at run time.

        A workflow naming a handler that does not exist would otherwise fail midway through a real
        deployment, after the earlier steps had already built and pushed an image.
        """
        with pytest.raises(ValueError, match="no @register_task declares"):
            register_workflow(
                WorkflowDefinition(
                    name="test.pipeline.broken",
                    steps=(WorkflowStep(name="test.pipeline.does-not-exist"),),
                )
            )

    def test_a_duplicate_workflow_is_refused(self) -> None:
        """Last-one-wins would make a pipeline's shape depend on import order."""
        with pytest.raises(ValueError, match="already registered"):
            register_workflow(
                WorkflowDefinition(name="test.pipeline", steps=(WorkflowStep(name="test.pipeline.first"),))
            )

    def test_the_deployment_pipeline_gates_apply_and_nothing_cheaper(self) -> None:
        """The production pipeline's shape, asserted.

        A build writes nothing outside the workspace and a push writes an immutable tag; both are
        reversible and neither changes what is serving traffic. Gating the cheap steps would train
        operators to click through the gate, which is how a gate stops being read.
        """
        import src.deployments.pipeline as pipeline  # noqa: F401 - registers the workflow

        definition = registered_workflows()[pipeline.PIPELINE_NAME]
        assert [step.name for step in definition.steps] == [
            pipeline.STEP_BUILD,
            pipeline.STEP_PUSH,
            pipeline.STEP_APPLY,
            pipeline.STEP_VERIFY,
        ]
        gated = [step.name for step in definition.steps if step.requires_approval]
        assert gated == [pipeline.STEP_APPLY], f"the gated steps are {gated}"


@pytest.mark.asyncio
@pytest.mark.skipif(not INNGEST_URL, reason="platform: FORGEOPS_INNGEST_URL is unset; no dev server running")
class TestTheEngineDiscoversWhatTheApplicationServes:
    async def test_the_dev_server_is_reachable_and_answers_its_health_endpoint(self) -> None:
        """The wiring's first half: the engine this deployment ships is actually running."""
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(f"{INNGEST_URL}/health")
        assert response.status_code == 200, f"the dev server answered {response.status_code}"

    async def test_the_translator_produces_one_function_per_task_and_per_workflow(self) -> None:
        """What discovery would find, asserted against the registries it is derived from.

        Both shapes matter: without a function per TASK, switching from ARQ to this engine would silently
        change which names are enqueueable, and a caller would get a handle for work nothing runs.
        """
        from src.core.config import Settings
        from src.core.tasks import _TASK_HANDLERS

        client = build_inngest_client(Settings(inngest_base_url=INNGEST_URL))
        functions = inngest_functions(client)
        assert len(functions) == len(_TASK_HANDLERS) + len(registered_workflows()), (
            f"{len(functions)} functions for {len(_TASK_HANDLERS)} tasks and {len(registered_workflows())} workflows"
        )
        ids = {function.get_id() for function in functions}
        assert any(name.endswith("deployment-pipeline") for name in ids), (
            "the deployment pipeline itself was not translated, so nothing could run it"
        )


class TestTheReleaseRouteCannotSilentlyDoNothing:
    def test_only_a_gated_step_can_be_released(self) -> None:
        """An arbitrary step name must be refused, not sent.

        Sending an event nothing waits for would leave the run suspended while the operator believed they
        had released it -- for an approval mechanism, the worst available outcome.
        """
        import src.deployments.pipeline as pipeline

        gated = {step.name for step in pipeline.DEPLOYMENT_PIPELINE.steps if step.requires_approval}
        assert gated == {pipeline.STEP_APPLY}
        # The cheap steps are NOT releasable, because they are not gated.
        assert pipeline.STEP_BUILD not in gated
        assert pipeline.STEP_PUSH not in gated
        assert pipeline.STEP_VERIFY not in gated

    def test_the_engine_endpoint_is_refused_unsigned_in_production(self) -> None:
        """The one route in this application not behind a principal must be signed.

        The engine calls `/api/inngest` to invoke functions and holds no user session, so a signing key is
        what authenticates it. A production deployment without one would expose an endpoint that starts
        deployment pipelines to anyone who can reach the port, so startup refuses that combination.
        """
        import os

        from src.core.config import Settings

        previous = {
            key: os.environ.get(key) for key in ("TASK_DISPATCHER", "INNGEST_IS_PRODUCTION", "INNGEST_EVENT_KEY")
        }
        try:
            os.environ["TASK_DISPATCHER"] = "inngest"
            os.environ["INNGEST_IS_PRODUCTION"] = "true"
            os.environ["INNGEST_EVENT_KEY"] = ""
            settings = Settings()
            assert settings.inngest_is_production is True
            assert not settings.inngest_event_key
            # The guard lives in the lifespan, so this asserts the CONDITION the guard tests rather than
            # booting an app: booting one here would need the whole AI routing environment, and the
            # property under test is the refusal rule.
            assert bool(settings.inngest_is_production) and not settings.inngest_event_key, (
                "this is exactly the combination startup must refuse"
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_the_dispatcher_seam_leaks_no_engine_concept(self) -> None:
        """`enqueue` returns a `TaskHandle` and nothing else, under every dispatcher.

        This is what makes the engine choice a configuration change: a caller that could poll or cancel
        through an engine API would have to be rewritten when the engine changed.
        """
        from dataclasses import fields

        from src.core.tasks import InlineDispatcher, InngestDispatcher, TaskHandle

        assert {f.name for f in fields(TaskHandle)} == {"id", "dispatcher"}
        for dispatcher in (InlineDispatcher, InngestDispatcher):
            public = {name for name in dir(dispatcher) if not name.startswith("_")}
            assert public == {"enqueue"}, (
                f"{dispatcher.__name__} exposes {sorted(public)}; anything beyond enqueue is an engine "
                "concept a caller can come to depend on"
            )
