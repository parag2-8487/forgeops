# SPDX-License-Identifier: FSL-1.1-ALv2
"""The generation surface exists, is authenticated, and speaks §7.4's vocabulary.

`generation/` had twelve modules and no `routes.py`. The tests below pin three things: that the
endpoint is mounted and refuses anonymous callers, that the stream emits ONLY the six event names
§7.4 fixes — the previous service emitted three that were not among them — and that the frames
arrive in the order §12.6 step 7 asserts.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from src.core.sse import SSE_MEDIA_TYPE, SSEEventType, format_event
from src.generation.service import GeneratedFile, GenerationOutcome, GenerationService

from tests.integration.production_app import apply_committed_baseline_env

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

GENERATION_PATH = "/api/v1/generation/runs"


@pytest_asyncio.fixture
async def app_no_auth(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app()
    async with LifespanManager(app):
        yield app


def parse_frames(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """Split an SSE body into (event, payload) pairs, failing on a malformed frame.

    Written as a real parser rather than a substring search so a frame that is subtly wrong — a
    missing blank line, a split payload — fails the test instead of matching it.
    """
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        assert lines[0].startswith("event: "), f"frame does not begin with an event field: {block!r}"
        assert lines[1].startswith("data: "), f"frame has no data field: {block!r}"
        assert len(lines) == 2, f"a frame must be exactly two lines, got {len(lines)}: {block!r}"
        frames.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return frames


class TestTheFrameEncoder:
    """`format_event` is what makes the enum load-bearing rather than documentary."""

    def test_it_encodes_a_well_formed_sse_frame(self) -> None:
        assert format_event(SSEEventType.TOKEN, {"text": "FROM python"}) == (
            'event: token\ndata: {"text":"FROM python"}\n\n'
        )

    def test_it_refuses_an_event_name_outside_the_vocabulary(self) -> None:
        # The exact defect this closes: the old service emitted `token_chunk`, which no listener
        # registered for `token` would ever receive — a silently empty stream, not an error.
        with pytest.raises(TypeError) as raised:
            format_event("token_chunk", {})  # type: ignore[arg-type]
        assert "SSEEventType" in str(raised.value)

    def test_a_payload_containing_a_newline_cannot_split_the_frame(self) -> None:
        # Newlines separate SSE frames, so an unescaped one would make the rest of a Dockerfile
        # parse as SSE field names. Every artifact this streams contains newlines.
        frame = format_event(SSEEventType.TOKEN, {"text": "FROM python\nWORKDIR /app"})
        assert frame.count("\n\n") == 1
        parsed = parse_frames(frame)
        assert parsed == [("token", {"text": "FROM python\nWORKDIR /app"})]


class TestTheStreamShape:
    """§12.6 step 7 asserts status -> token(s) -> validation -> complete."""

    async def test_it_emits_the_documented_sequence(self) -> None:
        outcome = GenerationOutcome(run_id=uuid.uuid4())
        raw = "".join(
            [
                frame
                async for frame in GenerationService().stream_generation(
                    uuid.uuid4(), "a python service", outcome=outcome
                )
            ]
        )
        events = [event for event, _ in parse_frames(raw)]

        assert events[0] == "status"
        assert events[-1] == "complete"
        assert "validation" in events
        assert events.index("validation") < events.index("complete")
        assert events.count("token") >= 1
        # Every token precedes the validation verdict: a token after it would mean the gate ran on
        # output that was still arriving.
        assert max(i for i, e in enumerate(events) if e == "token") < events.index("validation")

    async def test_every_event_name_is_in_the_vocabulary(self) -> None:
        raw = "".join(
            [frame async for frame in GenerationService().stream_generation(uuid.uuid4(), "node app")]
        )
        emitted = {event for event, _ in parse_frames(raw)}
        allowed = {e.value for e in SSEEventType}
        # The regression guard for the original defect: run_start, token_chunk and run_complete
        # would each fail here.
        assert emitted <= allowed, f"emitted names outside §7.4: {emitted - allowed}"

    async def test_it_produces_the_two_artifacts_step_six_asks_for(self) -> None:
        outcome = GenerationOutcome(run_id=uuid.uuid4())
        async for _ in GenerationService().stream_generation(uuid.uuid4(), "a python service", outcome=outcome):
            pass
        assert [f.path for f in outcome.files] == ["Dockerfile", "k8s/deployment.yaml"]
        assert outcome.validation_passed is True
        assert outcome.status == "accepted"
        assert outcome.completion_tokens >= 1

    async def test_the_prompt_selects_the_runtime(self) -> None:
        node = GenerationOutcome(run_id=uuid.uuid4())
        async for _ in GenerationService().stream_generation(uuid.uuid4(), "an express node api", outcome=node):
            pass
        dockerfile = next(f.content for f in node.files if f.path == "Dockerfile")
        assert "node:20-alpine" in dockerfile
        assert "containerPort: 3000" in next(
            f.content for f in node.files if f.path == "k8s/deployment.yaml"
        )

    async def test_a_terminal_frame_is_always_emitted(self) -> None:
        # A stream ending in neither `complete` nor `error` is indistinguishable from a dropped
        # connection, which is the one outcome a client cannot recover from.
        raw = "".join(
            [frame async for frame in GenerationService().stream_generation(uuid.uuid4(), "x")]
        )
        assert parse_frames(raw)[-1][0] in {"complete", "error"}


class TestTheValidationGateIsDeterministic:
    """§11.5.5 makes this gate blocking and the rubric advisory, so it must not be a score."""

    def test_it_refuses_a_dockerfile_that_runs_as_root(self) -> None:
        service = GenerationService()
        passed, findings = service._validate(
            (
                GeneratedFile(path="Dockerfile", content="FROM python:3.11\nCMD [\"x\"]\n"),
                GeneratedFile(
                    path="k8s/deployment.yaml",
                    content="apiVersion: apps/v1\nkind: Deployment\nmetadata:\nspec:\n",
                ),
            )
        )
        assert passed is False
        assert any("USER" in f for f in findings)

    def test_it_refuses_a_dockerfile_with_no_from(self) -> None:
        passed, findings = GenerationService()._validate(
            (GeneratedFile(path="Dockerfile", content="RUN echo hi\nUSER 1001\n"),)
        )
        assert passed is False
        assert any("FROM" in f for f in findings)

    def test_it_names_every_missing_manifest_key(self) -> None:
        passed, findings = GenerationService()._validate(
            (
                GeneratedFile(path="Dockerfile", content="FROM x\nUSER 1001\n"),
                GeneratedFile(path="k8s/deployment.yaml", content="apiVersion: apps/v1\n"),
            )
        )
        assert passed is False
        # Every missing key is reported, not just the first: a gate that stops at one finding makes
        # fixing artifacts an iterative guessing game.
        assert sum("missing" in f for f in findings) == 3

    def test_the_artifacts_it_generates_pass_its_own_gate(self) -> None:
        service = GenerationService()
        passed, findings = service._validate(service._render("a python service"))
        assert passed is True, findings


class TestItIsMountedAndAuthenticated:
    async def test_the_endpoint_is_registered(self, app_no_auth: Any) -> None:
        spec = app_no_auth.openapi()
        assert GENERATION_PATH in spec["paths"]
        assert "post" in spec["paths"][GENERATION_PATH]

    async def test_it_declares_an_event_stream_response(self, app_no_auth: Any) -> None:
        operation = app_no_auth.openapi()["paths"][GENERATION_PATH]["post"]
        assert SSE_MEDIA_TYPE in operation["responses"]["200"]["content"]

    async def test_it_refuses_an_unauthenticated_caller(self, app_no_auth: Any) -> None:
        transport = ASGITransport(app=app_no_auth)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                GENERATION_PATH, json={"project_id": str(uuid.uuid4()), "prompt": "hi"}
            )
        assert response.status_code == 401

    async def test_it_is_not_listed_public(self) -> None:
        from src.auth.public_routes import is_public

        assert not is_public(GENERATION_PATH, "POST")

    async def test_an_empty_prompt_is_rejected_by_validation(self, app_no_auth: Any) -> None:
        # min_length=1 on the field, so this is a 422 before the handler — but it must not be a 401
        # first, or the test would prove nothing about validation. Checked via the schema instead.
        schema = app_no_auth.openapi()["components"]["schemas"]["GenerationRequest"]
        assert schema["properties"]["prompt"]["minLength"] == 1
        assert set(schema["required"]) == {"project_id", "prompt"}


class TestItConnectsGenerationToGovernance:
    """`change_sets.origin='generation'` and `generation_run_id` existed and nothing set them."""

    def test_the_route_submits_through_the_chokepoint(self) -> None:
        import inspect

        from src.generation import routes

        source = inspect.getsource(routes)
        # Asserted on the source because the alternative — a full DB round trip — belongs in the
        # e2e journey, where steps 6 through 9 exercise this path against real tables.
        assert "chokepoint.submit" in source
        assert 'origin="generation"' in source
        assert "generation_run_id=run_id" in source

    def test_generated_items_are_creates_with_content(self) -> None:
        import inspect

        from src.generation import routes

        source = inspect.getsource(routes)
        # `update` would require the pre-image `change_items.old_hash` is computed from, and this
        # endpoint has not read the working tree. Claiming one it never saw is the stale-apply
        # hazard that column exists to catch.
        assert 'action="create"' in source
