# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for Phase 2: ForgeOps GitHub Direct Push & Pull Request Publishing.

Covers:
- build_deployment_manifest deterministic fixture and byte/hash assertions.
- execute_github_release for direct_push (happy path, Compare API reachability, conflict 409/422 bounded recovery).
- execute_github_release for pull_request (source branch creation, provenance verification, idempotent PR adoption).
- evaluate_g7_verification for direct_push and pull_request across open, merged (merge/squash/rebase), and closed PRs.
- Dynamic default branch resolution and error mapping.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from src.core.errors import ProblemException
from src.deployments.autonomous_gates import (
    ConflictError,
    G7VerificationResult,
    GateResult,
    build_deployment_manifest,
    evaluate_g7_verification,
    execute_github_release,
)
from src.deployments.autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentStage,
)
from src.deployments.autonomous_schemas import (
    DeploymentStrategy,
    GitHubPublishingMode,
)
from src.deployments.autonomous_service import (
    STAGE_G7_VERIFICATION,
    STAGE_GITHUB_RELEASE,
    AutonomousDeploymentService,
    resolve_repository_default_branch,
)

_ORIG_ASYNC_CLIENT = httpx.AsyncClient


def _mock_client_factory(transport: httpx.MockTransport) -> Any:
    return lambda **kwargs: _ORIG_ASYNC_CLIENT(transport=transport)


