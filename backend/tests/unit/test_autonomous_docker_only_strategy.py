# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for the first-class docker_only Autonomous Deployment strategy.

Verifies:
1. docker_only is accepted without GitHub or Vercel configuration.
2. Existing strategies preserve original validation behavior.
3. docker_only stage graph excludes both external deployment stages.
4. No GitHub/Vercel handler is invoked during a Docker-only run, including recovery paths.
5. Local generation, validation, build, and verification stages remain connected in exact canonical order.
6. Strategy-specific artifact selection behaves correctly for single-container, multi-service, and k8s projects.
7. Lease renewal weights and recovery semantics are preserved for docker_only.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from src.deployments.autonomous_gates import (
    _active_targets_for_strategy,
    evaluate_g7_final_verification,
)
from src.deployments.autonomous_models import AutonomousDeployment
from src.deployments.autonomous_schemas import (
    CreateAutonomousRunRequest,
    DeploymentStrategy,
    DockerConfigRequest,
    GitHubConfigRequest,
    VercelConfigRequest,
)
from src.deployments.autonomous_service import (
    STAGE_G1_BLUEPRINT,
    STAGE_G2_ARTIFACT,
    STAGE_G3_CONSISTENCY,
    STAGE_G4_BUILD,
    STAGE_G5_APPLY,
    STAGE_G6_WORKLOAD,
    STAGE_G7_VERIFICATION,
    STAGE_GITHUB_RELEASE,
    STAGE_VERCEL_DEPLOY,
    build_stage_graph,
)
from src.deployments.autonomous_worker import (
    DEFAULT_STRATEGY_WEIGHTS,
)
from src.generation.prompt_compiler import (
    ReadinessCheck,
    compile_prompt,
)


def _valid_gh_config() -> GitHubConfigRequest:
    return GitHubConfigRequest(
        repository_mode="existing",
        repository_name="owner/repo",
        target_branch="main",
    )


def _valid_vercel_config() -> VercelConfigRequest:
    return VercelConfigRequest(
        project_name="my-frontend-app",
        production_deploy=True,
    )


def _valid_docker_config() -> DockerConfigRequest:
    return DockerConfigRequest(
        image_tag="repo:test",
        host_port=8080,
        container_port=8080,
    )


class TestDockerOnlyStrategyValidation:
    """Verifies schema validation for docker_only and preservation of existing strategies."""

    def test_docker_only_accepted_without_external_configs(self) -> None:
        """docker_only must succeed without github_config and vercel_config."""
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.DOCKER_ONLY,
            docker_config=_valid_docker_config(),
        )
        assert req.strategy == DeploymentStrategy.DOCKER_ONLY
        assert req.github_config is None
        assert req.vercel_config is None

    def test_docker_only_accepted_with_minimal_config(self) -> None:
        """docker_only must succeed even when docker_config is omitted."""
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.DOCKER_ONLY,
        )
        assert req.strategy == DeploymentStrategy.DOCKER_ONLY

    def test_docker_only_forbids_github_config(self) -> None:
        """docker_only must reject github_config."""
        with pytest.raises(ValidationError, match="forbids github_config"):
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.DOCKER_ONLY,
                github_config=_valid_gh_config(),
            )

    def test_docker_only_forbids_vercel_config(self) -> None:
        """docker_only must reject vercel_config."""
        with pytest.raises(ValidationError, match="forbids vercel_config"):
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.DOCKER_ONLY,
                vercel_config=_valid_vercel_config(),
            )

    def test_existing_strategies_preserve_validation(self) -> None:
        """Existing strategies must enforce original requirements and prohibitions."""
        # docker_github_vercel requires both
        with pytest.raises(ValidationError, match="requires github_config"):
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
                vercel_config=_valid_vercel_config(),
            )
        with pytest.raises(ValidationError, match="requires vercel_config"):
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
                github_config=_valid_gh_config(),
            )

        # docker_github requires github, forbids vercel
        with pytest.raises(ValidationError, match="forbids vercel_config"):
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.DOCKER_GITHUB,
                github_config=_valid_gh_config(),
                vercel_config=_valid_vercel_config(),
            )

        # github_only forbids vercel and docker
        with pytest.raises(ValidationError, match="forbids docker_config"):
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.GITHUB_ONLY,
                github_config=_valid_gh_config(),
                docker_config=_valid_docker_config(),
            )


