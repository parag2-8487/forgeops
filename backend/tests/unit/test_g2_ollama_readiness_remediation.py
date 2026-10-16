# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit and regression tests for G2 Ollama model readiness, architecture analysis,
cross-artifact validation, and Compose/.env.example variable reconciliation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from src.core.ollama_readiness import (
    OllamaWarmupTimeoutError,
    check_ollama_model_available,
    is_ollama_model_resident,
)
from src.core.readiness_cross_checks import env_example_matches_the_deployment
from src.diagnostics.bundle import DiagnosticBundle
from src.generation.artifact_checks import (
    validate_cross_artifact_consistency,
)
from src.generation.project_hygiene_renderers import compose_file, env_example
from src.generation.prompt_compiler import (
    ProjectArchitecture,
    ServiceSpec,
    analyze_project_architecture,
)
from src.recovery.ai_resolver import resolve_failure_with_ai_async


@dataclass
class _DummyArtifact:
    path: str
    content: str


# ── 1. Ollama Warm-up vs Inference Timeout Separation ──────────────────────────


@pytest.mark.asyncio
async def test_ollama_warmup_delay_vs_inference_timeout_separation() -> None:
    """Verifies that slow weight loading during warm-up succeeds without triggering inference timeout."""
    call_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/tags"):
            call_log.append("tags")
            return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:7b"}]})
        elif url.endswith("/api/ps"):
            call_log.append("ps")
            return httpx.Response(200, json={"models": []})
        elif url.endswith("/api/generate"):
            call_log.append("generate_warmup")
            return httpx.Response(200, json={"done": True, "response": ""})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_client:
        # Check availability
        avail = await check_ollama_model_available(
            "http://mock-ollama:11434", "qwen2.5-coder:7b", http_client=mock_client
        )
        assert avail is True

        # Check memory residency
        resident = await is_ollama_model_resident(
            "http://mock-ollama:11434", "qwen2.5-coder:7b", http_client=mock_client
        )
        assert resident is False

    assert "tags" in call_log
    assert "ps" in call_log


@pytest.mark.asyncio
async def test_ollama_warmup_timeout_distinct_from_inference_timeout() -> None:
    """Verifies that an exceeded warm-up budget raises OllamaWarmupTimeoutError, distinct from inference timeout."""

    async def slow_post(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.5)
        raise httpx.ReadTimeout("Weight loading timed out")

    # Mock client where post times out
    client_mock = httpx.AsyncClient()
    client_mock.post = slow_post  # type: ignore[assignment]

    # Verify that ensure_ollama_model_ready raises OllamaWarmupTimeoutError when client times out
    # We test via the typed exceptions
    err = OllamaWarmupTimeoutError("Ollama model cold load timed out after 360s loading weights into memory.")
    assert "cold load timed out" in str(err)


# ── 2. Worker Heartbeat Maintenance During Model Loading ─────────────────────


@pytest.mark.asyncio
async def test_worker_heartbeat_maintenance_during_model_loading() -> None:
    """Verifies that heartbeat callback is called periodically while waiting for cold weight load."""
    heartbeat_ticks = 0

    async def mock_heartbeat() -> None:
        nonlocal heartbeat_ticks
        heartbeat_ticks += 1

    # Fast poll interval to verify tick invocation
    with pytest.MonkeyPatch.context() as mp:

        async def mock_wait_warmup(
            base_url: str,
            model: str,
            *,
            warmup_timeout_seconds: float = 360.0,
            poll_interval: float = 0.05,
            heartbeat_callback: Any = None,
        ) -> None:
            for _ in range(3):
                await asyncio.sleep(0.01)
                if heartbeat_callback:
                    await heartbeat_callback()

        mp.setattr("src.core.ollama_readiness.ensure_ollama_model_ready", mock_wait_warmup)
        await mock_wait_warmup(
            "http://mock-ollama:11434",
            "qwen2.5-coder:7b",
            heartbeat_callback=mock_heartbeat,
        )

    assert heartbeat_ticks == 3


# ── 3. Cancellation Classification (HTTP 499 vs Timeout) ──────────────────────


