# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests verifying architecture-aware artifact generation and G2 prerequisite enforcement.

Test scenarios:
1. G2 prerequisite: Fails before generation when canonical index is missing or empty.
2. G2 prerequisite: Fails when source preparation status is not 'source_ready'.
3. G2 prerequisite: Fails when workspace directory does not exist or is empty on disk.
4. Provenance tracking: True provenance recorded ('provider', 'template', 'reused', 'deterministic_template').
5. Truthful AI generation failure: G2 fails without falling back to static templates when AI generation fails.
6. Architecture detection: Multi-service layout detected from multiple service directories or workspaces.
7. Multi-service Compose generation: Multi-service projects compile and render multi-service compose manifests.
8. Non-Kubernetes strategy routing: Docker strategies route k8s checks to unaddressable without consuming write slots.
9. Single-container Docker semantics: Single-container Docker projects do not force compose unless requested.
10. Worker lease fencing: Lease heartbeats and fencing tokens remain preserved during generation operations.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from src.core.readiness import ReadinessCheck
from src.deployments.autonomous_gates import (
    evaluate_g2_existing_artifacts,
)
from src.deployments.autonomous_models import AutonomousDeployment
from src.deployments.autonomous_schemas import DeploymentStrategy
from src.deployments.autonomous_worker import (
    AutonomousWorker,
    PipelineHeartbeat,
)
from src.generation.project_hygiene_renderers import compose_file
from src.generation.prompt_compiler import (
    ProjectArchitecture,
    ServiceSpec,
    analyze_project_architecture,
    compile_prompt,
)

from tests.unit.test_autonomous_gates import MockAsyncSession

pytestmark = [pytest.mark.mandatory]


class StubRows:
    """Mock result set implementing scalar and iterable row protocols."""

    def __init__(
        self,
        scalar_val: Any = None,
        rows: list[Any] | None = None,
        mappings_list: list[dict[str, Any]] | None = None,
    ) -> None:
        self._scalar = scalar_val
        self._rows = rows or []
        self._mappings = mappings_list or []

    def scalar(self) -> Any:
        return self._scalar

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalars(self) -> StubRows:
        return self

    def first(self) -> Any:
        return self._mappings[0] if self._mappings else (self._rows[0] if self._rows else None)

    def mappings(self) -> StubRows:
        return self

    def __iter__(self):
        return iter(self._rows)

    def all(self) -> list[Any]:
        return self._rows


class LiveSessionStub:
    """Session stub simulating a real database session to trigger live G2 validation logic."""

    def __init__(
        self,
        *,
        file_tree_count: int = 10,
        project_name: str = "test-project",
        paths: list[str] | None = None,
        contents: dict[str, str] | None = None,
    ) -> None:
        self.file_tree_count = file_tree_count
        self.project_name = project_name
        self.paths = paths or []
        self.contents = contents or {}
        self.committed = False

    async def get(self, model_cls: Any, pk: Any) -> Any:
        return None

    async def execute(self, stmt: Any, params: Any = None) -> StubRows:
        stmt_text = str(getattr(stmt, "text", stmt))
        if "count(*)" in stmt_text and "file_tree" in stmt_text:
            return StubRows(scalar_val=self.file_tree_count, rows=[(self.file_tree_count,)])
        if "SELECT path FROM file_tree" in stmt_text:
            return StubRows(rows=[(p,) for p in self.paths])
        if "SELECT f.path, c.content FROM file_contents" in stmt_text:
            return StubRows(rows=[(p, c) for p, c in self.contents.items()])
        if "FROM projects" in stmt_text or "SELECT name FROM projects" in stmt_text:
            return StubRows(
                scalar_val=self.project_name,
                mappings_list=[{"name": self.project_name, "path": "/test", "settings": {}}],
            )
        if "SELECT inventory FROM analysis_reports" in stmt_text:
            return StubRows(scalar_val={})
        return StubRows(scalar_val=1)

    async def commit(self) -> None:
        self.committed = True

    async def flush(self) -> None:
        pass


