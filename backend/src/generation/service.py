# SPDX-License-Identifier: FSL-1.1-ALv2
"""Generation streaming (design.md §1.5, §7.4, §11.5; Leaf 13.8).

**What changed and why.** This service emitted three event names — `run_start`, `token_chunk`,
`run_complete` — by hand-formatting SSE frames. None of the three is in `SSEEventType`, which §7.4
fixes at six values and says explicitly that "no divergent names may be invented". Nothing caught it
because the vocabulary was an enum nobody had to go through and this was the only producer, so the
enum described a contract the one implementation broke.

The failure that would have caused is quiet in the worst way: a browser `EventSource` with a
listener on `token` never fires for `token_chunk`. No error, no warning — an apparently empty
stream. Frames now go through `core.sse.format_event`, which takes the enum rather than a string, so
a divergent name is a `TypeError` at the call site.

The stream shape is §12.6 step 7's: `status`, then one `token` per chunk, then `validation`
carrying the deterministic gate's verdict, then `complete`. `error` replaces `complete` when the
pipeline fails, and is a terminal frame — a stream that ended with neither is a stream a client
cannot distinguish from a dropped connection.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field

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


class GenerationService:
    """Streams a generation run and reports what it produced.

    The pipeline behind this is still the Phase 1 template path — `template_library.py` and
    `renderers.py` — not a live model call. That is stated rather than hidden: `served_from` on the
    persisted `GenerationRun` records `template`, so the row says what produced it and nobody has
    to infer it from the absence of a provider.
    """

    async def stream_generation(
        self,
        project_id: uuid.UUID,
        prompt: str,
        *,
        outcome: GenerationOutcome | None = None,
    ) -> AsyncGenerator[str]:
        """Yield §7.4 frames for one generation run.

        `outcome` is filled as the run proceeds so the caller can persist the artifacts and the
        token counts after the stream closes. It is optional so the service stays usable — and
        testable — without one.
        """
        run_id = outcome.run_id if outcome is not None else uuid.uuid4()

        yield format_event(
            SSEEventType.STATUS,
            {"run_id": str(run_id), "project_id": str(project_id), "state": "running"},
        )

        try:
            files = self._render(prompt)
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

        # §11.5.5's deterministic gate is the blocking one; the rubric is advisory and is not
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

    # ── the pipeline, kept small and honest ──────────────────────────────────

    def _render(self, prompt: str) -> tuple[GeneratedFile, ...]:
        """Render the artifacts §12.6 step 6 asks for: a Dockerfile and a Kubernetes manifest.

        Templates rather than a model call, which is what Phase 1 has. The prompt selects the
        runtime; anything unrecognised falls back to the Python template rather than failing, since
        a fallback is what `served_from = 'template_fallback'` exists to record.
        """
        lowered = prompt.lower()
        if "node" in lowered or "express" in lowered:
            base, start, port = "node:20-alpine", ["node", "server.js"], 3000
            install = "npm ci --omit=dev"
        else:
            base, start, port = "python:3.11-slim", ["python", "main.py"], 8000
            install = "pip install --no-cache-dir -r requirements.txt"

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
        manifest = "\n".join(
            [
                "apiVersion: apps/v1",
                "kind: Deployment",
                "metadata:",
                "  name: forgeops-app",
                "spec:",
                "  replicas: 1",
                "  selector:",
                "    matchLabels:",
                "      app: forgeops-app",
                "  template:",
                "    metadata:",
                "      labels:",
                "        app: forgeops-app",
                "    spec:",
                "      containers:",
                "        - name: app",
                "          image: forgeops-app:latest",
                "          ports:",
                f"            - containerPort: {port}",
                "",
            ]
        )
        return (
            GeneratedFile(path="Dockerfile", content=dockerfile),
            GeneratedFile(path="k8s/deployment.yaml", content=manifest),
        )

    def _chunks(self, content: str, size: int = 120) -> list[str]:
        """Split rendered content into token-ish frames.

        Not real tokenisation, and named so. The point of streaming here is that a client receives
        progressive output; claiming these are model tokens would be a fiction the `served_from`
        column already contradicts.
        """
        return [content[i : i + size] for i in range(0, len(content), size)] or [""]

    def _validate(self, files: Sequence[GeneratedFile]) -> tuple[bool, tuple[str, ...]]:
        """§11.5.5's deterministic gate: structural checks that either hold or do not.

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
