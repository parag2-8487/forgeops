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

import contextlib
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ..core.sse import SSEEventType, format_event


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


class GenerationService:
    """Streams a generation run and reports what it produced.

    The pipeline behind this is still the Phase 1 template path ? `template_library.py` and
    `renderers.py` ? not a live model call. That is stated rather than hidden: `served_from` on the
    persisted `GenerationRun` records `template`, so the row says what produced it and nobody has
    to infer it from the absence of a provider.
    """

    async def stream_generation(
        self,
        project_id: uuid.UUID,
        prompt: str,
        *,
        outcome: GenerationOutcome | None = None,
        project: Mapping[str, Any] | None = None,
    ) -> AsyncGenerator[str]:
        """Yield ?7.4 frames for one generation run.

        `outcome` is filled as the run proceeds so the caller can persist the artifacts and the
        token counts after the stream closes. It is optional so the service stays usable ? and
        testable ? without one.

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

        try:
            files = self._render(prompt, project)
        except Exception as exc:  # noqa: BLE001 - reported to the client as a terminal frame
            if outcome is not None:
                outcome.status = "failed"
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

        # ?11.5.5's deterministic gate is the blocking one; the rubric is advisory and is not
        # consulted here, deliberately, so a low rubric score cannot fail a run.
        passed, findings = self._validate(files)
        yield format_event(
            SSEEventType.VALIDATION,
            {"run_id": str(run_id), "passed": passed, "findings": list(findings)},
        )

        if outcome is not None:
            outcome.files = list(files)
            outcome.prompt_tokens = max(1, len(prompt.split()))
            outcome.completion_tokens = completion_tokens
            outcome.validation_passed = passed
            outcome.status = "accepted" if passed else "failed"

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
        That is the same source `GET /projects/{id}/readiness` scores from, and the same limit
        applies ? there is no indexed file tree yet, because scanning the repository is group 11's
        analysis work and `file_tree` is empty until it lands. When `settings` says nothing, the
        prompt is used as an explicit, LAST-resort hint rather than the primary signal, and
        `served_from` on the persisted run still records `template` so the row never claims a model
        produced this.
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