def _create_test_run(
    project_id: uuid.UUID,
    strategy: DeploymentStrategy = DeploymentStrategy.DOCKER_GITHUB,
    source_prep_status: str = "source_ready",
    workspace_path: str = "",
) -> AutonomousDeployment:
    run_id = uuid.uuid4()
    return AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status="running",
        strategy=strategy,
        configuration={
            "strategy": strategy.value,
            "simulation_mode": False,
            "source_preparation": {
                "status": source_prep_status,
                "workspace_path": workspace_path,
            },
        },
    )


# Scenario 1: G2 prerequisite - Fails before generation when canonical index is missing or empty
@pytest.mark.asyncio
async def test_g2_prerequisite_fails_when_canonical_index_missing() -> None:
    project_id = uuid.uuid4()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ws = Path(tmp_dir)
        (ws / "main.py").write_text("print('test')", encoding="utf-8")

        session = LiveSessionStub(file_tree_count=0)
        run = _create_test_run(project_id, workspace_path=str(ws))

        res = await evaluate_g2_existing_artifacts(
            session,  # type: ignore[arg-type]
            run,
            context={"is_live": True, "force_source_prep_check": True},
        )
        assert res.passed is False
        assert res.status == "failed"
        assert res.details.get("error") == "canonical_index_missing"
        assert "no codebase index exists in file_tree" in res.message


# Scenario 2: G2 prerequisite - Fails when source preparation status is not 'source_ready'
@pytest.mark.asyncio
async def test_g2_prerequisite_fails_when_source_prep_not_ready() -> None:
    project_id = uuid.uuid4()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ws = Path(tmp_dir)
        (ws / "main.py").write_text("print('test')", encoding="utf-8")

        session = LiveSessionStub(file_tree_count=10)
        run = _create_test_run(project_id, source_prep_status="cloning", workspace_path=str(ws))

        res = await evaluate_g2_existing_artifacts(
            session,  # type: ignore[arg-type]
            run,
            context={"is_live": True, "force_source_prep_check": True},
        )
        assert res.passed is False
        assert res.status == "failed"
        assert res.details.get("error") == "source_prep_not_ready"
        assert "source_ready" in res.message


# Scenario 3: G2 prerequisite - Fails when workspace directory does not exist or is empty
@pytest.mark.asyncio
async def test_g2_prerequisite_fails_when_workspace_missing_or_empty() -> None:
    project_id = uuid.uuid4()
    session = LiveSessionStub(file_tree_count=10)

    # Sub-case A: Non-existent workspace path
    run_missing = _create_test_run(
        project_id,
        workspace_path=f"C:/non_existent_path_{uuid.uuid4().hex}",
    )
    res_missing = await evaluate_g2_existing_artifacts(
        session,  # type: ignore[arg-type]
        run_missing,
        context={"is_live": True, "force_source_prep_check": True},
    )
    assert res_missing.passed is False
    assert res_missing.status == "failed"
    assert res_missing.details.get("error") == "workspace_missing_on_disk"

    # Sub-case B: Empty workspace directory
    with tempfile.TemporaryDirectory() as tmp_dir:
        empty_ws = Path(tmp_dir)
        run_empty = _create_test_run(project_id, workspace_path=str(empty_ws))
        res_empty = await evaluate_g2_existing_artifacts(
            session,  # type: ignore[arg-type]
            run_empty,
            context={"is_live": True, "force_source_prep_check": True},
        )
        assert res_empty.passed is False
        assert res_empty.status == "failed"
        assert res_empty.details.get("error") == "workspace_empty"


