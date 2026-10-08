"""Regression tests for nested Next.js and workspace repository deployments."""

import json
from pathlib import Path

import pytest

from src.blueprint.models import (
    BuildConfig,
    NetworkContract,
    ProjectBlueprint,
    ProtocolType,
    RuntimeContract,
    WorkloadType,
)
from src.validation.consistency_gate import verify_consistency_gate


@pytest.fixture
def workspace_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "code-review-repo"
    repo.mkdir()

    root_pkg = {
        "name": "code-review-suite",
        "version": "1.0.0",
        "private": True,
        "workspaces": ["code-review"],
        "scripts": {
            "build": "npm run build --workspace=code-review",
            "start": "npm run start --workspace=code-review",
        },
    }
    (repo / "package.json").write_text(json.dumps(root_pkg), encoding="utf-8")
    (repo / "package-lock.json").write_text("{}", encoding="utf-8")

    ws_dir = repo / "code-review"
    ws_dir.mkdir()
    sub_pkg = {
        "name": "code-review",
        "version": "0.1.0",
        "scripts": {"build": "next build", "start": "next start"},
        "dependencies": {"next": "^14.2.0", "react": "^18.2.0"},
    }
    (ws_dir / "package.json").write_text(json.dumps(sub_pkg), encoding="utf-8")
    return repo


@pytest.fixture
def nextjs_blueprint(tmp_path: Path) -> ProjectBlueprint:
    return ProjectBlueprint(
        blueprint_id="bp-test-nextjs",
        repository_root=str(tmp_path),
        is_monorepo=True,
        workload_type=WorkloadType.WEB_SERVICE,
        runtime=RuntimeContract(
            language="nodejs",
            runtime_version="20",
            framework="nextjs",
            package_manager="npm",
            start_command="npm run start",
        ),
        build_config=BuildConfig(
            source_dir=".",
            build_command="npm run build",
            artifact_output_dir=".next",
            install_command="npm install",
        ),
        network=NetworkContract(listen_port=3000, protocol=ProtocolType.HTTP),
    )


def test_consistency_gate_rejects_missing_workspace_manifests(
    workspace_repo: Path, nextjs_blueprint: ProjectBlueprint
):
    df_content = """FROM node:22-slim
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["npm", "start"]
"""
    df_path = workspace_repo / "Dockerfile"
    df_path.write_text(df_content, encoding="utf-8")

    result = verify_consistency_gate(
        repo_path=workspace_repo,
        dockerfile_path=df_path,
        compose_path=None,
        blueprint=nextjs_blueprint,
    )
    assert not result.passed
    assert any("Workspace repository runs dependency installation before copying workspace package manifests" in e for e in result.errors)


def test_consistency_gate_accepts_workspace_manifests_copied_before_install(
    workspace_repo: Path, nextjs_blueprint: ProjectBlueprint
):
    df_content = """FROM node:22-slim
WORKDIR /app
COPY package*.json ./
COPY code-review/package*.json ./code-review/
RUN npm install
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["npm", "start"]
"""
    df_path = workspace_repo / "Dockerfile"
    df_path.write_text(df_content, encoding="utf-8")

    result = verify_consistency_gate(
        repo_path=workspace_repo,
        dockerfile_path=df_path,
        compose_path=None,
        blueprint=nextjs_blueprint,
    )
    assert result.passed, f"Gate failed with errors: {result.errors}"


def test_consistency_gate_rejects_hallucinated_dist_copy_for_ssr(
    workspace_repo: Path, nextjs_blueprint: ProjectBlueprint
):
    df_content = """FROM node:22-slim AS builder
WORKDIR /app
COPY package*.json ./
COPY code-review/package*.json ./code-review/
RUN npm install
COPY . .
RUN npm run build

FROM node:22-slim AS runner
WORKDIR /app
COPY --from=builder /app/dist ./dist
EXPOSE 3000
CMD ["npm", "start"]
"""
    df_path = workspace_repo / "Dockerfile"
    df_path.write_text(df_content, encoding="utf-8")

    result = verify_consistency_gate(
        repo_path=workspace_repo,
        dockerfile_path=df_path,
        compose_path=None,
        blueprint=nextjs_blueprint,
    )
    assert not result.passed
    assert any("does not output to dist" in e for e in result.errors)
