# SPDX-License-Identifier: FSL-1.1-ALv2
"""Readiness is scored from the codebase index (phases.md §1.4, design §11.4).

The defect these cases close: `get_project_readiness` built its input from
`projects.settings` — `config_files` was `sorted(settings.keys())` and `manifests` was
`["Dockerfile"] if repo_url else []`, with `"README.md"` substituted when the settings were
empty. So the score described what an operator had typed into the create form. It moved
when the settings changed and stayed still when the repository did, which makes it a number
that looks like a measurement and is not one.

The decisive case here is `test_the_score_rises_when_the_repository_improves`: the SAME
project, the SAME settings, two different scans. A score derived from settings cannot move
between them, so the assertion cannot pass unless the score is derived from the index.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from src.auth.dependencies import require_principal
from src.auth.device_dependencies import require_device
from src.auth.models import UserRole
from src.auth.principal import Principal

from tests.integration.production_app import apply_committed_baseline_env

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

TENANT = uuid.UUID("11112222-3333-4444-5555-666677778888")
USER = uuid.UUID("99990000-1111-2222-3333-444455556666")

MULTI_STAGE_DOCKERFILE = (
    "FROM golang:1.24 AS build\nWORKDIR /src\nRUN go build -o /app ./...\n\n"
    "FROM gcr.io/distroless/static\nCOPY --from=build /app /app\nUSER 65532:65532\n"
    'ENTRYPOINT ["/app"]\n'
)
SINGLE_STAGE_DOCKERFILE = "FROM golang:1.24\nWORKDIR /src\nRUN go build ./...\n"
K8S_DEPLOYMENT = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  replicas: 2\n"
TERRAFORM = 'terraform {\n  backend "s3" {\n    bucket = "state"\n  }\n}\n'


class _StubDevice:
    """What `authenticate_session` returns, reduced to the two fields the index route reads."""

    def __init__(self, project_id: uuid.UUID) -> None:
        self.project_id = project_id
        self.tenant_id = None


def _device(request: Request) -> _StubDevice:
    """A device paired to WHICHEVER project the request names.

    The index route authenticates a DEVICE, not a user: an agent holds a device token plus a client
    certificate and can never satisfy `require_principal`. These tests are about what the SCORE does
    with an index, so the authentication is overridden -- the two-factor requirement and the
    project-scoping refusal are asserted in `test_index_route_device_auth.py`, which is where a
    weaker credential is proved insufficient.

    Adopts the requested project rather than pinning one, because these tests create projects
    dynamically.
    """
    return _StubDevice(uuid.UUID(str(request.path_params["project_id"])))


class _StubDevice:
    """What `authenticate_session` returns, reduced to the two fields the index route reads."""

    def __init__(self, project_id: uuid.UUID) -> None:
        self.project_id = project_id
        self.tenant_id = None


def _device(request: Request) -> _StubDevice:
    """A device paired to WHICHEVER project the request names.

    Adopts the requested project rather than pinning one, because these tests create their projects
    dynamically. That makes the route's project-scoping check a no-op here, which is deliberate: it
    is asserted in `test_index_route_device_auth.py` together with the two-factor refusals.
    """
    return _StubDevice(uuid.UUID(str(request.path_params["project_id"])))


def _principal() -> Principal:
    return Principal.for_user(
        user_id=USER,
        subject="readiness-test",
        email="scorer@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=TENANT,
    )


@pytest_asyncio.fixture
async def readiness_app(monkeypatch: pytest.MonkeyPatch, schema_at_head: str) -> AsyncIterator[Any]:
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    redis_url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
    if redis_url:
        monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app()
    app.dependency_overrides[require_principal] = _principal
    # The index route authenticates a DEVICE, not a user: an agent holds a device token plus a
    # client certificate and can never satisfy `require_principal`. These tests are about what the
    # SCORE does with an index, so the authentication is overridden — the two-factor requirement and
    # the project-scoping refusal are asserted in `test_index_route_device_auth.py`, which is where a
    # weaker credential is proved insufficient.
    app.dependency_overrides[require_device] = _device
    async with LifespanManager(app):
        yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(readiness_app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=readiness_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _file(path: str, content: str, language: str = "yaml") -> dict[str, Any]:
    return {
        "path": path,
        "content_hash": f"{abs(hash((path, content))):064x}"[:64],
        "size_bytes": len(content.encode("utf-8")),
        "last_modified": datetime(2026, 8, 26, 12, 0, tzinfo=UTC).isoformat(),
        "language": language,
        "detection_tier": 2,
        "content": content,
        "redaction_count": 0,
        "symbols_supported": False,
        "chunks": [],
    }


def _report(files: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": datetime(2026, 8, 26, 12, 0, tzinfo=UTC).isoformat(),
        "partial": False,
        "inventory": {
            "languages": sorted({f["language"] for f in files}),
            "manifests": [],
            "config_files": [],
            "entry_points": [],
            "file_count": len(files),
            "total_size_bytes": sum(f["size_bytes"] for f in files),
        },
        "files": files,
        "dependencies": [],
        "inventory_hash": "c" * 64,
        "redaction_count": 0,
    }


PRODUCTION_TREE = [
    _file("Dockerfile", MULTI_STAGE_DOCKERFILE, "dockerfile"),
    _file(".dockerignore", ".git\n", "unknown"),
    _file(".github/workflows/ci.yml", "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n"),
    _file("tests/test_api.py", "def test_ok():\n    assert True\n", "python"),
    _file(".pre-commit-config.yaml", "repos: []\n"),
    _file("k8s/deployment.yaml", K8S_DEPLOYMENT),
    _file("chart/Chart.yaml", "apiVersion: v2\nname: api\n"),
    _file("docker-compose.yml", "services:\n  api:\n    image: api\n"),
    _file(".env.example", "DATABASE_URL=\n", "unknown"),
    _file("config/settings.py", "DEBUG = False\n", "python"),
    _file("SECURITY.md", "# Security\n", "unknown"),
    _file(".gitleaks.toml", "[allowlist]\n", "unknown"),
    _file("go.sum", "example.com/x v1.0.0 h1:abc=\n", "go"),
    _file("infra/main.tf", TERRAFORM, "hcl"),
    _file("infra/.terraform.lock.hcl", 'provider "registry.terraform.io/hashicorp/aws" {}\n', "hcl"),
]

BARE_TREE = [
    _file("main.go", "package main\n\nfunc main() {}\n", "go"),
    _file("README.md", "# Project\n", "unknown"),
]


async def _create(client: AsyncClient, name: str, settings: dict[str, Any] | None = None) -> str:
    response = await client.post(
        "/api/v1/projects",
        json={
            "name": name,
            "path": "/srv/projects/scored",
            "repo_url": "https://github.com/parag8487/ForgeOps",
            "settings": settings or {},
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _index(client: AsyncClient, project_id: str, files: list[dict[str, Any]]) -> None:
    response = await client.post(f"/api/v1/analysis/codebase/{project_id}/index", json=_report(files))
    assert response.status_code == 200, response.text


async def test_a_well_equipped_repository_scores_across_every_category(client: AsyncClient) -> None:
    project_id = await _create(client, "Production ready")
    await _index(client, project_id, PRODUCTION_TREE)

    body = (await client.get(f"/api/v1/projects/{project_id}/readiness")).json()
    assert body["indexed"] is True
    assert body["evaluated_paths"] == len(PRODUCTION_TREE)
    assert body["score"] >= 80, body
    assert body["level"] == "production_ready"
    # Every §1.4 category has evidence in this tree, so none may be zero — a zero here
    # would mean a check that cannot pass.
    assert all(score > 0 for score in body["categories"].values()), body["categories"]
    # The checklist names the indexed path that satisfied each check, which is what makes
    # the number auditable rather than merely displayed.
    dockerfile_checks = [c for c in body["checks"] if c["id"] == "dockerfile_multi_stage"]
    assert dockerfile_checks and dockerfile_checks[0]["passed"] is True
    assert dockerfile_checks[0]["evidence"] == "dockerfile"


async def test_a_bare_repository_scores_low(client: AsyncClient) -> None:
    project_id = await _create(client, "Bare")
    await _index(client, project_id, BARE_TREE)

    body = (await client.get(f"/api/v1/projects/{project_id}/readiness")).json()
    assert body["indexed"] is True
    assert body["score"] < 50, body
    assert body["level"] == "blocked"
    assert body["categories"]["containerization_score"] == 0
    assert body["categories"]["iac_score"] == 0
    assert any("Dockerfile" in r for r in body["recommendations"])


async def test_the_score_rises_when_the_repository_improves(client: AsyncClient) -> None:
    """One project, one unchanged settings blob, two scans.

    This is the case a settings-derived score cannot pass: nothing about the project row
    changes between the two readings.
    """
    project_id = await _create(client, "Improving", settings={"favourite": True})

    await _index(client, project_id, BARE_TREE)
    before = (await client.get(f"/api/v1/projects/{project_id}/readiness")).json()

    await _index(client, project_id, PRODUCTION_TREE)
    after = (await client.get(f"/api/v1/projects/{project_id}/readiness")).json()

    assert after["score"] > before["score"], (before["score"], after["score"])
    assert after["categories"]["containerization_score"] > before["categories"]["containerization_score"]


async def test_a_single_stage_dockerfile_scores_below_a_multi_stage_one(client: AsyncClient) -> None:
    """The check reads the Dockerfile BODY from `file_contents`, not just its path."""
    single = await _create(client, "Single stage")
    multi = await _create(client, "Multi stage")
    await _index(client, single, [_file("Dockerfile", SINGLE_STAGE_DOCKERFILE, "dockerfile")])
    await _index(client, multi, [_file("Dockerfile", MULTI_STAGE_DOCKERFILE, "dockerfile")])

    single_body = (await client.get(f"/api/v1/projects/{single}/readiness")).json()
    multi_body = (await client.get(f"/api/v1/projects/{multi}/readiness")).json()

    assert multi_body["categories"]["containerization_score"] > single_body["categories"]["containerization_score"], (
        single_body["categories"],
        multi_body["categories"],
    )


async def test_ignore_globs_refine_the_evidence_and_cannot_invent_it(client: AsyncClient) -> None:
    """`projects.settings` may REFINE the index and never substitute for it.

    The glob removes the infrastructure directory from consideration, so the IaC category
    falls. No setting can raise a category for which the repository holds no evidence.
    """
    scored = await _create(client, "Unrefined")
    refined = await _create(client, "Refined", settings={"ignore_globs": ["infra/*"]})
    await _index(client, scored, PRODUCTION_TREE)
    await _index(client, refined, PRODUCTION_TREE)

    plain = (await client.get(f"/api/v1/projects/{scored}/readiness")).json()
    filtered = (await client.get(f"/api/v1/projects/{refined}/readiness")).json()

    assert plain["categories"]["iac_score"] > 0
    assert filtered["categories"]["iac_score"] == 0
    assert filtered["evaluated_paths"] < plain["evaluated_paths"]


async def test_the_ingest_stores_a_versioned_analysis_report(client: AsyncClient) -> None:
    """§1.4's report row, written at ingest — the moment the tree is known and hashed.

    `analysis_reports.inventory_hash` is determinism evidence, so a row whose hash came from
    a different scan than its score would not be evidence of anything.
    """
    project_id = await _create(client, "Reported")
    response = await client.post(f"/api/v1/analysis/codebase/{project_id}/index", json=_report(PRODUCTION_TREE))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["inventory_hash"] == "c" * 64

    readiness = (await client.get(f"/api/v1/projects/{project_id}/readiness")).json()
    # The score stored at ingest and the score served on read come from the same engine over
    # the same rows, so they must agree.
    assert result["readiness_score"] == readiness["score"]