# Scenario 4: Provenance tracking - Records true provenance without masquerading
@pytest.mark.asyncio
async def test_g2_provenance_tracking() -> None:
    project_id = uuid.uuid4()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ws = Path(tmp_dir)
        (ws / "Dockerfile").write_text(
            "FROM node:20-alpine AS builder\nWORKDIR /app\nCOPY . .\n"
            "FROM node:20-alpine\nWORKDIR /app\nCOPY --from=builder /app /app\n"
            'USER node\nEXPOSE 3000\nCMD ["npm", "start"]\n',
            encoding="utf-8",
        )

        session = LiveSessionStub(file_tree_count=5, paths=["Dockerfile"])
        run = _create_test_run(project_id, workspace_path=str(ws))

        # When Dockerfile exists, it should be REUSED and record 'reused' provenance
        res = await evaluate_g2_existing_artifacts(
            session,  # type: ignore[arg-type]
            run,
            context={"is_live": True, "force_source_prep_check": True},
        )
        assert res.passed is True
        assert res.details.get("action") == "REUSE"
        assert res.details.get("artifacts_provenance") == "reused"


# Scenario 5: Truthful AI generation failure - G2 fails without falling back to generic templates
@pytest.mark.asyncio
async def test_g2_fails_truthfully_when_ai_generation_yields_no_artifacts() -> None:
    project_id = uuid.uuid4()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ws = Path(tmp_dir)
        (ws / "package.json").write_text('{"name": "test-pkg"}', encoding="utf-8")

        session = LiveSessionStub(
            file_tree_count=1,
            paths=["package.json"],
            contents={"package.json": '{"name": "test-pkg"}'},
        )
        run = _create_test_run(project_id, workspace_path=str(ws))

        # Configure mock GenerationService that returns validation failure
        class FailingGenService:
            async def stream_generation(self, *args: Any, **kwargs: Any):
                outcome = kwargs.get("outcome")
                if outcome:
                    outcome.validation_passed = False
                    outcome.files = []
                    outcome.served_from = "provider"
                if False:
                    yield

        context = {
            "is_live": True,
            "force_source_prep_check": True,
            "force_real_generation": True,
            "generation_service": FailingGenService(),
            "use_mock_generation": False,
        }

        res = await evaluate_g2_existing_artifacts(
            session,  # type: ignore[arg-type]
            run,
            context=context,
        )
        assert res.passed is False
        assert res.status == "failed"
        assert "failed validation or produced no valid artifacts" in res.message
        # Crucial check: No static Dockerfile or compose was written to disk
        assert not (ws / "Dockerfile").exists()


# Scenario 6: Multi-service architecture detection
def test_architecture_detection_multi_service() -> None:
    # 6a. Multi-service layout from separate subdirectories
    paths = [
        "backend/package.json",
        "backend/src/index.ts",
        "frontend/package.json",
        "frontend/src/App.tsx",
    ]
    contents = {
        "backend/package.json": '{"name": "backend"}',
        "frontend/package.json": '{"name": "frontend"}',
    }
    arch = analyze_project_architecture(paths, contents, {})
    assert arch.is_multi_service is True
    assert len(arch.services) == 2
    svc_names = {s.name for s in arch.services}
    assert "backend" in svc_names
    assert "frontend" in svc_names

    # 6b. Workspaces layout from root package.json
    ws_paths = ["package.json", "code-review/package.json", "code-review/src/index.ts"]
    ws_contents = {
        "package.json": json.dumps({"name": "root-app", "workspaces": ["code-review"]}),
        "code-review/package.json": '{"name": "code-review"}',
    }
    ws_arch = analyze_project_architecture(ws_paths, ws_contents, {})
    assert ws_arch.is_multi_service is True
    assert ws_arch.has_workspaces is True


# Scenario 7: Multi-service Compose generation
def test_multi_service_compose_generation() -> None:
    arch = ProjectArchitecture(
        is_multi_service=True,
        services=(
            ServiceSpec(name="api", directory="backend", dockerfile_path="backend/Dockerfile", port=8000),
            ServiceSpec(name="web", directory="frontend", dockerfile_path="frontend/Dockerfile", port=3000),
        ),
        databases=("postgres", "redis"),
    )

    rendered = compose_file("test-app", 8000, "v1.0.0", architecture=arch)
    assert "name: test-app" in rendered
    assert "api:" in rendered
    assert "web:" in rendered
    assert "postgres:" in rendered
    assert "redis:" in rendered
    assert "5432:5432" in rendered
    assert "6379:6379" in rendered
    assert "build:" in rendered
    assert "context: backend" in rendered
    assert "context: frontend" in rendered


