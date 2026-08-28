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
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ..core.model_port import ArtifactModelPort
from ..core.sse import SSEEventType, format_event
from ..secrets.redaction import create_redacted_prompt
from .model_prompt import (
    ArtifactParseError,
    build_generation_prompt,
    facts_from_project,
    parse_artifacts,
)
from .models import MAX_GENERATION_ITERATIONS


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


def _deployment_yaml(app_name: str, port: int) -> str:
    """The Deployment. `replicas: 1` because nothing here knows the intended scale."""
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
            "      containers:",
            "        - name: app",
            f"          image: {app_name}:latest",
            "          ports:",
            f"            - containerPort: {port}",
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
                run_id=run_id, prompt=prompt, project=project, outcome=outcome, report=report
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
        ):
            yield frame

    # ── the provider path ────────────────────────────────────────────────────

    async def _stream_from_model(
        self,
        *,
        run_id: uuid.UUID,
        prompt: str,
        project: Mapping[str, Any] | None,
        outcome: GenerationOutcome | None,
        report: _ModelReport,
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

            model_prompt = build_generation_prompt(
                operator_prompt=prompt, facts=facts, previous_findings=findings, attempt=attempt
            )
            # D-44: the cache key AND the L2 vector are computed over this value, and it is the
            # only thing handed to the provider. Redacting here rather than inside the port keeps
            # the guarantee at the boundary where raw operator text last exists.
            redacted = create_redacted_prompt(model_prompt)

            deltas: asyncio.Queue[str | None] = asyncio.Queue()

            async def _sink(text: str, queue: asyncio.Queue[str | None] = deltas) -> None:
                await queue.put(text)

            task = asyncio.create_task(self._model.complete(prompt=redacted, on_token=_sink))
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
                    break
            finally:
                if not task.done():
                    task.cancel()

            result = await task
            if not result.ok or not result.content:
                # The port reports a transport fault and an exhausted cascade the same way, in the
                # words the next prompt can quote. Both are repairable by a retry; neither is a
                # reason to fail the run while the template fallback is still available.
                findings = result.failure_reasons
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
                parsed = parse_artifacts(result.content)
            except ArtifactParseError as exc:
                findings = (str(exc),)
                continue

            files = tuple(GeneratedFile(path=path, content=content) for path, content in parsed.items())
            passed, gate_findings = self._validate(files)
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
                findings = gate_findings
                continue

            if outcome is not None:
                outcome.files = list(files)
                outcome.prompt_tokens = (result.usage or {}).get("prompt_tokens", 0) or max(
                    1, len(model_prompt.split())
                )
                outcome.completion_tokens = (result.usage or {}).get("completion_tokens", 0) or token_count
                outcome.validation_passed = True
                outcome.status = "accepted"
                outcome.served_from = served_from
                outcome.tier = self._model.tier_name
                outcome.endpoint_id = result.endpoint_id
                # A cache hit consumed no provider attempt, and recording one would inflate the
                # NFR-04 iteration average the column exists to measure.
                outcome.iterations_used = 0 if served_from in {"l1", "l2"} else attempt

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
        passed, findings = self._validate(files)
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

        # Configured values win over the runtime default, because an operator who recorded a port
        # knows something this function cannot derive.
        if str(settings.get("base_image") or "").strip():
            base = str(settings["base_image"]).strip()
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

        dockerfile = "\n".join(
            [
                f"FROM {base}",
                "WORKDIR /app",
                "COPY . .",
                f"RUN {install}",
                f"EXPOSE {port}",
                "USER 1001",
                f"CMD {start!r}".replace("'", '"'),
                "",
            ]
        )
        return (
            GeneratedFile(path="Dockerfile", content=dockerfile),
            GeneratedFile(path="k8s/deployment.yaml", content=_deployment_yaml(app_name, port)),
            GeneratedFile(path="k8s/service.yaml", content=_service_yaml(app_name, port)),
            GeneratedFile(path="k8s/ingress.yaml", content=_ingress_yaml(app_name, port)),
        )

    def _chunks(self, content: str, size: int = 120) -> list[str]:
        """Split rendered content into token-ish frames.

        Not real tokenisation, and named so. The point of streaming here is that a client receives
        progressive output; claiming these are model tokens would be a fiction the `served_from`
        column already contradicts.
        """
        return [content[i : i + size] for i in range(0, len(content), size)] or [""]

    def _validate(self, files: Sequence[GeneratedFile]) -> tuple[bool, tuple[str, ...]]:
        """?11.5.5's deterministic gate: structural checks that either hold or do not.

        Deliberately not a quality score. Each check below is something a malformed artifact fails
        outright, so a refusal is explicable rather than a threshold judgement.
        """
        findings: list[str] = []
        by_path = {artifact.path: artifact.content for artifact in files}

        dockerfile = by_path.get("Dockerfile", "")
        if not dockerfile.startswith("FROM "):
            findings.append("Dockerfile does not begin with a FROM instruction")
        if "USER " not in dockerfile:
            # A container that runs as root is the finding the readiness rubric also flags, and it
            # is deterministic, so it belongs in the blocking gate rather than the advisory score.
            findings.append("Dockerfile does not drop root with a USER instruction")

        manifest = by_path.get("k8s/deployment.yaml", "")
        for required in ("apiVersion:", "kind:", "metadata:", "spec:"):
            if required not in manifest:
                findings.append(f"Kubernetes manifest is missing {required}")

        return (not findings), tuple(findings)
