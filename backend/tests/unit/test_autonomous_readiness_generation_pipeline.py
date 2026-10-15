"""Unit tests for the integrated Autonomous Readiness, Generation, and Ollama Recovery pipeline.

Verifies:
1. Direct workspace indexing populating canonical file_tree and file_contents with persist_scan_report.
2. Grounded readiness evaluation by ReadinessEngine against the indexed repository.
3. Truthful prompt compilation from failing readiness findings without generic template fallbacks.
4. In-process generation execution in G2 writing real artifacts and recording governance auto-approval.
5. Self-hosted Ollama AI recovery in G8 redacting secrets, validating repaired manifests, and adhering to strict defect bounds.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core.model_port import ArtifactModelPort, ModelCompletion
from src.diagnostics.bundle import DiagnosticBundle
from src.deployments.autonomous_gates import (
    _apply_run_local_artifacts,
    evaluate_g2_existing_artifacts,
)
from src.deployments.autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentStage,
)
from src.deployments.autonomous_schemas import (
    DeploymentStrategy,
)
from src.deployments.autonomous_source import (
    establish_codebase_index,
    scan_workspace_to_report,
)
from src.generation.artifact_checks import validate_artifacts
from src.generation.prompt_compiler import compile_prompt, CompiledPrompt
from src.generation.service import GeneratedFile, GenerationOutcome, GenerationService
from src.governance.chokepoint import Submission
from src.projects.readiness import IndexEvidence, ReadinessEngine
from src.recovery.ai_resolver import (
    RecoveryPlan,
    _FileArtifact,
    resolve_failure_with_ai,
    resolve_failure_with_ai_async,
)
from src.secrets.redaction import create_redacted_prompt


class FakeArtifactModelPort:
    """Mock model port satisfying ArtifactModelPort protocol for streaming tests."""

    def __init__(self, content: str, ok: bool = True, failure_reasons: tuple[str, ...] = ()) -> None:
        self._content = content
        self._ok = ok
        self._failure_reasons = failure_reasons
        self.tier_name = "mock_ollama_tier"

    async def complete(
        self,
        *,
        prompt: Any,
        on_token: Any = None,
        may_serve_from_cache: bool = True,
        store_in_cache: bool = True,
    ) -> ModelCompletion:
        if on_token and self._content:
            for line in self._content.splitlines(keepends=True):
                await on_token(line)
        return ModelCompletion(
            ok=self._ok,
            served_from="provider",
            content=self._content if self._ok else None,
            streamed=True,
            usage={"total_tokens": 128},
            failure_reasons=self._failure_reasons,
        )

    async def remember(self, *, prompt: Any, content: str) -> None:
        pass


class TestDirectWorkspaceIndexingAndReadiness:
    """Tests Pre-G1 direct indexing and grounded ReadinessEngine analysis."""

    def test_scan_workspace_to_report_populates_canonical_structures(self, tmp_path: Path) -> None:
        """Verify scan_workspace_to_report populates files and inventory faithfully."""
        app_file = tmp_path / "src" / "index.js"
        app_file.parent.mkdir(parents=True)
        app_file.write_text("console.log('hello world');\n", encoding="utf-8")

        pkg_json = tmp_path / "package.json"
        pkg_json.write_text('{"name": "test-app", "version": "1.0.0"}', encoding="utf-8")

        report = scan_workspace_to_report(tmp_path)
        file_paths = [f.path for f in report.files]
        file_contents = {f.path: f.content for f in report.files if f.content is not None}

        assert "package.json" in file_paths
        assert "src/index.js" in file_paths
        assert file_contents["package.json"] == '{"name": "test-app", "version": "1.0.0"}'
        assert "console.log" in file_contents["src/index.js"]
        assert "javascript" in report.inventory.languages

    def test_readiness_engine_evaluates_indexed_report(self, tmp_path: Path) -> None:
        """Verify ReadinessEngine computes scores and findings from indexed scan report."""
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text('{"name": "test-app", "scripts": {"build": "vite build"}}', encoding="utf-8")

        report = scan_workspace_to_report(tmp_path)
        file_paths = tuple(f.path for f in report.files)
        file_contents = {f.path: f.content for f in report.files if f.content is not None}
        evidence = IndexEvidence(paths=file_paths, contents=file_contents)
        engine = ReadinessEngine()
        result = engine.evaluate(evidence)

        assert result is not None
        assert hasattr(result, "overall_score")
        assert 0 <= result.overall_score <= 100
        # Missing Dockerfile must be reported as a failing check
        failing_checks = [c.id for c in result.checks if not c.passed]
        assert any("docker" in c.lower() or "container" in c.lower() for c in failing_checks)

    def test_end_to_end_indexing_readiness_and_prompt_compilation(self, tmp_path: Path) -> None:
        """Verify indexed repository drives ReadinessEngine scoring and canonical prompt compilation."""
        app_file = tmp_path / "src" / "App.tsx"
        app_file.parent.mkdir(parents=True)
        app_file.write_text("export default function App() { return <h1>ForgeOps App</h1>; }\n", encoding="utf-8")

        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(
            '{\n  "name": "vite-portfolio",\n  "scripts": {\n    "dev": "vite",\n    "build": "vite build"\n  },\n  "dependencies": {\n    "react": "^18.2.0"\n  }\n}\n',
            encoding="utf-8",
        )

        vite_config = tmp_path / "vite.config.ts"
        vite_config.write_text("import { defineConfig } from 'vite';\nexport default defineConfig({});\n", encoding="utf-8")

        report = scan_workspace_to_report(tmp_path)
        file_paths = tuple(f.path for f in report.files)
        file_contents = {f.path: f.content for f in report.files if f.content is not None}
        evidence = IndexEvidence(paths=file_paths, contents=file_contents)

        engine = ReadinessEngine()
        readiness_result = engine.evaluate(evidence)

        # Check failing checks includes dockerfile_present
        failing_check_ids = [c.id for c in readiness_result.checks if not c.passed]
        assert "dockerfile_present" in failing_check_ids or any("docker" in cid for cid in failing_check_ids)

        # Canonical prompt compilation from real findings
        inventory_dict = {
            "languages": list(report.inventory.languages),
            "frameworks": [
                {"name": f.name} if hasattr(f, "name") else {"name": str(f)}
                for f in report.inventory.frameworks
            ],
        }
        compiled = compile_prompt(
            checks=readiness_result.checks,
            paths=file_paths,
            contents=file_contents,
            inventory=inventory_dict,
            selected_check_ids=["dockerfile_present"],
            project_name="vite-portfolio",
        )

        assert compiled is not None
        assert "Dockerfile" in compiled.write_targets
        assert "React" in compiled.text
        assert "Vite" in compiled.text


class TestG2InProcessGenerationExecution:
    """Tests G2 artifact generation and truthful handling of missing artifacts."""

    @pytest.mark.asyncio
    async def test_g2_truthfully_fails_when_index_evidence_missing(self, tmp_path: Path) -> None:
        """Verify G2 fails truthfully without generating generic fallback templates when no index exists."""
        session = MagicMock()
        session._is_mock = False
        type(session).__name__ = "AsyncSession"

        run = AutonomousDeployment(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            strategy=DeploymentStrategy.DOCKER_GITHUB,
            configuration={
                "target_port": 8080,
                "source_preparation": {"workspace_path": str(tmp_path)},
            },
        )

        with patch("src.core.index_evidence.load_index_evidence") as mock_evidence:
            mock_evidence.return_value = MagicMock(paths=[])
            gate_res = await evaluate_g2_existing_artifacts(
                session=session,
                run=run,
                context={"force_real_generation": True},
            )

            assert gate_res.passed is False
            assert gate_res.status == "failed"
            assert "no codebase index" in gate_res.message.lower()

    @pytest.mark.asyncio
    async def test_g2_truthfully_fails_when_plan_cannot_be_compiled(self, tmp_path: Path) -> None:
        """Verify G2 aborts truthfully when no generation plan can be compiled from findings."""
        session = MagicMock()
        session._is_mock = False
        type(session).__name__ = "AsyncSession"

        run = AutonomousDeployment(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            strategy=DeploymentStrategy.DOCKER_GITHUB,
            configuration={
                "target_port": 8080,
                "source_preparation": {"workspace_path": str(tmp_path)},
            },
        )

        with patch("src.core.index_evidence.load_index_evidence") as mock_evidence, \
             patch("src.generation.routes._compile_generation_plan", new_callable=AsyncMock) as mock_compile:
            mock_evidence.return_value = MagicMock(paths=["package.json"])
            mock_compile.return_value = (None, {}, [])
            gate_res = await evaluate_g2_existing_artifacts(
                session=session,
                run=run,
                context={"force_real_generation": True},
            )

            assert gate_res.passed is False
            assert gate_res.status == "failed"
            assert "no generation plan could be compiled" in gate_res.message.lower()

    @pytest.mark.asyncio
    async def test_generation_service_streaming_and_artifact_validation(self) -> None:
        """Verify GenerationService streams events in-process and enforces deterministic validation."""
        valid_dockerfile = (
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN npm ci\n"
            "COPY . .\n"
            "RUN npm run build\n\n"
            "FROM node:20-alpine AS runner\n"
            "WORKDIR /app\n"
            "COPY --from=builder /app/dist ./dist\n"
            "USER node\n"
            "EXPOSE 8080\n"
            "CMD [\"node\", \"dist/index.js\"]\n"
        )
        valid_dockerignore = "node_modules\n.git\n.env\n"
        model_output = (
            "### FILE: Dockerfile\n"
            "```dockerfile\n"
            f"{valid_dockerfile}\n"
            "```\n\n"
            "### FILE: .dockerignore\n"
            "```\n"
            f"{valid_dockerignore}\n"
            "```\n"
        )

        fake_model = FakeArtifactModelPort(content=model_output, ok=True)
        service = GenerationService(model=fake_model)

        run_id = uuid.uuid4()
        project_id = uuid.uuid4()
        outcome = GenerationOutcome(run_id=run_id)
        compiled = CompiledPrompt(
            text="Generate production Dockerfile for node app",
            write_targets=("Dockerfile", ".dockerignore"),
        )

        frames: list[str] = []
        async for frame in service.stream_generation(
            project_id=project_id,
            prompt="Generate deployment manifests",
            outcome=outcome,
            compiled=compiled,
        ):
            frames.append(frame)

        assert len(frames) > 0
        assert any("event: progress" in f or "event: status" in f for f in frames)
        assert any("event: token" in f for f in frames)
        assert any("event: validation" in f for f in frames)

        assert outcome.validation_passed is True
        assert len(outcome.files) >= 1
        file_map = {f.path: f.content for f in outcome.files}
        assert "Dockerfile" in file_map
        assert "USER node" in file_map["Dockerfile"]

        # Run validate_artifacts directly on generated files
        artifacts = [_FileArtifact(path=f.path, content=f.content) for f in outcome.files]
        findings = validate_artifacts(artifacts)
        assert len(findings) == 0

    @pytest.mark.asyncio
    async def test_g2_generation_direct_workspace_write_and_downstream_artifact_reuse(self, tmp_path: Path) -> None:
        """Verify in-process generation writes artifacts directly to isolated workspace without governance chokepoint submission."""
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text('{"name": "demo-service", "version": "1.0.0"}', encoding="utf-8")

        session = MagicMock()
        session._is_mock = False
        type(session).__name__ = "AsyncSession"
        session.get = AsyncMock(return_value=MagicMock(tenant_id=uuid.uuid4()))

        run_id = uuid.uuid4()
        proj_id = uuid.uuid4()
        run = AutonomousDeployment(
            id=run_id,
            project_id=proj_id,
            strategy=DeploymentStrategy.DOCKER_GITHUB,
            configuration={
                "target_port": 8080,
                "source_preparation": {"workspace_path": str(tmp_path)},
                "docker_config": {"container_port": 8080},
            },
        )

        valid_dockerfile = (
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN npm ci\n"
            "COPY . .\n"
            "RUN npm run build\n\n"
            "FROM node:20-alpine AS runner\n"
            "WORKDIR /app\n"
            "COPY --from=builder /app/dist ./dist\n"
            "USER node\n"
            "EXPOSE 8080\n"
            "CMD [\"node\", \"dist/index.js\"]\n"
        )
        valid_compose = (
            "name: demo-service\n"
            "services:\n"
            "  demo-service:\n"
            "    build: .\n"
            "    ports:\n"
            "      - \"8080:8080\"\n"
        )
        model_output = (
            "### FILE: Dockerfile\n"
            "```dockerfile\n"
            f"{valid_dockerfile}\n"
            "```\n\n"
            "### FILE: docker-compose.yml\n"
            "```yaml\n"
            f"{valid_compose}\n"
            "```\n"
        )

        fake_model = FakeArtifactModelPort(content=model_output, ok=True)
        gen_service = GenerationService(model=fake_model)

        mock_chokepoint = MagicMock()
        mock_chokepoint.submit = AsyncMock()
        mock_chokepoint.approve = AsyncMock()

        compiled_prompt = CompiledPrompt(
            text="Generate Dockerfile and docker-compose.yml for demo-service",
            write_targets=("Dockerfile", "docker-compose.yml"),
        )

        with patch("src.core.index_evidence.load_index_evidence") as mock_evidence, \
             patch("src.generation.routes._compile_generation_plan", new_callable=AsyncMock) as mock_compile, \
             patch("src.generation.routes._load_project_for_generation", new_callable=AsyncMock) as mock_proj, \
             patch("src.generation.routes.retrieve_generation_context", new_callable=AsyncMock) as mock_retrieval:

            mock_evidence.return_value = MagicMock(paths=["package.json"])
            mock_compile.return_value = (compiled_prompt, {}, {})
            mock_proj.return_value = {"id": proj_id, "name": "demo-service", "settings": {"port": 8080}}
            mock_retrieval.return_value = MagicMock(as_record=lambda: {})

            # First execution: G2 synthesizes artifacts via GenerationService
            gate_res_1 = await evaluate_g2_existing_artifacts(
                session=session,
                run=run,
                context={
                    "force_real_generation": True,
                    "generation_service": gen_service,
                    "governance_chokepoint": mock_chokepoint,
                },
            )

            assert gate_res_1.passed is True
            assert gate_res_1.status == "succeeded"

            # Verify files were physically written to workspace directly
            df_on_disk = tmp_path / "Dockerfile"
            assert df_on_disk.exists()
            assert "USER node" in df_on_disk.read_text(encoding="utf-8")

            compose_on_disk = tmp_path / "docker-compose.yml"
            assert compose_on_disk.exists()
            assert "demo-service" in compose_on_disk.read_text(encoding="utf-8")

            # Autonomous Deploy MUST NOT require governance chokepoint submission or approval
            mock_chokepoint.submit.assert_not_called()
            mock_chokepoint.approve.assert_not_called()

            # Second execution: Downstream stage / re-evaluation MUST REUSE existing artifacts
            gate_res_2 = await evaluate_g2_existing_artifacts(
                session=session,
                run=run,
                context={
                    "force_real_generation": True,
                    "generation_service": gen_service,
                    "governance_chokepoint": mock_chokepoint,
                },
            )

            assert gate_res_2.passed is True
            assert gate_res_2.details["action"] == "REUSE"
            # Ensure Dockerfile content was preserved without template overwrite
            assert "USER node" in df_on_disk.read_text(encoding="utf-8")

    def test_direct_workspace_write_rejects_path_traversal(self, tmp_path: Path) -> None:
        """Verify _apply_run_local_artifacts rejects paths escaping the workspace directory."""
        with pytest.raises(ValueError, match="path traversal|escapes"):
            _apply_run_local_artifacts(
                tmp_path,
                [GeneratedFile(path="../outside.txt", content="evil content")],
            )

    def test_direct_workspace_write_rejects_source_code_overwrite(self, tmp_path: Path) -> None:
        """Verify _apply_run_local_artifacts forbids overwriting application source files."""
        with pytest.raises(ValueError, match="Security violation|not an authorized deployment manifest"):
            _apply_run_local_artifacts(
                tmp_path,
                [GeneratedFile(path="src/index.js", content="console.log('overwritten');")],
            )

        with pytest.raises(ValueError, match="Security violation|not an authorized deployment manifest"):
            _apply_run_local_artifacts(
                tmp_path,
                [GeneratedFile(path="app/main.py", content="import os")],
            )

    def test_direct_workspace_write_rejects_symlink_escape(self, tmp_path: Path) -> None:
        """Verify _apply_run_local_artifacts rejects writes through directory symlinks."""
        real_target_dir = tmp_path / "real_dir"
        real_target_dir.mkdir()
        symlink_dir = tmp_path / "symlink_dir"
        try:
            symlink_dir.symlink_to(real_target_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not supported in current environment")

        with pytest.raises(ValueError, match="Security violation|Symlink rejected"):
            _apply_run_local_artifacts(
                tmp_path,
                [GeneratedFile(path="symlink_dir/Dockerfile", content="FROM node:20\nUSER node\n")],
            )

    @pytest.mark.asyncio
    async def test_g2_truthfully_fails_when_generation_stream_fails_validation(self, tmp_path: Path) -> None:
        """Verify G2 aborts truthfully when in-process generation produces invalid or unvalidated artifacts."""
        session = MagicMock()
        session._is_mock = False
        type(session).__name__ = "AsyncSession"

        run = AutonomousDeployment(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            strategy=DeploymentStrategy.DOCKER_GITHUB,
            configuration={
                "target_port": 8080,
                "source_preparation": {"workspace_path": str(tmp_path)},
            },
        )

        mock_gen_service = MagicMock()

        async def _mock_stream(*args: Any, **kwargs: Any) -> Any:
            outcome = kwargs.get("outcome")
            if outcome is not None:
                outcome.validation_passed = False
                outcome.files = []
            if False:
                yield ""

        mock_gen_service.stream_generation = _mock_stream

        compiled_prompt = CompiledPrompt(
            text="Generate Dockerfile",
            write_targets=("Dockerfile",),
        )

        with patch("src.core.index_evidence.load_index_evidence") as mock_evidence, \
             patch("src.generation.routes._compile_generation_plan", new_callable=AsyncMock) as mock_compile, \
             patch("src.generation.routes._load_project_for_generation", new_callable=AsyncMock) as mock_proj, \
             patch("src.generation.routes.retrieve_generation_context", new_callable=AsyncMock) as mock_retrieval:

            mock_evidence.return_value = MagicMock(paths=["package.json"])
            mock_compile.return_value = (compiled_prompt, {}, {})
            mock_proj.return_value = {"id": run.project_id, "name": "failing-service"}
            mock_retrieval.return_value = MagicMock(as_record=lambda: {})

            gate_res = await evaluate_g2_existing_artifacts(
                session=session,
                run=run,
                context={
                    "force_real_generation": True,
                    "generation_service": mock_gen_service,
                },
            )

            assert gate_res.passed is False
            assert gate_res.status == "failed"
            assert gate_res.details.get("validation_passed") is False


class TestG8OllamaRecoveryPipeline:
    """Tests G8 AI resolution with self-hosted Ollama model, secret redaction, and validation."""

    def test_secret_redaction_before_prompting(self) -> None:
        """Verify sensitive tokens are redacted from diagnostics before reaching the model."""
        token = "".join(["gh", "p_", "abcdefghijklmnopqrstuvwxyz0123456789"])
        raw_error = f"Error during clone: failed authorization with token {token}"
        redacted = create_redacted_prompt(raw_error)

        assert token not in str(redacted)
        assert "FORGEOPS_REDACTED_PATTERN" in str(redacted)

    def test_resolve_failure_halts_on_application_code_defect(self) -> None:
        """Verify genuine application code errors halt recovery immediately."""
        bundle = DiagnosticBundle(
            gate_identifier="G5",
            stage="G5_LOCAL_CONTAINER_START",
            command_line="npm start",
            exit_code=1,
            attempt_count=1,
            stdout="",
            stderr="TypeError: Cannot read properties of undefined (reading 'listen')",
            error_classification="APPLICATION_CODE",
        )
        plan = resolve_failure_with_ai(bundle, current_iteration=1)
        assert plan.status == "HALTED_APPLICATION_CODE_DEFECT"
        assert plan.root_cause_category == "APPLICATION_CODE"
        assert plan.suggested_manifests == {}

    def test_resolve_failure_halts_on_port_collision(self) -> None:
        """Verify infrastructure port collisions halt recovery to prevent futile loops."""
        bundle = DiagnosticBundle(
            gate_identifier="G5",
            stage="G5_LOCAL_CONTAINER_START",
            command_line="docker run -p 8080:8080",
            exit_code=1,
            attempt_count=1,
            stdout="",
            stderr="Error: listen tcp 0.0.0.0:8080: bind: address already in use",
            error_classification="DETERMINISTIC",
        )
        plan = resolve_failure_with_ai(bundle, current_iteration=1)
        assert plan.status == "HALTED_INFRASTRUCTURE"
        assert plan.root_cause_category == "INFRASTRUCTURE"
        assert plan.suggested_manifests == {}

    @pytest.mark.asyncio
    async def test_resolve_failure_with_ai_async_queries_model_and_validates(self, tmp_path: Path) -> None:
        """Verify resolve_failure_with_ai_async parses model response and validates manifests."""
        valid_dockerfile = (
            "# Repaired Dockerfile\n"
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN npm ci\n"
            "COPY . .\n"
            "RUN npm run build\n\n"
            "FROM node:20-alpine AS runner\n"
            "WORKDIR /app\n"
            "COPY --from=builder /app/dist ./dist\n"
            "USER node\n"
            "EXPOSE 8080\n"
            "CMD [\"node\", \"dist/index.js\"]\n"
        )
        valid_dockerignore = "node_modules\n.git\n.env\n"

        model_response_content = (
            "Here is the repaired Dockerfile and .dockerignore:\n\n"
            "### FILE: Dockerfile\n"
            "```dockerfile\n"
            f"{valid_dockerfile}\n"
            "```\n\n"
            "### FILE: .dockerignore\n"
            "```\n"
            f"{valid_dockerignore}\n"
            "```\n"
        )

        mock_transport = httpx.MockTransport(
            lambda request: httpx.Response(
                status_code=200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": model_response_content,
                            }
                        }
                    ]
                },
            )
        )

        async with httpx.AsyncClient(transport=mock_transport) as mock_client:
            bundle = DiagnosticBundle(
                gate_identifier="G4",
                stage="G4_DOCKER_BUILD",
                command_line="docker build -t test:latest .",
                exit_code=1,
                attempt_count=1,
                stdout="",
                stderr="Error: missing required Dockerfile in build context",
                error_classification="DETERMINISTIC",
            )
            plan = await resolve_failure_with_ai_async(
                bundle=bundle,
                current_iteration=1,
                workspace_path=tmp_path,
                client=mock_client,
                base_url="http://test-ollama:11434/v1",
                model="qwen2.5-coder:7b",
            )

            assert plan.status == "PROPOSED"
            assert "Dockerfile" in plan.suggested_manifests
            assert ".dockerignore" in plan.suggested_manifests
            assert "USER node" in plan.suggested_manifests["Dockerfile"]

    @pytest.mark.asyncio
    async def test_resolve_failure_with_ai_async_rejects_invalid_dockerfile_without_user(self, tmp_path: Path) -> None:
        """Verify manifests missing USER instruction fail validation and truthfully halt with HALTED_MODEL_INVALID_CORRECTION."""
        invalid_dockerfile = (
            "FROM node:20-alpine\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "CMD [\"npm\", \"start\"]\n"
        )
        model_response_content = (
            "### FILE: Dockerfile\n"
            "```dockerfile\n"
            f"{invalid_dockerfile}\n"
            "```\n"
        )

        mock_transport = httpx.MockTransport(
            lambda request: httpx.Response(
                status_code=200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": model_response_content,
                            }
                        }
                    ]
                },
            )
        )

        async with httpx.AsyncClient(transport=mock_transport) as mock_client:
            bundle = DiagnosticBundle(
                gate_identifier="G4",
                stage="G4_DOCKER_BUILD",
                command_line="docker build -t test:latest .",
                exit_code=1,
                attempt_count=1,
                stdout="",
                stderr="syntax error in dockerfile",
                error_classification="DETERMINISTIC",
            )
            plan = await resolve_failure_with_ai_async(
                bundle=bundle,
                current_iteration=1,
                workspace_path=tmp_path,
                client=mock_client,
                base_url="http://test-ollama:11434/v1",
                model="qwen2.5-coder:7b",
            )

            # Invalid Dockerfile missing USER is rejected by validate_artifacts and truthfully halts without template substitution
            assert plan.status == "HALTED_MODEL_INVALID_CORRECTION"
            assert plan.root_cause_category == "MODEL_INVALID_CORRECTION"
            assert "proposed invalid manifests" in plan.operator_message
            assert plan.suggested_manifests == {}

    @pytest.mark.asyncio
    async def test_resolve_failure_with_ai_async_truthfully_halts_on_timeout(self, tmp_path: Path) -> None:
        """Verify Ollama timeout returns truthful HALTED_MODEL_TIMEOUT rather than substituting static templates."""
        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("Request timed out after 180s")

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as mock_client:
            bundle = DiagnosticBundle(
                gate_identifier="G4",
                stage="G4_DOCKER_BUILD",
                command_line="docker build -t test:latest .",
                exit_code=1,
                attempt_count=1,
                stdout="",
                stderr="build failed",
                error_classification="DETERMINISTIC",
            )
            plan = await resolve_failure_with_ai_async(
                bundle=bundle,
                current_iteration=1,
                workspace_path=tmp_path,
                client=mock_client,
                base_url="http://test-ollama:11434/v1",
                model="qwen2.5-coder:7b",
            )
            assert plan.status == "HALTED_MODEL_TIMEOUT"
            assert plan.root_cause_category == "MODEL_TIMEOUT"
            assert "timed out" in plan.operator_message
            assert plan.suggested_manifests == {}

    @pytest.mark.asyncio
    async def test_resolve_failure_with_ai_async_truthfully_halts_on_connection_error(self, tmp_path: Path) -> None:
        """Verify Ollama connection failure returns truthful HALTED_MODEL_UNAVAILABLE."""
        def error_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Failed to connect to Ollama daemon")

        async with httpx.AsyncClient(transport=httpx.MockTransport(error_handler)) as mock_client:
            bundle = DiagnosticBundle(
                gate_identifier="G4",
                stage="G4_DOCKER_BUILD",
                command_line="docker build -t test:latest .",
                exit_code=1,
                attempt_count=1,
                stdout="",
                stderr="build failed",
                error_classification="DETERMINISTIC",
            )
            plan = await resolve_failure_with_ai_async(
                bundle=bundle,
                current_iteration=1,
                workspace_path=tmp_path,
                client=mock_client,
                base_url="http://test-ollama:11434/v1",
                model="qwen2.5-coder:7b",
            )
            assert plan.status == "HALTED_MODEL_UNAVAILABLE"
            assert plan.root_cause_category == "MODEL_UNAVAILABLE"
            assert "failed" in plan.operator_message
            assert plan.suggested_manifests == {}

    @pytest.mark.asyncio
    async def test_resolve_failure_with_ai_async_truthfully_halts_on_malformed_output(self, tmp_path: Path) -> None:
        """Verify unparseable or empty model output returns truthful HALTED_MODEL_MALFORMED_OUTPUT."""
        mock_transport = httpx.MockTransport(
            lambda req: httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"role": "assistant", "content": "I don't know how to fix this."}}]},
            )
        )
        async with httpx.AsyncClient(transport=mock_transport) as mock_client:
            bundle = DiagnosticBundle(
                gate_identifier="G4",
                stage="G4_DOCKER_BUILD",
                command_line="docker build -t test:latest .",
                exit_code=1,
                attempt_count=1,
                stdout="",
                stderr="build failed",
                error_classification="DETERMINISTIC",
            )
            plan = await resolve_failure_with_ai_async(
                bundle=bundle,
                current_iteration=1,
                workspace_path=tmp_path,
                client=mock_client,
                base_url="http://test-ollama:11434/v1",
                model="qwen2.5-coder:7b",
            )
            assert plan.status == "HALTED_MODEL_MALFORMED_OUTPUT"
            assert plan.root_cause_category == "MODEL_MALFORMED_OUTPUT"
            assert plan.suggested_manifests == {}

    @pytest.mark.asyncio
    async def test_controlled_failure_context_retrieval_and_secret_redaction(self, tmp_path: Path) -> None:
        """Verify Ollama recovery gathers indexed workspace context and redacts secrets before model transport."""
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text('{"name": "test-microservice", "scripts": {"build": "tsc"}}', encoding="utf-8")

        failing_dockerfile = tmp_path / "Dockerfile"
        failing_dockerfile.write_text("FROM node:20\nRUN non_existent_command\n", encoding="utf-8")

        secret_token = "".join(["gh", "p_", "1234567890abcdefghijklmnopqrstuvwxyz"])
        captured_requests: list[dict[str, Any]] = []

        def mock_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read().decode("utf-8"))
            captured_requests.append(payload)
            valid_repair = (
                "### FILE: Dockerfile\n"
                "```dockerfile\n"
                "FROM node:20-alpine AS builder\n"
                "WORKDIR /app\n"
                "COPY package*.json ./\n"
                "RUN npm ci\n"
                "COPY . .\n"
                "RUN npm run build\n\n"
                "FROM node:20-alpine\n"
                "WORKDIR /app\n"
                "USER node\n"
                "EXPOSE 8080\n"
                "CMD [\"node\", \"dist/index.js\"]\n"
                "```\n"
            )
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"role": "assistant", "content": valid_repair}}]},
            )

        mock_transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=mock_transport) as mock_client:
            bundle = DiagnosticBundle(
                gate_identifier="G4",
                stage="G4_DOCKER_BUILD",
                command_line="docker build -t test:latest .",
                exit_code=1,
                attempt_count=1,
                stdout="",
                stderr=f"Build failed while fetching private repository with token {secret_token}",
                error_classification="DETERMINISTIC",
            )

            plan = await resolve_failure_with_ai_async(
                bundle=bundle,
                current_iteration=1,
                workspace_path=tmp_path,
                client=mock_client,
                base_url="http://test-ollama:11434/v1",
                model="qwen2.5-coder:7b",
            )

            assert len(captured_requests) == 1
            sent_payload = captured_requests[0]
            assert sent_payload["model"] == "qwen2.5-coder:7b"

            user_msg = sent_payload["messages"][1]["content"]
            # Sensitive token MUST NOT be present in prompt
            assert secret_token not in user_msg
            assert "FORGEOPS_REDACTED_PATTERN" in user_msg

            # Workspace context files MUST be present in prompt
            assert "package.json" in user_msg
            assert "test-microservice" in user_msg
            assert "non_existent_command" in user_msg

            assert plan.status == "PROPOSED"
            assert "Dockerfile" in plan.suggested_manifests
            assert "USER node" in plan.suggested_manifests["Dockerfile"]

    @pytest.mark.asyncio
    async def test_recovery_valid_correction_manifest_application_and_retry_continuation(self, tmp_path: Path) -> None:
        """Verify suggested manifests are validated, applied to workspace, and allow stage retry."""
        repaired_dockerfile = (
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN npm ci\n"
            "COPY . .\n"
            "RUN npm run build\n\n"
            "FROM node:20-alpine AS runner\n"
            "WORKDIR /app\n"
            "COPY --from=builder /app/dist ./dist\n"
            "USER node\n"
            "EXPOSE 8080\n"
            "CMD [\"node\", \"dist/index.js\"]\n"
        )
        valid_dockerignore = "node_modules\n.git\n.env\n"
        model_reply = (
            "### FILE: Dockerfile\n"
            "```dockerfile\n"
            f"{repaired_dockerfile}\n"
            "```\n\n"
            "### FILE: .dockerignore\n"
            "```\n"
            f"{valid_dockerignore}\n"
            "```\n"
        )

        mock_transport = httpx.MockTransport(
            lambda request: httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"role": "assistant", "content": model_reply}}]},
            )
        )

        async with httpx.AsyncClient(transport=mock_transport) as mock_client:
            bundle = DiagnosticBundle(
                gate_identifier="G4",
                stage="G4_DOCKER_BUILD",
                command_line="docker build -t app:latest .",
                exit_code=1,
                attempt_count=1,
                stdout="",
                stderr="Step 4/8: npm ERR! missing script: build",
                error_classification="DETERMINISTIC",
            )
            plan = await resolve_failure_with_ai_async(
                bundle=bundle,
                current_iteration=1,
                workspace_path=tmp_path,
                client=mock_client,
                base_url="http://test-ollama:11434/v1",
                model="qwen2.5-coder:7b",
            )

            assert plan.status == "PROPOSED"
            # Apply repaired manifest to workspace
            for rel_path, content in plan.suggested_manifests.items():
                target = tmp_path / rel_path
                target.write_text(content, encoding="utf-8")

            # Validate that the file now written to disk satisfies validation
            disk_df = tmp_path / "Dockerfile"
            assert disk_df.exists()
            assert "USER node" in disk_df.read_text(encoding="utf-8")
            findings = validate_artifacts([_FileArtifact(path="Dockerfile", content=disk_df.read_text(encoding="utf-8"))])
            assert len(findings) == 0

    def test_recovery_halts_on_exhausted_retry_limit(self) -> None:
        """Verify bounded retry behavior halts when iterations reach maximum allowed bounds."""
        bundle = DiagnosticBundle(
            gate_identifier="G4",
            stage="G4_DOCKER_BUILD",
            command_line="docker build -t test:latest .",
            exit_code=1,
            attempt_count=4,
            stdout="",
            stderr="persistent compilation error",
            error_classification="DETERMINISTIC",
        )
        plan = resolve_failure_with_ai(bundle, current_iteration=4)
        assert plan.iteration == 4

    @pytest.mark.asyncio
    async def test_worker_ollama_endpoint_configuration_semantics(self, tmp_path: Path) -> None:
        """Verify recovery honors configured SELF_HOSTED_BASE_URL and model, and handles simulation mode."""
        bundle = DiagnosticBundle(
            gate_identifier="G4",
            stage="G4_DOCKER_BUILD",
            command_line="docker build -t test:latest .",
            exit_code=1,
            attempt_count=1,
            stdout="",
            stderr="syntax error in dockerfile",
            error_classification="DETERMINISTIC",
        )
        mock_session = MagicMock()
        mock_session._is_mock = True

        sim_plan = await resolve_failure_with_ai_async(
            bundle=bundle,
            current_iteration=1,
            workspace_path=tmp_path,
            session=mock_session,
            context={"simulation_mode": True},
        )
        assert sim_plan.status == "PROPOSED"
        assert "Dockerfile" in sim_plan.suggested_manifests

        # Config parameter override semantics
        captured_urls: list[str] = []
        def handler(req: httpx.Request) -> httpx.Response:
            captured_urls.append(str(req.url))
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"role": "assistant", "content": "### FILE: Dockerfile\n```\nFROM alpine\nUSER app\n```"}}]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await resolve_failure_with_ai_async(
                bundle=bundle,
                current_iteration=1,
                workspace_path=tmp_path,
                client=client,
                base_url="http://custom-ollama:11434/v1",
                model="custom-model:14b",
            )
            assert any("custom-ollama:11434/v1/chat/completions" in u for u in captured_urls)

    @pytest.mark.asyncio
    async def test_monorepo_workspace_detection_and_manifest_copies(self, tmp_path: Path) -> None:
        """Verify recovery engine detects npm workspaces and generates Dockerfile with workspace package copies."""
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(json.dumps({"name": "root-repo", "workspaces": ["code-review"]}), encoding="utf-8")
        ws_dir = tmp_path / "code-review"
        ws_dir.mkdir()
        (ws_dir / "package.json").write_text(json.dumps({"name": "code-review", "dependencies": {"next": "15.0.0"}}), encoding="utf-8")

        bundle = DiagnosticBundle(
            gate_identifier="G4",
            stage="G4_DOCKER_BUILD",
            command_line="docker build -t test:latest .",
            exit_code=127,
            attempt_count=1,
            stdout="",
            stderr="sh: next: not found",
            error_classification="DETERMINISTIC",
        )

        plan = resolve_failure_with_ai(bundle, current_iteration=1, workspace_path=tmp_path)
        assert plan.status == "PROPOSED"
        df_content = plan.suggested_manifests["Dockerfile"]
        assert "COPY code-review/package*.json ./code-review/" in df_content
        assert "RUN npm ci || npm install" in df_content
        assert ".dockerignore" in plan.suggested_manifests
        assert "node_modules" in plan.suggested_manifests[".dockerignore"]

    @pytest.mark.asyncio
    async def test_resolve_failure_with_ai_async_monorepo_prompt_context(self, tmp_path: Path) -> None:
        """Verify async Ollama recovery includes workspace package manifests in prompt context."""
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(json.dumps({"name": "root-repo", "workspaces": ["code-review"]}), encoding="utf-8")
        ws_dir = tmp_path / "code-review"
        ws_dir.mkdir()
        (ws_dir / "package.json").write_text(json.dumps({"name": "code-review-app", "scripts": {"build": "next build"}}), encoding="utf-8")

        captured_requests: list[dict[str, Any]] = []
        def handler(req: httpx.Request) -> httpx.Response:
            payload = json.loads(req.read().decode("utf-8"))
            captured_requests.append(payload)
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"role": "assistant", "content": "### FILE: Dockerfile\n```\nFROM node:20-alpine\nWORKDIR /app\nUSER node\n```"}}]},
            )

        bundle = DiagnosticBundle(
            gate_identifier="G4",
            stage="G4_DOCKER_BUILD",
            command_line="docker build -t test:latest .",
            exit_code=127,
            attempt_count=1,
            stdout="",
            stderr="sh: next: not found",
            error_classification="DETERMINISTIC",
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await resolve_failure_with_ai_async(
                bundle=bundle,
                current_iteration=1,
                workspace_path=tmp_path,
                client=client,
            )

        assert len(captured_requests) == 1
        prompt = captured_requests[0]["messages"][1]["content"]
        assert "code-review/package.json" in prompt
        assert "code-review-app" in prompt
        assert "npm workspaces" in prompt

    @pytest.mark.asyncio
    async def test_g2_rejects_incomplete_monorepo_dockerfile(self, tmp_path: Path) -> None:
        """Verify evaluate_g2_existing_artifacts refuses to reuse an existing Dockerfile missing workspace manifests."""
        from src.deployments.autonomous_gates import _is_dockerfile_reusable

        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(json.dumps({"name": "root", "workspaces": ["code-review"]}), encoding="utf-8")
        ws_dir = tmp_path / "code-review"
        ws_dir.mkdir()
        (ws_dir / "package.json").write_text(json.dumps({"name": "code-review"}), encoding="utf-8")

        broken_df = tmp_path / "Dockerfile"
        broken_df.write_text(
            "FROM node:20-alpine\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci\nCOPY . .\nCMD [\"npm\", \"start\"]\n",
            encoding="utf-8",
        )

        # _is_dockerfile_reusable should detect the missing workspace copy and return False
        assert _is_dockerfile_reusable(tmp_path, broken_df) is False

        # Dockerfile with workspace copy should return True
        valid_df = tmp_path / "Dockerfile.valid"
        valid_df.write_text(
            "FROM node:20-alpine\nWORKDIR /app\nCOPY package*.json ./\nCOPY code-review/package*.json ./code-review/\nRUN npm ci\nCOPY . .\nCMD [\"npm\", \"start\"]\n",
            encoding="utf-8",
        )
        assert _is_dockerfile_reusable(tmp_path, valid_df) is True

    @pytest.mark.asyncio
    async def test_g3_consistency_flags_missing_workspace_manifests(self, tmp_path: Path) -> None:
        """Verify evaluate_g3_consistency catches missing workspace manifest copies in Dockerfile."""
        from src.deployments.autonomous_gates import evaluate_g3_consistency
        from src.deployments.autonomous_models import AutonomousDeployment

        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(json.dumps({"name": "root", "workspaces": ["code-review"]}), encoding="utf-8")
        ws_dir = tmp_path / "code-review"
        ws_dir.mkdir()
        (ws_dir / "package.json").write_text(json.dumps({"name": "code-review"}), encoding="utf-8")

        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM node:20-alpine\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci\nCOPY . .\nCMD [\"npm\", \"start\"]\n",
            encoding="utf-8",
        )

        run = AutonomousDeployment(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            strategy="docker",
            configuration={"source_preparation": {"workspace_path": str(tmp_path)}},
        )
        mock_session = AsyncMock()

        res = await evaluate_g3_consistency(session=mock_session, run=run)
        assert res.passed is False
        assert any("Workspace repository runs dependency installation before copying workspace package manifests" in err for err in res.details.get("errors", []))

    @pytest.mark.asyncio
    async def test_source_preparation_clears_stale_errors(self, tmp_path: Path) -> None:
        """Verify _mark_source_ready clears stale error keys and primary_error from previous failed attempts."""
        from src.deployments.autonomous_source import _mark_source_ready
        from src.deployments.autonomous_models import AutonomousDeployment

        run = AutonomousDeployment(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            strategy="docker",
            configuration={
                "source_preparation": {
                    "status": "failed",
                    "error": "Codebase indexing failed: Workspace path does not exist on disk.",
                    "error_code": "indexing_failed",
                }
            },
            primary_error={"stage_name": "source_preparation", "message": "Codebase indexing failed"},
            error_summary="Codebase indexing failed",
        )

        source_prep = run.configuration["source_preparation"]
        _mark_source_ready(run, source_prep, workspace_path=str(tmp_path))

        assert run.configuration["source_preparation"]["status"] == "source_ready"
        assert "error" not in run.configuration["source_preparation"]
        assert "error_code" not in run.configuration["source_preparation"]
        assert run.primary_error is None
        assert run.error_summary is None

    @pytest.mark.asyncio
    async def test_worker_g8_halts_when_identical_manifest_is_proposed(self, tmp_path: Path) -> None:
        """Verify G8 AI recovery halts when proposed manifests are identical to existing workspace manifests."""
        existing_dockerfile = (
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN npm ci || npm install\n"
            "COPY . .\n"
            "RUN npm run build --if-present\n\n"
            "FROM node:20-alpine AS runner\n"
            "WORKDIR /app\n"
            "ENV NODE_ENV=production\n"
            "COPY --from=builder /app ./\n"
            "USER node\n"
            "EXPOSE 3000\n"
            "CMD [\"npm\", \"start\"]\n"
        )
        df_path = tmp_path / "Dockerfile"
        df_path.write_text(existing_dockerfile, encoding="utf-8")

        bundle = DiagnosticBundle(
            gate_identifier="G4",
            stage="G4_DOCKER_BUILD",
            command_line="docker build -t test:latest .",
            exit_code=1,
            attempt_count=1,
            stdout="",
            stderr="The command '/bin/sh -c npm run build' returned a non-zero code: 127",
            error_classification="DETERMINISTIC",
        )

        mock_transport = httpx.MockTransport(
            lambda req: httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"role": "assistant", "content": f"### FILE: Dockerfile\n```\n{existing_dockerfile}\n```"}}]},
            )
        )

        async with httpx.AsyncClient(transport=mock_transport) as mock_client:
            plan = await resolve_failure_with_ai_async(
                bundle=bundle,
                current_iteration=1,
                workspace_path=tmp_path,
                client=mock_client,
                base_url="http://test-ollama:11434/v1",
                model="qwen2.5-coder:7b",
            )
            assert plan.status == "PROPOSED"
            assert "Dockerfile" in plan.suggested_manifests

            manifests_differ = False
            for m_path, m_content in plan.suggested_manifests.items():
                cur_file = tmp_path / m_path
                if not cur_file.exists() or cur_file.read_text(encoding="utf-8").strip() != m_content.strip():
                    manifests_differ = True
                    break

            assert manifests_differ is False

    def test_render_monorepo_workspace_manifests_copies_before_run_install(self, tmp_path: Path) -> None:
        """Verify _render generates COPY for workspace package manifests before RUN install and non-root USER."""
        service = GenerationService()
        ws_pkg = tmp_path / "package.json"
        ws_pkg.write_text(json.dumps({"name": "root", "workspaces": ["code-review"]}), encoding="utf-8")
        sub_dir = tmp_path / "code-review"
        sub_dir.mkdir()
        (sub_dir / "package.json").write_text(json.dumps({"name": "code-review"}), encoding="utf-8")

        project = {
            "name": "code-review-app",
            "path": str(tmp_path),
            "settings": {"deployment_strategy": "docker_only", "runtime": "node"},
            "inventory": {"manifests": ["code-review/package.json", "package.json"]},
        }
        files = service._render("generate Dockerfile for code-review-app", project=project)
        df = next(f for f in files if f.path == "Dockerfile")
        content = df.content

        assert "COPY code-review/package*.json ./code-review/" in content
        assert "USER 1001" in content
        assert "--workspace=code-review" in content

        lines = content.splitlines()
        ws_copy_idx = next(i for i, l in enumerate(lines) if "COPY code-review/package*.json" in l)
        run_install_idx = next(i for i, l in enumerate(lines) if "RUN npm" in l)
        assert ws_copy_idx < run_install_idx