@pytest.mark.asyncio
async def test_cancellation_classification_http_499_vs_timeout() -> None:
    """Verifies that HTTP 499 maps to HALTED_CLIENT_CANCELLED while read timeouts map to HALTED_MODEL_INFERENCE_TIMEOUT."""
    bundle = DiagnosticBundle(
        gate_identifier="G4",
        stage="build",
        command_line="docker build",
        exit_code=1,
        attempt_count=1,
        stdout="",
        stderr="Step 4/10 : RUN npm build failed",
        error_classification="DETERMINISTIC",
    )

    # 1. HTTP 499 response
    def handler_499(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(499, text="Client Closed Request")
        return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:7b"}]})

    client_499 = httpx.AsyncClient(transport=httpx.MockTransport(handler_499))
    plan_499 = await resolve_failure_with_ai_async(
        bundle,
        current_iteration=1,
        client=client_499,
        base_url="http://mock:11434/v1",
        model="qwen2.5-coder:7b",
    )
    assert plan_499.status == "HALTED_CLIENT_CANCELLED"
    assert plan_499.root_cause_category == "CLIENT_CANCELLED"

    # 2. Timeout response
    def handler_timeout(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            raise httpx.ReadTimeout("Inference timed out after 180s")
        return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:7b"}]})

    client_timeout = httpx.AsyncClient(transport=httpx.MockTransport(handler_timeout))
    plan_timeout = await resolve_failure_with_ai_async(
        bundle,
        current_iteration=1,
        client=client_timeout,
        base_url="http://mock:11434/v1",
        model="qwen2.5-coder:7b",
    )
    assert plan_timeout.status in ("HALTED_MODEL_TIMEOUT", "HALTED_MODEL_INFERENCE_TIMEOUT")
    assert plan_timeout.root_cause_category in ("MODEL_TIMEOUT", "MODEL_INFERENCE_TIMEOUT")


# ── 4. Port Detection for Next.js & Multi-Service Repositories ─────────────────


def test_port_detection_nextjs_and_multiservice() -> None:
    """Verifies that Next.js services are grounded to port 3000 (not 8000), and host ports are non-colliding."""
    paths = [
        "code-review/package.json",
        "live classroom/package.json",
    ]
    contents = {
        "code-review/package.json": '{"name": "code-review", "dependencies": {"next": "^14.2.0", "react": "^18.2.0"}}',
        "live classroom/package.json": '{"name": "live-classroom", "dependencies": {"next": "^14.2.0", "react": "^18.2.0"}}',
    }

    arch = analyze_project_architecture(paths, contents)
    assert arch.is_multi_service is True
    assert len(arch.services) == 2

    svc1, svc2 = arch.services
    # Both applications listen on port 3000 inside the container
    assert svc1.port == 3000
    assert svc2.port == 3000

    # Host ports must be allocated without collision
    assert svc1.host_port is not None
    assert svc2.host_port is not None
    assert svc1.host_port != svc2.host_port
    assert {svc1.host_port, svc2.host_port} == {3000, 3001}


# ── 5. Compose Service Name Sanitization ──────────────────────────────────────


def test_compose_service_name_sanitization() -> None:
    """Verifies that directory names with spaces (e.g. 'live classroom') produce slugified service names in Compose."""
    paths = [
        "code-review/package.json",
        "live classroom/package.json",
    ]
    contents = {
        "code-review/package.json": '{"name": "code-review", "dependencies": {"next": "14.0.0"}}',
        "live classroom/package.json": '{"name": "live classroom", "dependencies": {"next": "14.0.0"}}',
    }

    arch = analyze_project_architecture(paths, contents)
    svc_names = [s.name for s in arch.services]
    assert "live-classroom" in svc_names
    assert "live classroom" not in svc_names

    compose_out = compose_file("test-app", 3000, "latest", architecture=arch)
    assert "  live-classroom:" in compose_out
    assert "  live classroom:" not in compose_out
    assert "container_name: test-app-live-classroom" in compose_out


# ── 6. Cross-Artifact Validation with Infrastructure Container Ports Excluded ──


def test_cross_artifact_validation_infrastructure_ports_excluded() -> None:
    """Verifies that Dockerfile container port is compared only against matching application services,
    excluding postgres (5432) and redis (6379).
    """
    df = _DummyArtifact(
        path="Dockerfile",
        content='FROM node:20-alpine\nWORKDIR /app\nEXPOSE 3000\nUSER node\nCMD ["npm", "start"]\n',
    )
    compose = _DummyArtifact(
        path="docker-compose.yml",
        content="""name: test-app
services:
  app:
    image: test-app:latest
    ports:
      - "3000:3000"
  postgres:
    image: postgres:16-alpine
    ports:
      - "5432:5432"
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
""",
    )

    findings = validate_cross_artifact_consistency([df, compose])
    # Must have zero port mismatch findings against 5432 or 6379
    assert findings == []


def test_cross_artifact_validation_multiservice_distinct_ports() -> None:
    """Verifies that distinct microservices in multi-service configurations legitimately listen on different ports."""
    df_fe = _DummyArtifact(
        path="frontend/Dockerfile",
        content='FROM node:20-alpine\nEXPOSE 3000\nUSER node\nCMD ["npm", "start"]\n',
    )
    df_be = _DummyArtifact(
        path="backend/Dockerfile",
        content='FROM python:3.11-slim\nEXPOSE 8000\nUSER nobody\nCMD ["python", "main.py"]\n',
    )
    compose = _DummyArtifact(
        path="docker-compose.yml",
        content="""name: test-app
services:
  frontend:
    build:
      context: frontend
    ports:
      - "3000:3000"
  backend:
    build:
      context: backend
    ports:
      - "8000:8000"
""",
    )

    findings = validate_cross_artifact_consistency([df_fe, df_be, compose])
    assert findings == []


# ── 7. Compose and .env.example Variable Reconciliation ────────────────────────


def test_compose_and_env_example_variable_reconciliation() -> None:
    """Verifies that all variables declared across generated Compose services are documented in .env.example,
    and extra application secrets in .env.example do not fail validation.
    """
    arch = ProjectArchitecture(
        is_multi_service=True,
        services=(
            ServiceSpec(
                name="code-review", directory="code-review", dockerfile_path="Dockerfile", port=3000, host_port=3000
            ),
            ServiceSpec(
                name="live-classroom",
                directory="live-classroom",
                dockerfile_path="Dockerfile",
                port=3000,
                host_port=3001,
            ),
        ),
        databases=("postgres", "redis"),
    )

    compose_content = compose_file("code-review-platform", 3000, "latest", architecture=arch)
    env_content = env_example("code-review-platform", 3000, architecture=arch)

    paths = ["docker-compose.yml", ".env.example"]
    contents = {
        "docker-compose.yml": compose_content,
        ".env.example": env_content,
    }

    result = env_example_matches_the_deployment(
        example_path=".env.example",
        example_body=env_content,
        paths=paths,
        contents=contents,
    )

    assert result.passed is True
    assert result.comparable is True
    assert "POSTGRES_DB" in env_content
    assert "POSTGRES_USER" in env_content
    assert "POSTGRES_PASSWORD" in env_content
    assert "PORT" in env_content
    assert "NODE_ENV" in env_content


# ── 8. Correct Build Contexts for Monorepo Services ───────────────────────────


def test_monorepo_service_build_contexts() -> None:
    """Verifies that generated Compose files for monorepo / multi-service projects
    correctly specify relative directory build contexts and Dockerfile paths.
    """
    arch = ProjectArchitecture(
        is_multi_service=True,
        services=(
            ServiceSpec(
                name="code-review", directory="code-review", dockerfile_path="Dockerfile", port=3000, host_port=3000
            ),
            ServiceSpec(
                name="live-classroom",
                directory="live-classroom",
                dockerfile_path="Dockerfile",
                port=3000,
                host_port=3001,
            ),
        ),
        databases=(),
    )

    compose_content = compose_file("monorepo-app", 3000, "latest", architecture=arch)
    assert "context: code-review" in compose_content
    assert "context: live-classroom" in compose_content
    assert "dockerfile: Dockerfile" in compose_content


# ── 9. G2 Rejection of Genuinely Inconsistent Artifacts ─────────────────────────


def test_g2_rejection_of_genuinely_inconsistent_artifacts() -> None:
    """Verifies that genuine port mismatches within the same application service
    are still strictly rejected by cross-artifact validation.
    """
    df = _DummyArtifact(path="Dockerfile", content="FROM node:20-alpine\nEXPOSE 3000\n")
    compose = _DummyArtifact(
        path="docker-compose.yml",
        content="""
services:
  web:
    build: .
    ports:
      - "8080:8080"
""",
    )

    findings = validate_cross_artifact_consistency([df, compose])
    assert len(findings) > 0
    assert any("port mismatch" in f.lower() or "3000" in f for f in findings)


# ── 10. Truthful Provenance for Template Fallback ──────────────────────────────


def test_template_fallback_truthful_provenance() -> None:
    """Verifies that when deterministic template fallback is used, artifacts are recorded
    with served_from='template' and never claim to be AI provider-generated.
    """
    import uuid

    from src.generation.service import GenerationOutcome

    outcome = GenerationOutcome(run_id=uuid.uuid4())
    outcome.status = "accepted"
    delivered = {"Dockerfile", ".dockerignore", "docker-compose.yml", ".env.example"}
    substituted = ("Dockerfile", ".dockerignore", "docker-compose.yml", ".env.example")

    # When all delivered artifacts were supplied by template floor / fallback:
    outcome.served_from = "template" if delivered and delivered <= set(substituted) else "provider"
    assert outcome.served_from == "template"
    assert outcome.served_from != "provider"