class TestDockerOnlyStageGraph:
    """Verifies that docker_only constructs a local-only stage graph."""

    def test_stage_graph_contains_only_local_stages(self) -> None:
        """docker_only stage graph must have G1 through G6 and G7, excluding GitHub and Vercel."""
        graph = build_stage_graph(DeploymentStrategy.DOCKER_ONLY)
        stage_names = [s[0] for s in graph]
        gate_ids = [s[1] for s in graph]

        expected_stages = [
            STAGE_G1_BLUEPRINT,
            STAGE_G2_ARTIFACT,
            STAGE_G3_CONSISTENCY,
            STAGE_G4_BUILD,
            STAGE_G5_APPLY,
            STAGE_G6_WORKLOAD,
            STAGE_G7_VERIFICATION,
        ]

        assert stage_names == expected_stages
        assert gate_ids == ["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
        assert STAGE_GITHUB_RELEASE not in stage_names
        assert STAGE_VERCEL_DEPLOY not in stage_names

    def test_existing_stage_graphs_preserved(self) -> None:
        """Existing strategies must still include their respective stages."""
        dgv_graph = [s[0] for s in build_stage_graph(DeploymentStrategy.DOCKER_GITHUB_VERCEL)]
        assert STAGE_GITHUB_RELEASE in dgv_graph
        assert STAGE_VERCEL_DEPLOY in dgv_graph

        dg_graph = [s[0] for s in build_stage_graph(DeploymentStrategy.DOCKER_GITHUB)]
        assert STAGE_GITHUB_RELEASE in dg_graph
        assert STAGE_VERCEL_DEPLOY not in dg_graph


class TestDockerOnlyHandlerExclusionAndRecovery:
    """Verifies that no GitHub or Vercel handlers are reachable under docker_only."""

    def test_active_targets_is_docker_only(self) -> None:
        targets = _active_targets_for_strategy("docker_only")
        assert targets == {"docker"}
        assert "github" not in targets
        assert "vercel" not in targets

    @pytest.mark.asyncio
    async def test_g7_marks_external_targets_not_applicable(self) -> None:
        """In G7, external targets must be marked not_applicable under docker_only."""
        run = MagicMock(spec=AutonomousDeployment)  # noqa: FO-TD002 - declarative SQLAlchemy model attributes not accessible via spec_set
        run.id = uuid.uuid4()
        run.strategy = DeploymentStrategy.DOCKER_ONLY
        run.configuration = {}
        run.stages = []

        session = MagicMock()
        res = await evaluate_g7_final_verification(session, run, context={"simulation_mode": True})

        assert res.passed is True
        details = res.details
        assert details["target_results"]["docker"] == "verified"
        assert details["target_results"]["github"] == "not_applicable"
        assert details["target_results"]["vercel"] == "not_applicable"

    @pytest.mark.asyncio
    async def test_recovery_reconcile_skips_external_targets(self) -> None:
        """AutonomousRecoverySweeper must not reconcile GitHub or Vercel for docker_only."""
        from src.deployments.autonomous_recovery import AutonomousRecoverySweeper

        run = MagicMock(spec=AutonomousDeployment)  # noqa: FO-TD002 - declarative SQLAlchemy model attributes not accessible via spec_set
        run.id = uuid.uuid4()
        run.strategy = DeploymentStrategy.DOCKER_ONLY
        run.configuration = {}

        session = MagicMock()
        sweeper = AutonomousRecoverySweeper()
        res = await sweeper.reconcile_run(session, run, context={})

        assert res.docker_status in ("verified", "reconciled", "not_applicable")
        assert res.github_status == "not_applicable"
        assert res.vercel_status == "not_applicable"


class TestDockerOnlyArtifactSelection:
    """Verifies prompt compiler behavior for docker_only."""

    def test_prompt_compiler_omits_k8s_for_docker_only(self) -> None:
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
        ]

        compiled = compile_prompt(
            checks=failing_checks,
            paths=["package.json", "src/index.js"],
            contents={"package.json": '{"name": "demo"}'},
            inventory={"languages": ["javascript"]},
            project_name="demo",
            target_strategy="docker_only",
        )

        targets = compiled.write_targets
        assert "Dockerfile" in targets
        assert not any(t.startswith("k8s/") for t in targets)
        assert any("does not target Kubernetes" in u for u in compiled.unaddressable)

    def test_weights_and_progress_sum_to_100(self) -> None:
        weights = DEFAULT_STRATEGY_WEIGHTS[DeploymentStrategy.DOCKER_ONLY.value]
        total = sum(weights.values())
        assert abs(total - 100.0) < 1e-6


