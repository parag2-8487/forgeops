# SPDX-License-Identifier: FSL-1.1-ALv2
"""Generation streaming (design.md ?1.5, ?7.4, ?11.5; Leaf 13.8).

**What changed and why.** This service emitted three event names ? `run_start`, `token_chunk`,
`run_complete` ? by hand-formatting SSE frames. None of the three is in `SSEEventType`, which ?7.4
fixes at six values and says explicitly that "no divergent names may be invented". Nothing caught it
because the vocabulary was an enum nobody had to go through and this was the only producer, so the
enum described a contract the one implementation broke.

The failure that would have caused is quiet in the worst way: a browser `EventSource` with a
listener on `token` never fires for `token_chunk`. No error, no warning ? an apparently empty
stream. Frames now go through `core.sse.format_event`, which takes the enum rather than a string, so
a divergent name is a `TypeError` at the call site.

The stream shape is ?12.6 step 7's: `status`, then one `token` per chunk, then `validation`
carrying the deterministic gate's verdict, then `complete`. `error` replaces `complete` when the
pipeline fails, and is a terminal frame ? a stream that ended with neither is a stream a client
cannot distinguish from a dropped connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ..core.content_regression import regression_findings
from ..core.manifest_facts import dockerfile_base_pinned
from ..core.model_port import ArtifactModelPort
from ..core.sse import SSEEventType, format_event
from ..core.target_checks import score_lowering_findings, unsatisfied_targets
from ..secrets.redaction import RedactedPrompt, create_redacted_prompt
from .artifact_checks import validate_artifacts
from .iac_renderers import (
    GENERATED_IMAGE_TAG,
    GENERATED_RUN_AS_USER,
    github_workflow_yaml,
    helm_chart_ignore,
    helm_chart_yaml,
    helm_deployment_template,
    helm_helpers_template,
    helm_values_yaml,
    opentofu_main_tf,
)
from .model_prompt import (
    REQUIRED_ARTIFACTS,
    ArtifactParseError,
    build_generation_prompt,
    facts_from_project,
    parse_artifacts,
)
from .models import MAX_GENERATION_ITERATIONS
from .project_hygiene_renderers import (
    compose_file,
    dockerignore,
    env_example,
    lint_config,
    secret_scanner_config,
    security_policy,
)
from .prompt_compiler import CompiledPrompt
from .retrieval import RetrievalContext, render_context_section


class RunRecord(BaseModel):
    run_id: uuid.UUID
    project_id: uuid.UUID
    status: str
    token_usage: int


@dataclass(frozen=True, slots=True)
class GeneratedFile:
    """One artifact the pipeline produced, ready to become a `change_items` row.

    `path` and `content` are what a change set needs, so generation hands governance exactly the
    shape it consumes rather than a blob a caller has to re-parse. That is what lets
    `change_sets.origin = 'generation'` and `change_sets.generation_run_id` mean something.
    """

    path: str
    content: str


@dataclass(slots=True)
class GenerationOutcome:
    """What a completed stream produced, for the caller that has to persist it.

    Separate from the stream itself because an async generator's return value is awkward to reach:
    the route needs the artifacts AFTER the last frame, and reading them off a mutable outcome the
    generator fills is clearer than threading a queue or re-running the pipeline.
    """

    run_id: uuid.UUID
    files: list[GeneratedFile] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    validation_passed: bool = False
    status: str = "running"
    #: One of `models.SERVED_FROM`. `pending` until the run resolves, because a row whose status
    #: is `running` has not been served from anywhere yet and the previous code's answer to that
    #: was the SQL literal `'template'` — a claim about the pipeline made before it ran.
    served_from: str = "pending"
    #: The `ModelTier` that produced this, or `template` when nothing did. Was the SQL literal
    #: `'deterministic'`, which is not a tier.
    tier: str = "template"
    #: Which endpoint answered, for the NFR-04 evidence `generation_runs.endpoint_id` exists for.
    endpoint_id: str | None = None
    #: ONE RECORD PER MODEL ATTEMPT, so a run that served templates can say which attempt failed and why.
    #:
    #: Built because three investigations this cycle needed it and had to rebuild it from a live stream that
    #: no longer existed. The cause it exposed was a 300s client budget against attempts measured at
    #: 155-817s: every one was cut off mid-flight and recorded as a refusal. A truncated attempt and a
    #: refused artifact look identical in a row that does not carry the duration.
    attempts: list[dict[str, object]] = field(default_factory=list)
    #: FR-13's retrieval record: which retrievers ran, how many chunks reached the prompt, and their
    #: paths. Persisted to `generation_runs.retrieval`, a column that has existed since revision `0006`
    #: and was never written.
    #:
    #: `None` means retrieval was never ATTEMPTED, which is a different fact from an empty result — a
    #: project that has been scanned and yielded nothing relevant is not the same as one that was never
    #: searched. That is why this is not defaulted to an empty dict.
    retrieval: dict[str, Any] | None = None
    #: Provider attempts consumed, bounded by §3.8's three. `0` for a cache hit or a run with no
    #: router configured, which is the truth in both cases: neither called a provider.
    iterations_used: int = 0


def _kubernetes_name(raw: str) -> str:
    """Turn a project name into a valid RFC 1123 label, or return empty when it cannot.

    Kubernetes rejects a name with capitals, underscores or a leading digit, and the manifests are
    validated by the pipeline ? so an invalid name would fail the run rather than deploy badly. An
    empty return means "no usable name", and the caller keeps its documented default instead of
    emitting something Kubernetes will refuse.
    """
    lowered = "".join(character if character.isalnum() else "-" for character in raw.strip().lower())
    trimmed = "-".join(part for part in lowered.split("-") if part)[:63].strip("-")
    if not trimmed or not trimmed[0].isalpha():
        # A label must start with a letter. Prefixing rather than discarding keeps a numeric project
        # name identifiable instead of silently becoming the generic default.
        trimmed = f"app-{trimmed}".strip("-")[:63] if trimmed else ""
    return trimmed


def _deployment_yaml(app_name: str, port: int, image_tag: str = GENERATED_IMAGE_TAG) -> str:
    """The Deployment. `replicas: 1` because nothing here knows the intended scale.

    EVERY PROPERTY BELOW IS SCORED, AND THAT IS WHY IT IS HERE. This template used to emit
    `image: {app_name}:latest` with no `resources` and no probes, and the readiness engine scores
    exactly those three things — `kubernetes_image_tags_pinned` (20), `kubernetes_probes_declared` (25)
    and `kubernetes_resource_limits_declared` (35). So a run whose instruction said "probes are
    missing" fell back to this template, applied it, and the operator's score did not move. The
    fallback is the FLOOR: an artifact that fails the check it was generated to fix is worse than no
    artifact, because it consumes the change set and reports success.

    THE NUMBERS ARE A STARTING POINT AND SAY SO IN THE FILE. An earlier version of the Helm values
    left `resources: {}` with a comment arguing that a guessed request is worse than none. That
    reasoning is defensible in isolation and wrong here: `kubernetes_resource_limits_declared` is a
    check this platform scores the user against, so shipping an artifact that fails it hands the user a
    defect and then bills them for it. A modest, clearly-labelled default that the operator will tune
    is the honest resolution — and unlike an absent block, it cannot evict a neighbour on day one.

    The probe path is `/` rather than `/healthz`: nothing here knows that the application serves a
    dedicated health route, and a probe pointed at a 404 would restart a healthy container forever.
    `/` is the one path a web application can be assumed to answer.
    """
    return "\n".join(
        [
            "apiVersion: apps/v1",
            "kind: Deployment",
            "metadata:",
            f"  name: {app_name}",
            "  labels:",
            f"    app: {app_name}",
            "spec:",
            "  replicas: 1",
            "  selector:",
            "    matchLabels:",
            f"      app: {app_name}",
            "  template:",
            "    metadata:",
            "      labels:",
            f"        app: {app_name}",
            "    spec:",
            "      securityContext:",
            "        runAsNonRoot: true",
            f"        runAsUser: {GENERATED_RUN_AS_USER}",
            "      containers:",
            "        - name: app",
            # An explicit tag, not `latest`: two applies of one manifest must deploy the same code.
            f"          image: {app_name}:{image_tag}",
            "          ports:",
            f"            - containerPort: {port}",
            "          # Requests are what the scheduler reserves; limits are what stops this container",
            "          # taking the node. Tune both to the workload's measured usage.",
            "          resources:",
            "            requests:",
            '              cpu: "100m"',
            '              memory: "128Mi"',
            "            limits:",
            '              cpu: "500m"',
            '              memory: "512Mi"',
            "          # Without probes a wedged container keeps receiving traffic, because nothing asks it.",
            "          livenessProbe:",
            "            httpGet:",
            "              path: /",
            f"              port: {port}",
            "            initialDelaySeconds: 10",
            "            periodSeconds: 20",
            "          readinessProbe:",
            "            httpGet:",
            "              path: /",
            f"              port: {port}",
            "            initialDelaySeconds: 5",
            "            periodSeconds: 10",
            "          securityContext:",
            "            allowPrivilegeEscalation: false",
            "            readOnlyRootFilesystem: true",
            "            capabilities:",
            "              drop:",
            '                - "ALL"',
            "",
        ]
    )


def _service_yaml(app_name: str, port: int) -> str:
    """The Service, without which nothing can reach the pod.

    `ClusterIP` and not `LoadBalancer`: a Service type that provisions cloud infrastructure is a
    decision with a bill attached, and nothing here knows whether this cluster should expose it that
    way. The Ingress below is the deliberate entry point.
    """
    return "\n".join(
        [
            "apiVersion: v1",
            "kind: Service",
            "metadata:",
            f"  name: {app_name}",
            "  labels:",
            f"    app: {app_name}",
            "spec:",
            "  type: ClusterIP",
            "  selector:",
            f"    app: {app_name}",
            "  ports:",
            "    - name: http",
            "      port: 80",
            f"      targetPort: {port}",
            "      protocol: TCP",
            "",
        ]
    )


def _ingress_yaml(app_name: str, port: int) -> str:
    """The Ingress. The host is derived from the application name, not invented from a real domain.

    `.local` deliberately: it is reserved for local resolution, so a manifest applied by accident
    cannot claim a name that belongs to somebody else. An operator who has a real hostname edits one
    line, which is a better default than a plausible-looking domain nobody owns.
    """
    del port  # The Ingress addresses the Service's port 80, not the container's.
    return "\n".join(
        [
            "apiVersion: networking.k8s.io/v1",
            "kind: Ingress",
            "metadata:",
            f"  name: {app_name}",
            "  labels:",
            f"    app: {app_name}",
            "spec:",
            "  rules:",
            f"    - host: {app_name}.local",
            "      http:",
            "        paths:",
            "          - path: /",
            "            pathType: Prefix",
            "            backend:",
            "              service:",
            f"                name: {app_name}",
            "                port:",
            "                  number: 80",
            "",
        ]
    )


@dataclass(slots=True)
class _ModelReport:
    """How the provider path finished, for the generator that cannot return a value.

    `succeeded` is what decides whether the template runs. Reading it off a shared object is the
    same technique `GenerationOutcome` uses one level up, and for the same reason: the caller needs
    a fact the generator learns after its last frame.
    """

    succeeded: bool = False
    findings: tuple[str, ...] = ()


class GenerationService:
    """Streams a generation run and reports what it produced.

    THE ORDER, AND WHY IT IS THIS ORDER
    -----------------------------------
    cache -> provider -> fallback cascade -> safe template, and the template is reached ONLY
    after the provider path has genuinely been tried and failed up to §3.8's three times.

    That ordering is the whole change. `stream_generation` used to call `self._render` directly
    and this docstring used to say "the pipeline behind this is still the Phase 1 template path
    ... not a live model call", while `routes.py` INSERTed `served_from` as the SQL string literal
    `'template'` — not a bound parameter — so the column could not have recorded a provider call
    even if one had happened. The template library was not the fallback; it was the only path, and
    the schema was shaped so that nobody could tell from a row.

    THE MODEL ARRIVES AS `core.model_port.ArtifactModelPort`, NOT AS `ModelRouter`
    -----------------------------------------------------------------------------
    `src/generation/` may not import `src.ai` (§2.2.1), and that ban is re-asserted by parsing in
    `scripts/chokepoint_graph.py` rather than by a lint, so it cannot be silenced. The first
    version of this wiring imported `ai.routing.router` and the parse check refused it. `src/ai`
    implements the port in `ai/generation_port.py`; this module names only the seam, which is also
    what would let generation be extracted with `core/model_port.py` and no knowledge of routing.

    The port is INJECTED rather than constructed here for a second reason: the router behind it
    owns the cache and the per-endpoint breakers, and it is composed once in the lifespan. A
    service that built its own would get a private set of breakers whose state nothing else could
    see, so a tripped endpoint would keep being retried by generation after `/api/v1/ai/complete`
    had given up on it.

    `model=None` IS A SUPPORTED CONFIGURATION, NOT A TEST SEAM
    A deployment with no reachable endpoint runs the template path and records `served_from`
    `template` with status `template_fallback`, which is an honest row. It is also what keeps
    `GenerationService()` constructible with no arguments, which Q-26 and
    `test_generation_service.py` rely on — those properties are about SSE framing and must not
    need a model server to run.
    """

    def __init__(
        self,
        *,
        model: ArtifactModelPort | None = None,
        max_attempts: int = MAX_GENERATION_ITERATIONS,
    ) -> None:
        self._model = model
        if max_attempts < 1 or max_attempts > MAX_GENERATION_ITERATIONS:
            # §3.8's bound is expressed in the type (`Literal[3]`), in the schema
            # (`iterations_used BETWEEN 0 AND 3`) and in Q-08. This is the fourth place it could
            # be broken, so it refuses rather than letting a caller write an unstorable row.
            raise ValueError(f"max_attempts must be between 1 and {MAX_GENERATION_ITERATIONS}, got {max_attempts}")
        self._max_attempts = max_attempts

    @property
    def routes_to_a_model(self) -> bool:
        """Whether this service has a provider path at all.

        Read by `routes.py` to decide the `tier` it records on the `running` row, so the row says
        what was ATTEMPTED rather than what a previous phase happened to hard-code.
        """
        return self._model is not None

    @property
    def attempted_tier(self) -> str:
        """The tier this run will ask for, for the `running` row's `tier` column.

        `template` when there is no provider path, which is a true statement about what will
        produce the artifacts. The column previously carried the SQL literal `'deterministic'`,
        which is not a `ModelTier` and told a reader nothing about routing.
        """
        return self._model.tier_name if self._model is not None else "template"

    async def stream_generation(
        self,
        project_id: uuid.UUID,
        prompt: str,
        *,
        outcome: GenerationOutcome | None = None,
        project: Mapping[str, Any] | None = None,
        retrieval: RetrievalContext | None = None,
        compiled: CompiledPrompt | None = None,
        existing: Mapping[str, str] | None = None,
        repair_prompts: Mapping[str, CompiledPrompt] | None = None,
    ) -> AsyncGenerator[str]:
        """Yield §7.4 frames for one generation run.

        `outcome` is filled as the run proceeds so the caller can persist the artifacts and the
        token counts after the stream closes. It is optional so the service stays usable — and
        testable — without one.

        `project` is the `projects` row, so the rendered artifacts describe the REAL application
        rather than a fixed `forgeops-app`. Optional for the same reason as `outcome`: the service
        must remain constructible in a test that is not about project facts, and `_render`
        documents what it falls back to.
        """
        run_id = outcome.run_id if outcome is not None else uuid.uuid4()

        yield format_event(
            SSEEventType.STATUS,
            {"run_id": str(run_id), "project_id": str(project_id), "state": "running"},
        )

        provider_findings: tuple[str, ...] = ()
        if self.routes_to_a_model:
            report = _ModelReport()
            async for frame in self._stream_from_model(
                run_id=run_id,
                prompt=prompt,
                project=project,
                outcome=outcome,
                report=report,
                retrieval=retrieval,
                compiled=compiled,
                repair_prompts=repair_prompts,
                existing=existing,
            ):
                yield frame
            if report.succeeded:
                # The provider path emitted its own terminal frame. §7.4 permits exactly one.
                return
            provider_findings = report.findings
        # Falling through means either no router is configured or every provider attempt failed.
        # Either way the template is now a genuine fallback rather than the only path, and the row
        # will say `template`.
        async for frame in self._stream_from_template(
            run_id=run_id,
            prompt=prompt,
            project=project,
            outcome=outcome,
            provider_findings=provider_findings,
            existing=existing,
        ):
            yield frame

    # ── the provider path ────────────────────────────────────────────────────

    async def _stream_from_model(
        self,
        *,
        run_id: uuid.UUID,
        prompt: str,
        project: Mapping[str, Any] | None,
        existing: Mapping[str, str] | None,
        outcome: GenerationOutcome | None,
        report: _ModelReport,
        retrieval: RetrievalContext | None = None,
        compiled: CompiledPrompt | None = None,
        repair_prompts: Mapping[str, CompiledPrompt] | None = None,
    ) -> AsyncGenerator[str]:
        """Route through the cascade, streaming real deltas, up to `max_attempts` times.

        The verdict goes on `report` rather than being raised or returned. An async generator
        cannot return a value a caller can read, and an exception would strand a stream that has
        already emitted frames with no terminal event — the one outcome a client cannot distinguish
        from a dropped connection.
        """
        assert self._model is not None
        app_name = _kubernetes_name(str((project or {}).get("name") or "")) or "forgeops-app"
        facts = facts_from_project(project=project, operator_prompt=prompt, default_app_name=app_name)

        findings: tuple[str, ...] = ()
        # Assigned on attempt 1 and used after acceptance; see `remember` below for why the retry
        # prompt must not be the key.
        first_attempt_prompt: RedactedPrompt | None = None
        # Bound HERE rather than only on the gate-failure path, which is where it used to live. The
        # provenance decision below reads it on every successful attempt including the ones that never
        # reached the floor, and leaving it unbound raised UnboundLocalError on the ordinary happy path --
        # caught immediately by `test_generation_routing.py`, which is what those tests are for.
        substituted: tuple[str, ...] = ()
        # ARTIFACTS THAT ALREADY PASSED, kept across attempts so a repair does not have to reproduce them.
        #
        # Before this, a rejected artifact made the whole set be asked for again. MEASURED on the real
        # model: asked for six files with 55 requirements, 7b produced an unpinned `node:$NODE_VERSION`
        # base image and REPRODUCED IT on both corrections; asked for the Dockerfile alone with its own 24
        # requirements, it made the same mistake once and fixed it on the first correction. Every other
        # requirement was satisfied in both asks, so this is not capability -- what a wide ask costs is the
        # model's ability to act on a correction while regenerating five unrelated files.
        carried: dict[str, GeneratedFile] = {}
        # The artifact a repair attempt is for, or None for the opening whole-plan ask.
        repair_for: str | None = None
        asked_for_order: tuple[str, ...] = ()
        # EVERY PATH THE GATE EVER REJECTED IN THIS RUN. A later attempt can pass while one of them is
        # simply absent, and an absent artifact produces no finding -- so without this the run would
        # report success on a set quietly missing a file. Measured on the criterion-10 fixture: a
        # Deployment was rejected on attempt 1, the repair went to the Dockerfile, and the run was
        # accepted delivering a Service and an Ingress with no Deployment -- the exact gap
        # `ARTIFACT_COMPANIONS` exists to prevent, reintroduced by narrowing the repair.
        ever_rejected: set[str] = set()
        for attempt in range(1, self._max_attempts + 1):
            yield format_event(
                SSEEventType.PROGRESS,
                {
                    "run_id": str(run_id),
                    "state": "requesting_model",
                    "attempt": attempt,
                    "max_attempts": self._max_attempts,
                    "tier": self._model.tier_name,
                },
            )

            # THE COMPILED INSTRUCTION WINS when one was built. It already carries the facts, the
            # per-path create-or-modify decision, the acceptance criteria and the prohibitions, all
            # derived from the index — so wrapping it in the older template would restate some of it
            # and contradict the rest.
            #
            # `build_generation_prompt` remains the path for a free-text prompt with no readiness
            # findings behind it, which is still a legitimate request.
            if compiled is not None:
                # THE OPENING ASK IS THE WHOLE PLAN; A REPAIR IS ONE ARTIFACT.
                #
                # The first call is unchanged deliberately: it is what produces most of the set in one
                # round, it is the cache key of the run, and nothing measured argues against it. Only the
                # REPAIR is narrowed, because that is where the measurement shows the loss.
                active = compiled
                if repair_for is not None and repair_prompts is not None:
                    narrow = repair_prompts.get(repair_for)
                    if narrow is not None:
                        active = narrow
                model_prompt = active.text
                if findings:
                    # THE VALIDATOR'S OWN WORDS, fed back verbatim. A repair attempt that is told only
                    # "it failed" has to guess what to change, and guessing is what produced the
                    # failure. The findings are appended rather than merged into the instruction so the
                    # model can see which of its own output is being objected to.
                    model_prompt = (
                        model_prompt
                        + "\n## 6. WHAT THE VALIDATORS SAID ABOUT YOUR PREVIOUS ATTEMPT\n\n"
                        + "Your last output was rejected. Fix exactly these and change nothing else:\n\n"
                        + "\n".join(f"  - {finding}" for finding in findings)
                        + "\n"
                    )
            else:
                model_prompt = build_generation_prompt(
                    operator_prompt=prompt,
                    facts=facts,
                    previous_findings=findings,
                    attempt=attempt,
                    # FR-13. Rendered per attempt from the SAME retrieval, so the repair loop keeps the
                    # grounding it started with rather than searching again for rows that cannot have
                    # changed between attempts. `render_context_section` returns an empty tuple when there
                    # is nothing to say, so an unscanned project gets no heading rather than an empty one.
                    context_lines=render_context_section(retrieval) if retrieval is not None else (),
                )
            # D-44: the cache key AND the L2 vector are computed over this value, and it is the
            # only thing handed to the provider. Redacting here rather than inside the port keeps
            # the guarantee at the boundary where raw operator text last exists.
            redacted = create_redacted_prompt(model_prompt)
            if attempt == 1:
                # THE CACHE KEY OF THE WHOLE RUN, kept because a later attempt's prompt is not one any
                # future run will present: it carries the previous attempt's gate findings appended to
                # it. Caching the accepted content under that prompt writes an entry whose key nobody
                # will ever ask for again -- a silent permanent miss, which looks exactly like a cache
                # that is working and never hitting.
                first_attempt_prompt = redacted

            attempt_started = time.monotonic()
            deltas: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)

            async def _sink(text: str, queue: asyncio.Queue[str | None] = deltas) -> None:
                await queue.put(text)

            # ONLY THE FIRST ATTEMPT MAY BE SERVED FROM CACHE. A later attempt exists because the gate
            # rejected what the earlier one produced, and that rejected output is what the cache now
            # holds -- so a hit would re-deliver the artifact that just failed, guaranteeing the loop
            # cannot converge, and would record `served_from='l1'` with `iterations_used=0` for a run
            # that plainly iterated.
            task = asyncio.create_task(
                self._model.complete(
                    prompt=redacted,
                    on_token=_sink,
                    may_serve_from_cache=attempt == 1,
                    # NOT CACHED ON PRODUCTION. What the model produced is not yet what this run will
                    # deliver: the validation gate runs below and may reject all of it. Caching here
                    # would poison the entry -- every later run with this prompt served the known-bad
                    # artifact, failing the gate again and burning its attempt budget re-delivering
                    # it. `remember` is called after acceptance instead.
                    store_in_cache=False,
                )
            )
            # The frames are emitted from the QUEUE rather than from the task's result, which is
            # what makes them real: each one leaves this process as soon as the provider produced
            # it. Draining after the call returned would be the 120-character slicing this
            # replaced, wearing a different name.
            token_count = 0
            try:
                while True:
                    drain = asyncio.ensure_future(deltas.get())
                    done, _ = await asyncio.wait({drain, task}, return_when=asyncio.FIRST_COMPLETED)
                    if drain in done:
                        token_count += 1
                        yield format_event(
                            SSEEventType.TOKEN,
                            {"run_id": str(run_id), "text": drain.result(), "attempt": attempt},
                        )
                        await asyncio.sleep(0.005)
                        continue
                    # The call finished. Anything already queued is still owed to the client.
                    drain.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await drain
                    while not deltas.empty():
                        text = deltas.get_nowait()
                        if text is None:
                            continue
                        token_count += 1
                        yield format_event(
                            SSEEventType.TOKEN,
                            {"run_id": str(run_id), "text": text, "attempt": attempt},
                        )
                        await asyncio.sleep(0.005)
                    break
            finally:
                if not task.done():
                    task.cancel()

            result = await task
            attempt_seconds = round(time.monotonic() - attempt_started, 3)

            def _record_attempt(
                verdict: str,
                reason: str = "",
                *,
                _n: int = attempt,
                _seconds: float = attempt_seconds,
                _repairing: str | None = repair_for,
            ) -> None:
                """One row of evidence per attempt.

                `seconds` is the point of it. A refusal recorded at 300.0s against a 300s client budget is a
                TRUNCATION rather than a verdict, and without the duration beside the reason that is
                unrecoverable once the request has ended -- which is how a truncating timeout went unseen
                through three investigations.
                """
                if outcome is not None:
                    attempts_list: list[dict[str, Any]] = outcome.attempts
                    attempts_list.append(
                        {
                            "attempt": _n,
                            "seconds": _seconds,
                            "verdict": verdict,
                            "reason": reason[:400],
                            "repairing": _repairing or "",
                        }
                    )

            if not result.ok or not result.content:
                # The port reports a transport fault and an exhausted cascade the same way, in the
                # words the next prompt can quote. Both are repairable by a retry; neither is a
                # reason to fail the run while the template fallback is still available.
                findings = result.failure_reasons
                _record_attempt("no_content", "; ".join(findings))
                continue

            served_from = result.served_from
            if token_count == 0:
                # A cache hit delivers no deltas by design (see `ModelRouter.complete`), so the
                # content is replayed here. `replayed: true` is on the payload so a client can tell
                # the difference; these are real model tokens produced by an earlier call, and
                # nothing here re-chunks them into invented units — the split is on line
                # boundaries, which are units the artifact itself has.
                for line in result.content.splitlines(keepends=True):
                    token_count += 1
                    yield format_event(
                        SSEEventType.TOKEN,
                        {"run_id": str(run_id), "text": line, "attempt": attempt, "replayed": True},
                    )

            try:
                # WHAT THIS RUN ASKED FOR, not a fixed list of four paths.
                #
                # `parse_artifacts` defaulted to `REQUIRED_ARTIFACTS` — Dockerfile plus three
                # Kubernetes manifests — so a run asked for a CI workflow failed the parse for missing a
                # Dockerfile nobody had requested, and a run asked only to fix a `USER` directive failed
                # for missing an ingress. That default is why the product could produce four kinds of
                # file and no others: the parser, not the model and not the validators, was the limit.
                #
                # The validators were never the constraint. `artifact_checks.checker_for` already
                # dispatches on path for Dockerfile, compose, Chart.yaml, `.github/workflows/`, `k8s/`
                # and `*.tf`, and returns None for a kind it has no opinion about rather than refusing
                # it.
                #
                # PASSED AS `requested`, NOT `required`, AND THE DIFFERENCE IS A REGRESSION I CAUSED.
                # Treating a compiled plan's dozen write targets as all-or-nothing meant one omission
                # discarded eleven correct files and substituted canned template output — the
                # thirteen-step journey recorded a run that produced most of what it asked for as
                # `template_fallback`. A shortfall is now a shortfall: the files that came back are
                # used, the checks whose files did not are simply still failing, and the score says so
                # honestly. Canned output in place of real partial work is worse than the partial work.
                #
                # Falls back to the old default when no plan was compiled, so an operator typing a free
                # prompt still gets the previous contract rather than a run that requires nothing and
                # therefore accepts an empty answer.
                # AGAINST WHAT THIS CALL ASKED FOR, which on a repair is one artifact rather than six.
                # Parsing a repair answer against the whole plan's targets would report five missing files
                # the model was deliberately not asked for.
                if compiled is not None and compiled.write_targets:
                    asked_for = tuple(
                        active.write_targets if active is not None and active.write_targets else compiled.write_targets
                    )
                    parsed = parse_artifacts(result.content, required=(), requested=asked_for)
                    # The PLAN's order, not this call's: a repair asks for one file and must not
                    # reorder the set around it.
                    asked_for_order = tuple(compiled.write_targets)
                else:
                    parsed = parse_artifacts(result.content, required=REQUIRED_ARTIFACTS)
                    asked_for_order = tuple(REQUIRED_ARTIFACTS)
            except ArtifactParseError as exc:
                findings = (str(exc),)
                _record_attempt("parse_failed", str(exc))
                continue

            # WHAT THIS CALL PRODUCED, PLUS WHAT EARLIER CALLS ALREADY GOT RIGHT. The new answer wins for
            # a path it covers, so a repair replaces the artifact it was asked to repair and nothing else.
            merged = dict(carried)
            for path, content in parsed.items():
                merged[path] = GeneratedFile(path=path, content=content)
            files = tuple(merged.values())
            passed, gate_findings = self._validate(files, existing)
            yield format_event(
                SSEEventType.VALIDATION,
                {
                    "run_id": str(run_id),
                    "passed": passed,
                    "findings": list(gate_findings),
                    "served_from": served_from,
                    "attempt": attempt,
                },
            )
            if not passed:
                _record_attempt("gate_refused", "; ".join(gate_findings))
                # RETRY WHILE THERE IS AN ATTEMPT LEFT, because the model can usually repair what the
                # gate named and a fully valid set is the better outcome.
                if attempt < self._max_attempts:
                    # KEEP WHAT PASSED, AND ASK AGAIN FOR ONE THING.
                    #
                    # The artifacts the gate did not name are correct; re-asking for them invites the model
                    # to change them and, measured, stops it from fixing the one that is wrong. So they are
                    # carried forward and the next call is narrowed to a single rejected artifact with only
                    # that artifact's requirements.
                    rejected_paths = [
                        artifact.path
                        for artifact in files
                        if any(finding.startswith(f"{artifact.path}: ") for finding in gate_findings)
                    ]
                    # An artifact the model was asked for and did not produce at all is also a repair
                    # target: it is missing rather than wrong, and a narrow ask is the better second try.
                    missing = [
                        path
                        for path in (compiled.write_targets if compiled is not None else ())
                        if path not in {artifact.path for artifact in files}
                    ]
                    ever_rejected.update(rejected_paths)
                    ever_rejected.update(missing)
                    for artifact in files:
                        if artifact.path not in rejected_paths:
                            carried[artifact.path] = artifact
                    # HIGHEST STAKES FIRST when several failed, because the attempt budget is finite and a
                    # Dockerfile carries more of the score than a `.gitleaks.toml`. `repair_prompts` is
                    # keyed by write target, so a path with no repair prompt is one no artifact instruction
                    # claims and there is nothing narrower to ask.
                    candidates = [
                        path
                        for path in rejected_paths + missing
                        if repair_prompts is not None and path in repair_prompts
                    ]
                    repair_for = candidates[0] if candidates else None
                    if repair_for is not None:
                        # ONLY THIS ARTIFACT'S FINDINGS. Handing over five other files' complaints is the
                        # wide ask again, in the one place it does the most harm.
                        narrowed = tuple(finding for finding in gate_findings if finding.startswith(f"{repair_for}: "))
                        findings = narrowed or gate_findings
                    else:
                        findings = gate_findings
                    continue

                # LAST ATTEMPT. Keep the artifacts that PASSED rather than discarding them because a
                # sibling failed, and this is a strengthening rather than a relaxation: an artifact the
                # gate rejected is still never accepted — §11.5.5's gate stays blocking, per file.
                #
                # What changes is the alternative. Discarding the whole set fell through to the template
                # path, which writes canned files addressing none of the user's specific findings. So a
                # run that produced nine correct artifacts and one malformed one delivered zero correct
                # ones. The thirteen-step journey caught exactly this, twice: first as a parse failure
                # over the requested set, then here as a gate failure over it.
                accepted = tuple(
                    artifact
                    for artifact in files
                    if not any(finding.startswith(f"{artifact.path}: ") for finding in gate_findings)
                )
                # THE FLOOR IS PER ARTIFACT, NOT PER RUN — AND THAT IS THE DEFECT THE JOURNEY CAUGHT.
                #
                # Everything below `if not accepted` reaches the template path. So the floor applied
                # only when the model produced NOTHING usable, and the case it was designed for is the
                # opposite one: the model returns nine good artifacts and a Dockerfile with
                # `FROM node:latest`. The bad Dockerfile was withheld, correctly, and nothing put the
                # known-good one in its place — the change set went out with no Dockerfile at all,
                # which is what step 8 found. `check-template-readiness.py` had been proving all along
                # that the floor satisfies its target checks; no runtime path consulted it per kind.
                #
                # THIS WEAKENS NOTHING. The model's failing artifact is still never accepted. The
                # substitute is the template the CI audit gate certifies, and the assembled set is
                # re-validated through the SAME `_validate` — including the regression and
                # score-lowering rules — so a substitution that would make the set worse is rejected
                # and the withholding stands. A floor that cannot satisfy the check is not used.
                substituted = ()
                # ONLY WHEN SOMETHING OF THE MODEL'S SURVIVED, and the reason is provenance.
                #
                # If NOTHING passed, substituting the floor for every artifact would deliver a set that is
                # entirely template content while the run row said `served_from='provider'`. That is the
                # same class of wrongness as the `served_from` defects already recorded in PROGRESS.md: a
                # row reporting a provenance the run did not have. The template path below already handles
                # the nothing-survived case correctly and records `template`, so it is left to do so.
                #
                # Caught by `test_the_template_is_still_reached_when_no_artifact_passes`, which asserted
                # `served_from == 'template'` and got `'provider'`. The fix for a mixed result had
                # silently changed the answer for a total failure.
                if accepted:
                    accepted, substituted = self._apply_floor(
                        accepted=accepted,
                        rejected=tuple(artifact.path for artifact in files if artifact not in accepted),
                        prompt=prompt,
                        project=project,
                        existing=existing,
                    )
                if not accepted:
                    findings = gate_findings
                    continue
                withheld = len(files) - len(accepted)
                files = accepted
                yield format_event(
                    SSEEventType.VALIDATION,
                    {
                        "run_id": str(run_id),
                        "passed": True,
                        "findings": [
                            f"delivering {len(accepted)} artifact(s) that passed the gate; "
                            f"{withheld} rejected artifact(s) were withheld and are not in the "
                            "change set",
                            *(
                                [
                                    "the audited template floor replaced "
                                    f"{len(substituted)} rejected artifact(s): "
                                    f"{', '.join(substituted)}"
                                ]
                                if substituted
                                else []
                            ),
                            *gate_findings,
                        ],
                        "served_from": served_from,
                        "attempt": attempt,
                    },
                )

            # THE FLOOR COVERS WHAT WAS REJECTED AND NEVER REPAIRED.
            #
            # The attempt budget is finite, so when several artifacts fail only some get a narrow repair.
            # The rest must be SUBSTITUTED, not dropped: the audited template is the known-good answer for
            # exactly this case, and `check-template-readiness.py` proves it satisfies its target checks.
            #
            # This weakens nothing. The model's rejected artifact is still never accepted, and the
            # assembled set goes back through the SAME `_validate` -- including the regression and
            # score-lowering rules -- so a substitution that would make the set worse is refused and the
            # withholding stands.
            outstanding = tuple(
                path
                for path in asked_for_order
                if path in ever_rejected and path not in {artifact.path for artifact in files}
            )
            if outstanding:
                filled, also_substituted = self._apply_floor(
                    accepted=files,
                    rejected=outstanding,
                    prompt=prompt,
                    project=project,
                    existing=existing,
                )
                recheck_passed, recheck_findings = self._validate(filled, existing)
                if recheck_passed:
                    files = filled
                    substituted = tuple(sorted(set(substituted) | set(also_substituted)))
                    yield format_event(
                        SSEEventType.VALIDATION,
                        {
                            "run_id": str(run_id),
                            "passed": True,
                            "findings": [],
                            "substituted": list(substituted),
                            "served_from": served_from,
                            "attempt": attempt,
                        },
                    )
                else:
                    # The floor could not satisfy the checks either, so the withholding stands and the
                    # shortfall is reported rather than papered over.
                    report.findings = recheck_findings

            # DELIVERED IN THE ORDER THE RUN ASKED FOR, applied where the set is FINAL -- after both the
            # repair and the floor have had their say. Order is a property of the PLAN, not of the
            # model's emission order, not of which attempt produced what, and not of the floor's
            # iteration order. It reaches `change_items.ordinal`, so before this the diff a reviewer
            # reads was sequenced by something nobody chose and `ORDER BY ordinal` gave different
            # answers for identical inputs. A path the plan did not name sorts after the named ones by
            # its own name rather than being dropped.
            delivery_order = {path: i for i, path in enumerate(asked_for_order)}
            files = tuple(
                sorted(
                    files,
                    key=lambda f: (delivery_order.get(f.path, len(delivery_order)), f.path),
                )
            )

            if outcome is not None:
                outcome.files = list(files)
                outcome.prompt_tokens = (result.usage or {}).get("prompt_tokens", 0) or max(
                    1, len(model_prompt.split())
                )
                outcome.completion_tokens = (result.usage or {}).get("completion_tokens", 0) or token_count
                outcome.validation_passed = True
                outcome.status = "accepted"
                # WHERE THE DELIVERED ARTIFACTS ACTUALLY CAME FROM, which is not always where the
                # completion came from. When the gate rejected everything the model produced and the
                # audited template floor supplied every artifact being delivered, the provenance is
                # `template` -- recording the model's `served_from` would put a row in the table
                # claiming a provider (or a cache) produced files it did not produce. That is the same
                # class of defect as the `served_from` failures already recorded in PROGRESS.md, and it
                # is reachable on any prompt whose checks the configured model cannot satisfy: with
                # `qwen2.5-coder:1.5b` and a Dockerfile needing a HEALTHCHECK, every attempt fails the
                # gate and the floor delivers all four artifacts, yet the row said `provider`.
                #
                # A PARTIAL substitution stays with the model's provenance: some delivered artifact did
                # come from it, and calling the whole run `template` would understate what the model did.
                delivered = {artifact.path for artifact in files}
                outcome.served_from = "template" if delivered and delivered <= set(substituted) else served_from
                outcome.tier = self._model.tier_name
                outcome.endpoint_id = result.endpoint_id
                # A cache hit consumed no provider attempt, and recording one would inflate the
                # NFR-04 iteration average the column exists to measure.
                outcome.iterations_used = 0 if served_from in {"l1", "l2"} else attempt

            # THE GATE HAS PASSED, so this answer is fit to keep. Cached here rather than in the router
            # because only this line knows the artifacts survived validation.
            #
            # AND ONLY IF THE MODEL'S OWN OUTPUT PASSED, which is what `not substituted` adds.
            #
            # MEASURED, end to end, on the real model. A run took three attempts, was delivered with the
            # template floor standing in for the artifacts the gate refused, and correctly recorded
            # `served_from='provider'` because the substitution was partial. It then cached THE RAW MODEL
            # TEXT -- the text the gate had just refused. The next run with that prompt hit the entry
            # (`LOOKUP -> L1_exact`, observed), had the content refused by the same gate for the same
            # reason, burned its first attempt, and went to the provider anyway: `served_from='provider'`,
            # `iterations_used=3`, for a run that began with a cache hit.
            #
            # That is precisely the POISONED ENTRY the `store_in_cache=False` comment in the router
            # describes, arriving by the one route that guard does not cover. It is strictly worse than
            # not caching: the run pays the lookup, receives a known-bad answer, spends an attempt
            # rediscovering that it is bad, and still calls the provider. An entry that can never serve
            # is indistinguishable from a cache that is working and never hitting -- which is how this
            # survived: the hit rate was zero and every run looked correct.
            #
            # `substituted` is the floor's own record of which artifacts it supplied. Non-empty means the
            # delivered set is model output PLUS template files, and the raw completion alone is not the
            # thing that was accepted -- so it is not the thing to keep.
            #
            # Skipped when the content came FROM the cache: rewriting an entry with itself is wasted work,
            # and it would refresh the TTL on every read, so a hot entry could never expire -- which is how
            # a cache comes to serve an answer from a model version that is no longer configured.
            if served_from == "provider" and result.content and first_attempt_prompt is not None and not substituted:
                await self._model.remember(
                    prompt=first_attempt_prompt,
                    content=result.content,
                )

            _record_attempt("accepted")
            report.succeeded = True
            yield format_event(
                SSEEventType.COMPLETE,
                {
                    "run_id": str(run_id),
                    "state": "accepted",
                    "files": [artifact.path for artifact in files],
                    "completion_tokens": token_count,
                    "served_from": served_from,
                    "endpoint_id": result.endpoint_id,
                },
            )
            return

        # Every attempt failed. The template below is now reached for a stated reason.
        report.findings = findings or ("the provider path produced no usable artifacts",)

    # ── the template path, unchanged in behaviour and now genuinely a fallback ──

    async def _stream_from_template(
        self,
        *,
        run_id: uuid.UUID,
        prompt: str,
        project: Mapping[str, Any] | None,
        existing: Mapping[str, str] | None,
        outcome: GenerationOutcome | None,
        provider_findings: tuple[str, ...],
    ) -> AsyncGenerator[str]:
        """The deterministic renderer, reached when no model produced usable artifacts."""
        try:
            files = self._render(prompt, project)
        except Exception as exc:  # noqa: BLE001 - reported to the client as a terminal frame
            if outcome is not None:
                outcome.status = "failed"
                outcome.served_from = "template"
            # Terminal, and carrying the reason. A stream that just stopped would be
            # indistinguishable from a dropped connection.
            yield format_event(
                SSEEventType.ERROR,
                {"run_id": str(run_id), "detail": str(exc), "state": "failed"},
            )
            return

        completion_tokens = 0
        for artifact in files:
            for chunk in self._chunks(artifact.content):
                completion_tokens += 1
                yield format_event(
                    SSEEventType.TOKEN,
                    {"run_id": str(run_id), "path": artifact.path, "text": chunk},
                )

        # §11.5.5's deterministic gate is the blocking one; the rubric is advisory and is not
        # consulted here, deliberately, so a low rubric score cannot fail a run.
        passed, findings = self._validate(files, existing)
        yield format_event(
            SSEEventType.VALIDATION,
            {
                "run_id": str(run_id),
                "passed": passed,
                "findings": list(findings),
                "served_from": "template",
            },
        )

        if outcome is not None:
            outcome.files = list(files)
            outcome.prompt_tokens = max(1, len(prompt.split()))
            outcome.completion_tokens = completion_tokens
            outcome.validation_passed = passed
            outcome.served_from = "template"
            outcome.tier = "template"
            # `template_fallback` when a model was tried and could not deliver, `accepted` when no
            # provider path was configured at all. The two are different facts about a run and the
            # status vocabulary already distinguishes them; collapsing both to `accepted` would
            # hide every provider outage.
            if passed:
                outcome.status = "template_fallback" if provider_findings else "accepted"
            else:
                outcome.status = "failed"
            outcome.iterations_used = 0

        if not passed:
            yield format_event(
                SSEEventType.ERROR,
                {
                    "run_id": str(run_id),
                    "detail": "the deterministic validation gate refused the artifacts",
                    "state": "failed",
                },
            )
            return

        yield format_event(
            SSEEventType.COMPLETE,
            {
                "run_id": str(run_id),
                "state": "accepted",
                "files": [artifact.path for artifact in files],
                "completion_tokens": completion_tokens,
                "served_from": "template",
                # Present only when a provider was actually tried, so a client can tell a
                # deliberate template deployment from a degraded one.
                **({"provider_findings": list(provider_findings)} if provider_findings else {}),
            },
        )

    # ?? the pipeline, kept small and honest ??????????????????????????????????

    def _render(self, prompt: str, project: Mapping[str, Any] | None = None) -> tuple[GeneratedFile, ...]:
        """Render the four artifacts ?12.6 step 6 asks for, from what is actually known.

        WHAT CHANGED AND WHY

        Two artifacts were produced, not four. ?12.6 names a Dockerfile and Kubernetes manifests, and
        the journey's own list is `Dockerfile`, `k8s/deployment.yaml`, `k8s/service.yaml`,
        `k8s/ingress.yaml`. A Deployment with no Service is not a deployable manifest set ? nothing
        can reach the pod ? so the missing two were a functional gap and not a formatting one.

        And the resource name was the literal `forgeops-app` for every project, while the runtime was
        chosen by looking for the substring "node" in the operator's PROMPT. A prompt is a request,
        not a fact about the repository: the same project generates different infrastructure
        depending on how somebody phrased a sentence, and two projects generate colliding Kubernetes
        names. Both are hardcoded answers standing in for reading the project.

        WHAT IT READS NOW, AND THE LIMIT, STATED

        The `projects` row: its `name` (so the manifests name the real application) and its
        `settings` (so an operator who has recorded a runtime, a port or a start command gets those).
        When `settings` says nothing, the prompt is used as an explicit, LAST-resort hint rather than
        the primary signal, and `served_from` on the persisted run still records `template` so the row
        never claims a model produced this.

        THE LIMIT, CORRECTED. This docstring used to say the codebase index could not be read
        "because scanning the repository is group 11's analysis work and `file_tree` is empty until it
        lands". That has not been true for some time: an agent scan populates `file_tree` and
        `file_contents`, `GET /projects/{id}/readiness` scores from exactly those rows, and the
        project detail screen reports their contents. The statement was a description of a constraint
        that had since been removed, which is worse than no statement — a reader would conclude the
        limitation was structural.

        So the real limit is a narrower and more honest one: this TEMPLATE path reads the project row
        and does not consult the index, even though the index is now there. That is a choice about
        the fallback rather than a missing capability — the model path takes the index into account
        through retrieval, and the template library exists to produce something defensible when no
        model could be reached, where a partial index would make the output less predictable rather
        than more accurate. Widening it to read `file_tree` is a change to the fallback's contract and
        belongs with a decision about what a template may infer, not with a docstring.
        """
        settings: Mapping[str, Any] = {}
        project_name = ""
        if project is not None:
            raw_settings = project.get("settings")
            if isinstance(raw_settings, Mapping):
                settings = raw_settings
            project_name = str(project.get("name") or "")

        app_name = _kubernetes_name(project_name) or "forgeops-app"

        runtime = str(settings.get("runtime") or "").strip().lower()
        if not runtime:
            # The operator's words, used only because nothing about the project says otherwise.
            lowered = prompt.lower()
            runtime = "node" if ("node" in lowered or "express" in lowered) else "python"

        if runtime.startswith("node"):
            base, start, port = "node:20-alpine", ["node", "server.js"], 3000
            install = "npm ci --omit=dev"
        else:
            base, start, port = "python:3.11-slim", ["python", "main.py"], 8000
            install = "pip install --no-cache-dir -r requirements.txt"

        # THE FLOOR KNOWS TWO RUNTIMES, AND SAYS SO WHEN IT IS ASKED FOR A THIRD.
        #
        # Everything that is not Node takes the Python branch, so a project whose settings record
        # `runtime: go` was handed a Python Dockerfile with a `pip wheel` line and no indication that
        # its recorded runtime had been ignored. The artifact is well formed and passes every target
        # check, which is what makes it dangerous: nothing in the output or the run row contradicted it,
        # and the operator would find out from a failed build. Naming the substitution in the file is
        # the cheap honest fix; teaching the floor more runtimes is a template-library task, and
        # `template_library.py` already holds Go, Rust, Java, Ruby, PHP and .NET content that no
        # runtime path reads (recorded in PROGRESS.md).
        unsupported_runtime = ""
        if runtime and not runtime.startswith(("node", "python", "py")):
            unsupported_runtime = runtime

        # Configured values win over the runtime default, because an operator who recorded a port
        # knows something this function cannot derive.
        # AN OPERATOR PREFERENCE MAY NOT BREAK THE FLOOR'S CONTRACT.
        #
        # This read `settings["base_image"]` straight into `FROM`. A project whose settings say
        # `node` or `node:latest` therefore rendered a Dockerfile that fails
        # `dockerfile_base_pinned` — the very check the artifact is generated to satisfy — so the
        # per-file gate withheld the floor itself and the operator got no Dockerfile. The audit gate
        # never caught it because it only ever supplied a name and a port: production has an input
        # the gate had no case for.
        #
        # The pinned runtime default is used instead, and the rejected value is named in a comment in
        # the file so the substitution is visible in the diff the operator approves. Silently
        # honouring it would ship a known-failing artifact; silently dropping it would be the kind of
        # invisible override this codebase keeps having to dig out.
        rejected_base = ""
        if str(settings.get("base_image") or "").strip():
            configured_base = str(settings["base_image"]).strip()
            if dockerfile_base_pinned(f"FROM {configured_base}"):
                base = configured_base
            else:
                rejected_base = configured_base
        with contextlib.suppress(TypeError, ValueError):
            if settings.get("port") is not None:
                configured = int(settings["port"])
                if 1 <= configured <= 65535:
                    port = configured
        configured_start = settings.get("start_command")
        if isinstance(configured_start, list | tuple) and configured_start:
            start = [str(part) for part in configured_start]
        elif isinstance(configured_start, str) and configured_start.strip():
            start = configured_start.split()

        # MULTI-STAGE AND HEALTHCHECK ARE BOTH SCORED, AND NEITHER WAS PRESENT.
        #
        # `dockerfile_multi_stage` (25) counts `FROM` lines and wants at least two;
        # `dockerfile_healthcheck_present` (15) wants a `HEALTHCHECK` instruction. This renderer emitted
        # a single stage and no healthcheck, so the artifact the platform offered as the fix for those
        # checks failed them both.
        #
        # The split is real, not cosmetic: dependencies are installed in the builder and only their
        # product is copied forward, so the compilers and package caches never reach the shipped image.
        #
        # The healthcheck uses the runtime's own interpreter rather than `curl`, which a slim base image
        # does not carry — a HEALTHCHECK calling a missing binary reports unhealthy forever, which is
        # worse than none because an orchestrator will kill a working container.
        if runtime.startswith("node"):
            builder = [
                f"FROM {base} AS builder",
                "WORKDIR /app",
                "COPY package*.json ./",
                f"RUN {install}",
            ]
            copy_forward = ["COPY --from=builder /app/node_modules /app/node_modules", "COPY . ."]
            health = (
                "HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \\\n"
                f"  CMD node -e \"fetch('http://127.0.0.1:{port}/')"
                '.then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"'
            )
        else:
            builder = [
                f"FROM {base} AS builder",
                "WORKDIR /app",
                "COPY requirements.txt ./",
                # Wheels are built once here and installed in the runtime stage, so neither pip's cache
                # nor any build toolchain survives into the image that ships.
                "RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt",
            ]
            copy_forward = [
                "COPY --from=builder /wheels /wheels",
                "RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/* && rm -rf /wheels",
                "COPY . .",
            ]
            health = (
                "HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \\\n"
                f'  CMD python -c "import urllib.request,sys; '
                f"sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=2)"
                '.status < 400 else 1)"'
            )

        dockerfile = "\n".join(
            [
                *(
                    [
                        f"# NOTE: the configured runtime {unsupported_runtime!r} is not one this",
                        "# fallback renders. The Python layout below is what was produced; it is not a",
                        f"# {unsupported_runtime} image and will not build one.",
                    ]
                    if unsupported_runtime
                    else []
                ),
                *(
                    [
                        f"# NOTE: the configured base image {rejected_base!r} is not pinned to an exact",
                        "# version or digest, so it is not used here: it would fail",
                        "# `dockerfile_base_pinned`, the check this file exists to satisfy. Record a",
                        f"# pinned image in the project's settings to override {base!r}.",
                    ]
                    if rejected_base
                    else []
                ),
                *builder,
                "",
                f"FROM {base}",
                "WORKDIR /app",
                *copy_forward,
                f"EXPOSE {port}",
                health,
                f"USER {GENERATED_RUN_AS_USER}",
                f"CMD {start!r}".replace("'", '"'),
                "",
            ]
        )
        # The linter configuration is chosen by runtime, so the file is one the project's own linter
        # loads rather than a name that merely satisfies the path check.
        lint_path, lint_body = lint_config(runtime)

        return (
            GeneratedFile(path="Dockerfile", content=dockerfile),
            GeneratedFile(path="k8s/deployment.yaml", content=_deployment_yaml(app_name, port)),
            GeneratedFile(path="k8s/service.yaml", content=_service_yaml(app_name, port)),
            GeneratedFile(path="k8s/ingress.yaml", content=_ingress_yaml(app_name, port)),
            # FR-24's other two halves. A Dockerfile and manifests with nothing to build the image and
            # nothing to create the cluster is not a deployable project, and the agent's
            # `validate.yaml`, `validate.helm` and `validate.tofu` operations had nothing to check.
            GeneratedFile(
                path=".github/workflows/build.yml",
                # The runtime is threaded through so the workflow tests the language the image contains.
                content=github_workflow_yaml(app_name, runtime="node" if runtime.startswith("node") else "python"),
            ),
            GeneratedFile(path=f"charts/{app_name}/Chart.yaml", content=helm_chart_yaml(app_name)),
            # Declares what is not chart content, so a stray file beside a template cannot make
            # `helm lint` reject a chart this platform just wrote.
            GeneratedFile(path=f"charts/{app_name}/.helmignore", content=helm_chart_ignore()),
            GeneratedFile(path=f"charts/{app_name}/values.yaml", content=helm_values_yaml(app_name, port)),
            GeneratedFile(
                path=f"charts/{app_name}/templates/_helpers.tpl",
                content=helm_helpers_template(app_name),
            ),
            GeneratedFile(
                path=f"charts/{app_name}/templates/deployment.yaml",
                content=helm_deployment_template(app_name),
            ),
            GeneratedFile(path="infra/main.tf", content=opentofu_main_tf(app_name, port)),
            # The six kinds `GENERATED_ARTIFACT_KINDS` declared generatable and nothing rendered. The
            # readiness screen was offering these as fixes — `env_example_present` alone is 40 points —
            # while no code path produced them, so the offer could never be fulfilled.
            GeneratedFile(path=".dockerignore", content=dockerignore(runtime)),
            GeneratedFile(path=".env.example", content=env_example(app_name, port)),
            GeneratedFile(path="SECURITY.md", content=security_policy(app_name)),
            GeneratedFile(path=".gitleaks.toml", content=secret_scanner_config()),
            GeneratedFile(path=lint_path, content=lint_body),
            GeneratedFile(
                path="docker-compose.yml",
                content=compose_file(app_name, port, GENERATED_IMAGE_TAG),
            ),
        )

    def _chunks(self, content: str, size: int = 120) -> list[str]:
        """Split rendered content into token-ish frames.

        Not real tokenisation, and named so. The point of streaming here is that a client receives
        progressive output; claiming these are model tokens would be a fiction the `served_from`
        column already contradicts.
        """
        return [content[i : i + size] for i in range(0, len(content), size)] or [""]

    def _apply_floor(
        self,
        *,
        accepted: Sequence[GeneratedFile],
        rejected: Sequence[str],
        prompt: str,
        project: Mapping[str, Any] | None,
        existing: Mapping[str, str] | None,
    ) -> tuple[tuple[GeneratedFile, ...], tuple[str, ...]]:
        """Replace each rejected artifact with the audited template, when the template is better.

        Returns the set to deliver and the paths that were substituted.

        WHY THIS IS A SUBSTITUTION AND NOT A RELAXATION. The rejected artifact is gone either way; the
        only question is whether the operator gets the known-good template in its place or nothing. The
        template is the one `scripts/check-template-readiness.py` certifies against every target check,
        over every input production can supply — so "nothing" was strictly the worse of the two, and it
        was what the product did.

        THREE CONDITIONS, ALL NECESSARY:

        1. The template must actually render that path. A run that wrote something the floor has no
           opinion about gets no substitute, because inventing one would be fabricating an artifact.
        2. The substitute must satisfy its OWN target checks. Verified here at runtime rather than
           trusted from CI, because the inputs differ per project and a gate that passed on other bytes
           says nothing about these.
        3. The assembled set must pass the WHOLE gate — structure, regression against what exists, and
           the no-lowering rule. A substitution that costs points elsewhere is not an improvement, and
           this is the check that notices.

        If any fails, the withholding stands and the finding already names it.
        """
        if not rejected:
            return tuple(accepted), ()

        floor = {item.path: item.content for item in self._render(prompt, project)}
        candidates: dict[str, str] = {}
        for path in rejected:
            body = floor.get(path)
            if body is None:
                continue
            # Condition 2, asked of the substitute ALONE, so a sibling cannot carry it.
            if unsatisfied_targets({path: body}, existing):
                continue
            candidates[path] = body
        if not candidates:
            return tuple(accepted), ()

        assembled = tuple(accepted) + tuple(
            GeneratedFile(path=path, content=body) for path, body in sorted(candidates.items())
        )
        # Condition 3. `_validate` is the same function the model output answered to.
        passed, _ = self._validate(assembled, existing)
        if not passed:
            return tuple(accepted), ()
        return assembled, tuple(sorted(candidates))

    def _validate(
        self, files: Sequence[GeneratedFile], existing: Mapping[str, str] | None = None
    ) -> tuple[bool, tuple[str, ...]]:
        """§11.5.5's deterministic gate: structural checks that either hold or do not.

        Deliberately not a quality score. Each check is something a malformed artifact fails outright,
        so a refusal is explicable rather than a threshold judgement.

        THE CHECKS PARSE THE DOCUMENT NOW. This method used to test for substrings — `"apiVersion:" not
        in manifest`, `dockerfile.startswith("FROM ")`, `"USER " not in dockerfile`. That accepts a
        manifest whose `apiVersion` is inside a comment, a Dockerfile whose `USER` appears in an
        environment value, a document whose indentation is broken so the whole file is one scalar, and
        anything that is not YAML at all: every one of those contains the right words. It also accepted
        an object with no `metadata.name`, which no cluster will take. So the gate could only fail an
        artifact that failed to mention something.

        `artifact_checks` parses each document and checks its shape, and it covers the artifact kinds
        FR-24 requires generating rather than only the two that existed: Dockerfile, Compose, Kubernetes
        manifests, GitHub Actions workflows, `Chart.yaml` and OpenTofu configurations.

        This is not the agent's validation and does not replace it. FR-27's tool-based checks —
        `docker compose config`, `kubectl`, `helm`, `tofu`, `trivy` — run on the user's machine where
        the workspace and the daemons are, and none of those binaries is in the backend image. Two
        gates answering two questions: this one is "is the document well formed and the right shape",
        cheap and offline and actionable by a repair iteration; the agent's is "will the real tools
        accept it".
        """
        findings = list(validate_artifacts(files))
        # A SECOND QUESTION, ASKED IN THE SAME PLACE. `validate_artifacts` answers "is this document
        # well formed and the right shape"; a 345-byte Deployment with no probes, no resource limits
        # and `:latest` passes that and is still strictly worse than the 1301-byte one it replaces.
        # Findings share the `path: ` prefix, so D-106's per-file machinery handles them unchanged:
        # the model is told what it dropped while an attempt remains, and the artifact alone is
        # withheld if it will not put it back.
        findings.extend(regression_findings(files, existing))
        # A THIRD QUESTION, AND THE ONE THAT WAS ASKED OF NOTHING AT RUNTIME.
        #
        # `validate_artifacts` asks "is this well formed"; `regression_findings` asks "is this worse
        # than what it replaces". Neither asks "does this satisfy the check it was generated for", and a
        # real provider run exposed the gap: 296 completion tokens, an instruction naming
        # `kubernetes_probes_declared` with line numbers, and a returned Deployment with no probes that
        # passed this gate with zero findings and applied.
        #
        # The same rule the CI template gate uses, imported rather than restated so the two cannot
        # disagree about what satisfying a check means.
        findings.extend(unsatisfied_targets({item.path: item.content for item in files}, existing))
        # INVARIANT 2: no change set may lower the score. Checked over the SET rather than per file,
        # because a set can improve one artifact and cost points elsewhere without any single file
        # dropping a property of its own - which is exactly what the per-file guard above cannot see.
        findings.extend(score_lowering_findings({item.path: item.content for item in files}, existing))
        return (not findings), tuple(findings)