# Scenario 8: Non-Kubernetes strategy routing omits k8s from write-targets
def test_non_k8s_strategy_routes_k8s_checks_to_unaddressable() -> None:
    failing_checks = [
        ReadinessCheck(
            id="dockerfile_present",
            category="containerization",
            passed=False,
            points=0,
            max_points=25,
            generatable=True,
            remedy_path="Dockerfile",
            why_it_matters="Dockerfile is required to containerize the application.",
        ),
        ReadinessCheck(
            id="kubernetes_manifests_present",
            category="orchestration",
            passed=False,
            points=0,
            max_points=25,
            generatable=True,
            remedy_path="k8s/deployment.yaml",
            why_it_matters="Kubernetes manifests are required to deploy to cluster.",
        ),
        ReadinessCheck(
            id="compose_file_present",
            category="containerization",
            passed=False,
            points=0,
            max_points=15,
            generatable=True,
            remedy_path="docker-compose.yml",
            why_it_matters="Compose file is needed for multi-container orchestration.",
        ),
    ]

    compiled = compile_prompt(
        checks=failing_checks,
        paths=["package.json", "src/index.js"],
        contents={"package.json": '{"name": "app"}'},
        inventory={"languages": ["javascript"]},
        project_name="app",
        target_strategy="docker + github + vercel",
    )

    # k8s manifests must not be in write targets for Docker strategy
    assert not any(t.startswith("k8s/") for t in compiled.write_targets)
    assert any("does not target Kubernetes" in u for u in compiled.unaddressable)


# Scenario 9: Single-container Docker semantics
def test_single_container_docker_semantics() -> None:
    # Single-container Node app without compose request
    paths = ["package.json", "server.js"]
    contents = {"package.json": '{"name": "single-app"}'}
    arch = analyze_project_architecture(paths, contents, {"languages": ["javascript"]})
    assert arch.is_multi_service is False

    failing_checks = [
        ReadinessCheck(
            id="dockerfile_present",
            category="containerization",
            passed=False,
            points=0,
            max_points=25,
            generatable=True,
            remedy_path="Dockerfile",
            why_it_matters="Dockerfile is required to containerize the application.",
        ),
    ]

    compiled = compile_prompt(
        checks=failing_checks,
        paths=paths,
        contents=contents,
        inventory={"languages": ["javascript"]},
        project_name="single-app",
        target_strategy="docker",
        architecture=arch,
    )

    # Compose is not forced into write targets when single-container and not requested
    assert "docker-compose.yml" not in compiled.write_targets
    assert "Dockerfile" in compiled.write_targets


# Scenario 10: Worker lease heartbeat and fencing tokens remain preserved
@pytest.mark.asyncio
async def test_worker_lease_fencing_preserved() -> None:
    session = MockAsyncSession()
    worker = AutonomousWorker(worker_id="worker-1")
    now = datetime.now(UTC)

    run = AutonomousDeployment(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        status="running",
        fence_token=1,
        worker_id="worker-1",
        lease_expires_at=now + timedelta(seconds=0.30),
    )
    # Register run in MockAsyncSession
    session.runs[run.id] = run
    initial_lease = run.lease_expires_at

    heartbeat_mgr = PipelineHeartbeat(
        worker=worker,
        session=session,  # type: ignore[arg-type]
        run_id=run.id,
        fence_token=1,
        run=run,
        interval_seconds=0.03,
    )

    await heartbeat_mgr.start()
    try:
        # Sleep to allow heartbeat renewals to run
        await asyncio.sleep(0.12)
        assert heartbeat_mgr.renewal_count >= 1
        assert run.lease_expires_at > initial_lease
        assert heartbeat_mgr.ownership_lost_event.is_set() is False
        heartbeat_mgr.assert_ownership()
    finally:
        await heartbeat_mgr.stop()