class TestDockerOnlyRemediationAndSafeguards:
    """Verifies end-to-end strategy propagation, security validation, and safeguards."""

    def test_docker_only_prevents_helm_and_k8s_generation_in_service(self) -> None:
        """GenerationService._render must never produce k8s, helm, or external artifacts for docker_only."""
        from src.generation.service import GenerationService

        service = GenerationService()
        files = service._render(
            "deploy this app",
            project={"name": "test-app", "target_strategy": "docker_only"},
        )
        paths = [f.path for f in files]

        assert "Dockerfile" in paths
        assert ".dockerignore" in paths
        assert ".env.example" in paths

        # Ensure no Kubernetes, Helm, GitHub, or OpenTofu files are rendered
        assert not any(p.startswith("k8s/") for p in paths)
        assert not any(p.startswith("charts/") for p in paths)
        assert not any("chart.yaml" in p.lower() for p in paths)
        assert not any(p.startswith(".github/") for p in paths)
        assert not any(p.startswith("infra/") for p in paths)

    def test_k8s_strategy_continues_generating_required_artifacts(self) -> None:
        """Kubernetes-enabled strategy must continue generating k8s manifests and Helm charts."""
        from src.generation.service import GenerationService

        service = GenerationService()
        files = service._render(
            "deploy this app",
            project={"name": "k8s-app", "target_strategy": "kubernetes"},
        )
        paths = [f.path for f in files]

        assert "Dockerfile" in paths
        assert "k8s/deployment.yaml" in paths
        assert "k8s/service.yaml" in paths
        assert "k8s/ingress.yaml" in paths
        assert "charts/k8s-app/Chart.yaml" in paths
        assert "charts/k8s-app/values.yaml" in paths

    def test_docker_only_multi_service_vs_single_service_compose(self) -> None:
        """Multi-service docker_only projects receive compose while single-service without compose does not."""
        from src.generation.prompt_compiler import ProjectArchitecture, ServiceSpec
        from src.generation.service import GenerationService

        service = GenerationService()

        # Multi-service project
        multi_arch = ProjectArchitecture(
            is_multi_service=True,
            services=[
                ServiceSpec(name="api", directory="api", dockerfile_path="api/Dockerfile", port=8000),
                ServiceSpec(name="web", directory="web", dockerfile_path="web/Dockerfile", port=3000),
            ],
        )
        multi_files = service._render(
            "deploy this app",
            project={"name": "multi-app", "target_strategy": "docker_only", "architecture": multi_arch},
        )
        multi_paths = [f.path for f in multi_files]
        assert "docker-compose.yml" in multi_paths
        assert "Dockerfile" in multi_paths

        # Single-service project
        single_arch = ProjectArchitecture(is_multi_service=False)
        single_files = service._render(
            "deploy this app",
            project={"name": "single-app", "target_strategy": "docker_only", "architecture": single_arch},
        )
        single_paths = [f.path for f in single_files]
        assert "docker-compose.yml" not in single_paths
        assert "Dockerfile" in single_paths

    def test_g8_repair_and_ai_recovery_cannot_reintroduce_k8s_or_helm(self) -> None:
        """G8 repair prompts and AI recovery cannot emit or accept k8s or helm manifests for docker_only."""
        from src.generation.prompt_compiler import compile_repair_prompts

        failing_checks = [
            ReadinessCheck(
                id="dockerfile_present",
                category="containerization",
                passed=False,
                points=0,
                max_points=25,
                generatable=True,
                remedy_path="Dockerfile",
                why_it_matters="Dockerfile required.",
            ),
            ReadinessCheck(
                id="kubernetes_manifests_present",
                category="orchestration",
                passed=False,
                points=0,
                max_points=25,
                generatable=True,
                remedy_path="k8s/deployment.yaml",
                why_it_matters="K8s required.",
            ),
            ReadinessCheck(
                id="helm_chart_present",
                category="orchestration",
                passed=False,
                points=0,
                max_points=25,
                generatable=True,
                remedy_path="charts/<name>/Chart.yaml",
                why_it_matters="Helm required.",
            ),
        ]

        repairs = compile_repair_prompts(
            checks=failing_checks,
            paths=["package.json"],
            contents={"package.json": "{}"},
            inventory={},
            target_strategy="docker_only",
        )

        assert "Dockerfile" in repairs
        assert not any(p.startswith("k8s/") for p in repairs)
        assert not any(p.startswith("charts/") for p in repairs)

    @pytest.mark.asyncio
    async def test_ai_recovery_filters_unauthorized_k8s_and_helm_manifests(self) -> None:
        """resolve_failure_with_ai_async strictly strips k8s/helm artifacts for docker_only runs."""
        from src.diagnostics.bundle import DiagnosticBundle
        from src.recovery.ai_resolver import resolve_failure_with_ai_async

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": (
                            "### FILE: Dockerfile\n"
                            "```\n"
                            "FROM node:20-alpine AS builder\n"
                            "WORKDIR /app\n"
                            "COPY package*.json ./\n"
                            "RUN npm ci\n"
                            "COPY . .\n"
                            "USER 10001\n"
                            'CMD ["npm", "start"]\n'
                            "```\n\n"
                            "### FILE: charts/test-app/Chart.yaml\n"
                            "```\n"
                            "apiVersion: v2\n"
                            "name: test-app\n"
                            "version: 0.1.0\n"
                            "```\n\n"
                            "### FILE: k8s/deployment.yaml\n"
                            "```\n"
                            "apiVersion: apps/v1\n"
                            "kind: Deployment\n"
                            "metadata:\n"
                            "  name: test-app\n"
                            "```\n"
                        )
                    }
                }
            ]
        }
        mock_client.post.return_value = mock_resp

        bundle = DiagnosticBundle(
            gate_identifier="G4_build",
            stage="G4_build",
            command_line="docker build .",
            exit_code=1,
            stderr="syntax error in Dockerfile",
        )

        run = MagicMock()
        run.strategy = DeploymentStrategy.DOCKER_ONLY
        run.configuration = {"strategy": "docker_only"}

        plan = await resolve_failure_with_ai_async(
            bundle=bundle,
            current_iteration=1,
            workspace_path=None,
            client=mock_client,
            run=run,
        )

        assert plan.status == "PROPOSED"
        assert "Dockerfile" in plan.suggested_manifests
        assert "charts/test-app/Chart.yaml" not in plan.suggested_manifests
        assert "k8s/deployment.yaml" not in plan.suggested_manifests

    def test_docker_only_artifact_validation_security_checks(self, tmp_path: Path) -> None:
        """_apply_run_local_artifacts rejects unauthorized paths and preserves security validation."""
        from src.deployments.autonomous_gates import (
            _apply_run_local_artifacts,
            _is_allowed_manifest_path,
        )

        # Helm charts must NOT be allowed manifest paths
        assert not _is_allowed_manifest_path("charts/code-review-platform/Chart.yaml")
        assert not _is_allowed_manifest_path("src/app.py")
        assert not _is_allowed_manifest_path("package.json")
        assert not _is_allowed_manifest_path("../escaped.yaml")

        # Allowed root manifests
        assert _is_allowed_manifest_path("Dockerfile")
        assert _is_allowed_manifest_path(".dockerignore")
        assert _is_allowed_manifest_path("docker-compose.yml")
        assert _is_allowed_manifest_path(".env.example")

        # Applying unauthorized charts file must raise ValueError
        ws = tmp_path / "workspace"
        ws.mkdir()

        with pytest.raises(ValueError, match="Security violation.*not an authorized deployment manifest"):
            _apply_run_local_artifacts(ws, {"charts/code-review-platform/Chart.yaml": "apiVersion: v2"})

        with pytest.raises(ValueError, match="Security violation.*not an authorized deployment manifest"):
            _apply_run_local_artifacts(ws, {"src/server.js": "console.log('pwned')"})

        # Applying authorized manifests must succeed
        applied = _apply_run_local_artifacts(
            ws,
            {
                "Dockerfile": "FROM node:20-alpine",
                "docker-compose.yml": "services:\n  app:\n    image: test",
            },
        )
        assert "Dockerfile" in applied
        assert "docker-compose.yml" in applied
        assert (ws / "Dockerfile").exists()
        assert (ws / "docker-compose.yml").exists()

    @pytest.mark.asyncio
    async def test_canonical_index_precondition_aborts_when_index_missing(self, tmp_path: Path) -> None:
        """G2 must fail truthfully without generic templates when canonical index is absent."""
        from src.deployments.autonomous_gates import evaluate_g2_existing_artifacts

        session = AsyncMock()
        # Mock load_index_evidence returning empty
        with patch("src.core.index_evidence.load_index_evidence", autospec=True) as mock_ev:
            evidence_mock = MagicMock()
            evidence_mock.paths = ()
            mock_ev.return_value = evidence_mock

            run = MagicMock()
            run.id = uuid.uuid4()
            run.project_id = uuid.uuid4()
            run.strategy = DeploymentStrategy.DOCKER_ONLY
            run.configuration = {}

            ws = tmp_path / "clean_ws"
            ws.mkdir()

            res = await evaluate_g2_existing_artifacts(
                session,
                run,
                context={"workspace_dir": str(ws), "live": True, "force_real_generation": True},
            )

            assert res.passed is False
            assert "no codebase index was found" in res.message
            assert res.details.get("error") == "index_empty"