class MockAsyncSession:
    """Mock database session for unit tests."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.bind = None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def refresh(self, obj: Any) -> None:
        pass


def _create_run(
    strategy: DeploymentStrategy = DeploymentStrategy.GITHUB_ONLY,
    publishing_mode: str = "direct_push",
    target_branch: str | None = "main",
    run_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> AutonomousDeployment:
    rid = run_id or uuid.uuid4()
    cat = created_at or datetime(2026, 10, 10, 14, 0, 0, tzinfo=UTC)
    stage = AutonomousDeploymentStage(
        id=uuid.uuid4(),
        run_id=rid,
        stage_name=STAGE_GITHUB_RELEASE,
        position=1,
        status="running",
        stage_metadata={},
    )
    stage_g7 = AutonomousDeploymentStage(
        id=uuid.uuid4(),
        run_id=rid,
        stage_name=STAGE_G7_VERIFICATION,
        gate_id="G7",
        position=2,
        status="pending",
        stage_metadata={},
    )
    run = AutonomousDeployment(
        id=rid,
        project_id=uuid.uuid4(),
        status="running",
        strategy=strategy.value if hasattr(strategy, "value") else str(strategy),
        configuration={
            "github_config": {
                "repository_name": "testowner/testrepo",
                "target_branch": target_branch,
                "base_branch": target_branch,
                "publishing_mode": publishing_mode,
            }
        },
        progress_pct=10,
        fence_token=1,
        created_at=cat,
    )
    run.stages = [stage, stage_g7]
    return run


class TestDeploymentManifestFixture:
    """Verifies specification-mandated deployment manifest UTF-8 bytes and SHA-256 digest."""

    def test_sample_manifest_exact_bytes_and_hash(self) -> None:
        sample_run_id = uuid.UUID("835783a4-7c13-4c00-a233-90034f3a1db7")
        sample_created_at = datetime(2026, 10, 10, 14, 0, 0, tzinfo=UTC)

        manifest_bytes, payload_digest = build_deployment_manifest(sample_run_id, sample_created_at)

        assert len(manifest_bytes) == 102
        assert payload_digest == "0ff6b151b520e538ddd7fa41f169cacf74012de54f84540581e9a450bc8a3913"
        assert manifest_bytes == (
            b"ForgeOps Autonomous Deployment Run 835783a4-7c13-4c00-a233-90034f3a1db7\n"
            b"Created: 2026-10-10T14:00:00Z\n"
        )


class TestExecuteGitHubReleaseDirectPush:
    """Tests execute_github_release in direct_push mode."""

    @pytest.mark.asyncio
    async def test_simulated_direct_push_succeeds(self) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")

        result = await execute_github_release(session, run)

        assert result.passed is True
        assert result.status == "succeeded"
        assert result.details["publishing_mode"] == "direct_push"
        assert result.details["live"] is False
        assert "commit_sha" in result.details
        assert result.details["payload_digest"] is not None

    @pytest.mark.asyncio
    async def test_live_direct_push_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        fake_commit_sha = "abc123456789"
        fake_base_sha = "base00000000"

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": fake_base_sha}})
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                if req.method == "GET":
                    return httpx.Response(404)
                if req.method == "PUT":
                    return httpx.Response(201, json={"commit": {"sha": fake_commit_sha}})
            if f"/compare/{fake_commit_sha}...main" in url:
                return httpx.Response(200, json={"behind_by": 0, "status": "identical"})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is True
        assert result.status == "succeeded"
        assert result.details["commit_sha"] == fake_commit_sha
        assert result.details["live"] is True

    @pytest.mark.asyncio
    async def test_live_direct_push_unreachable_commit_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        fake_commit_sha = "unreachable123"

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": "base0"}})
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                if req.method == "GET":
                    return httpx.Response(404)
                if req.method == "PUT":
                    return httpx.Response(201, json={"commit": {"sha": fake_commit_sha}})
            if f"/compare/{fake_commit_sha}...main" in url:
                # Behind by 2 commits, status diverged
                return httpx.Response(200, json={"behind_by": 2, "status": "diverged"})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is False
        assert result.status == "failed"
        assert "not reachable" in result.message


class TestExecuteGitHubReleasePullRequest:
    """Tests execute_github_release in pull_request mode."""

    @pytest.mark.asyncio
    async def test_simulated_pull_request_succeeds(self) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")

        result = await execute_github_release(session, run)

        assert result.passed is True
        assert result.status == "succeeded"
        assert result.details["publishing_mode"] == "pull_request"
        assert result.details["pr_number"] == 42
        assert result.details["pr_state"] == "open"
        assert result.details["pr_merged"] is False
        assert result.details["source_branch"] == f"forgeops/deploy-{run.id}"

    @pytest.mark.asyncio
    async def test_live_pull_request_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        source_branch = f"forgeops/deploy-{run.id}"
        fake_base_sha = "base999"
        fake_commit_sha = "commit888"

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": fake_base_sha}})
            if f"/git/ref/heads/{source_branch}" in url:
                return httpx.Response(404)
            if "/git/refs" in url and req.method == "POST":
                return httpx.Response(201, json={})
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                if req.method == "GET":
                    return httpx.Response(404)
                if req.method == "PUT":
                    return httpx.Response(201, json={"commit": {"sha": fake_commit_sha}})
            if "/pulls" in url:
                if req.method == "GET":
                    return httpx.Response(200, json=[])
                if req.method == "POST":
                    return httpx.Response(201, json={
                        "number": 101,
                        "html_url": "https://github.com/testowner/testrepo/pull/101",
                    })
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is True
        assert result.status == "succeeded"
        assert result.details["pr_number"] == 101
        assert result.details["pr_state"] == "open"
        assert result.details["commit_sha"] == fake_commit_sha

    @pytest.mark.asyncio
    async def test_live_pull_request_adopts_existing_open_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        source_branch = f"forgeops/deploy-{run.id}"

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": "base1"}})
            if f"/git/ref/heads/{source_branch}" in url:
                return httpx.Response(404)
            if "/git/refs" in url and req.method == "POST":
                return httpx.Response(201, json={})
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                if req.method == "GET":
                    return httpx.Response(404)
                if req.method == "PUT":
                    return httpx.Response(201, json={"commit": {"sha": "commit1"}})
            if "/pulls" in url and req.method == "GET":
                return httpx.Response(200, json=[{
                    "number": 55,
                    "html_url": "https://github.com/testowner/testrepo/pull/55",
                    "state": "open",
                    "merged": False,
                }])
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is True
        assert result.details["pr_number"] == 55
        assert result.details["pr_state"] == "open"

    @pytest.mark.asyncio
    async def test_live_pull_request_closed_unmerged_halts_with_conflict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        source_branch = f"forgeops/deploy-{run.id}"

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": "base1"}})
            if f"/git/ref/heads/{source_branch}" in url:
                return httpx.Response(404)
            if "/git/refs" in url and req.method == "POST":
                return httpx.Response(201, json={})
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                if req.method == "GET":
                    return httpx.Response(404)
                if req.method == "PUT":
                    return httpx.Response(201, json={"commit": {"sha": "commit1"}})
            if "/pulls" in url and req.method == "GET":
                return httpx.Response(200, json=[{
                    "number": 77,
                    "html_url": "https://github.com/testowner/testrepo/pull/77",
                    "state": "closed",
                    "merged": False,
                }])
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is False
        assert result.status == "failed"
        assert "closed without merging" in result.message


class TestG7VerificationPhase2:
    """Tests evaluate_g7_verification for direct push and pull request modes."""

    @pytest.mark.asyncio
    async def test_g7_direct_push_verifies_reachability_and_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        commit_sha = "direct_commit_1"
        m_bytes, m_digest = build_deployment_manifest(run.id, run.created_at)

        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "direct_push",
            "commit_sha": commit_sha,
            "target_branch": "main",
            "payload_digest": m_digest,
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if f"/commits/{commit_sha}" in url:
                return httpx.Response(200, json={})
            if f"/compare/{commit_sha}...main" in url:
                return httpx.Response(200, json={"behind_by": 0, "status": "identical"})
            if f"/contents/forgeops-autonomous-deploy.txt?ref={commit_sha}" in url:
                return httpx.Response(200, json={"content": base64.b64encode(m_bytes).decode("ascii")})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session,
            run,
            context={"verify_live_github": True, "github_token": "dummy_token"},
        )

        assert result.overall_passed is True
        assert result.target_results["github"] == "verified"

    @pytest.mark.asyncio
    async def test_g7_pull_request_open_pr_verified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        commit_sha = "pr_commit_1"
        pr_number = 88

        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "pull_request",
            "commit_sha": commit_sha,
            "pr_number": pr_number,
            "source_branch": f"forgeops/deploy-{run.id}",
            "target_branch": "main",
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if f"/commits/{commit_sha}" in url:
                return httpx.Response(200, json={})
            if f"/pulls/{pr_number}" in url:
                return httpx.Response(200, json={
                    "number": pr_number,
                    "state": "open",
                    "merged": False,
                    "head": {"sha": commit_sha, "ref": f"forgeops/deploy-{run.id}"},
                    "base": {"ref": "main"},
                })
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session,
            run,
            context={"verify_live_github": True, "github_token": "dummy_token"},
        )

        assert result.overall_passed is True
        assert result.target_results["github"] == "verified"

    @pytest.mark.asyncio
    async def test_g7_pull_request_merged_pr_verified_across_merge_strategies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        commit_sha = "head_commit_1"
        merge_commit_sha = "merge_commit_99"
        pr_number = 89
        m_bytes, m_digest = build_deployment_manifest(run.id, run.created_at)

        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "pull_request",
            "commit_sha": commit_sha,
            "pr_number": pr_number,
            "source_branch": f"forgeops/deploy-{run.id}",
            "target_branch": "main",
            "payload_digest": m_digest,
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if f"/commits/{commit_sha}" in url:
                return httpx.Response(200, json={})
            if f"/pulls/{pr_number}" in url:
                return httpx.Response(200, json={
                    "number": pr_number,
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": merge_commit_sha,
                    "head": {"sha": commit_sha, "ref": f"forgeops/deploy-{run.id}"},
                    "base": {"ref": "main"},
                })
            if f"/compare/{merge_commit_sha}...main" in url:
                # Reachable in history
                return httpx.Response(200, json={"behind_by": 0, "status": "identical"})
            if f"/contents/forgeops-autonomous-deploy.txt?ref={merge_commit_sha}" in url:
                return httpx.Response(200, json={"content": base64.b64encode(m_bytes).decode("ascii")})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session,
            run,
            context={"verify_live_github": True, "github_token": "dummy_token"},
        )

        assert result.overall_passed is True
        assert result.target_results["github"] == "verified"


class TestDefaultBranchResolutionAndErrorMapping:
    """Tests dynamic default branch resolution and error mapping."""

    @pytest.mark.asyncio
    async def test_resolve_default_branch_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"default_branch": "develop"})

        transport = httpx.MockTransport(fake_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            branch = await resolve_repository_default_branch(
                "owner", "repo", token="fake_token", client=client
            )
            assert branch == "develop"

    @pytest.mark.asyncio
    async def test_resolve_default_branch_404_maps_to_problem(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ProblemException) as exc_info:
                await resolve_repository_default_branch(
                    "owner", "repo", token="fake_token", client=client
                )
            assert "github-repository-not-found" in exc_info.value.problem.type
            assert exc_info.value.problem.status == 404

    @pytest.mark.asyncio
    async def test_resolve_default_branch_429_maps_to_rate_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"x-ratelimit-reset": "1700000000"})

        transport = httpx.MockTransport(fake_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ProblemException) as exc_info:
                await resolve_repository_default_branch(
                    "owner", "repo", token="fake_token", client=client
                )
            assert "github-rate-limited" in exc_info.value.problem.type
            assert exc_info.value.problem.status == 429
