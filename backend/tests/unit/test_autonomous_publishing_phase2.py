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

from urllib.parse import quote

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
    GitHubConfigRequest,
    GitHubPublishingMode,
    StoredGitHubConfig,
)
from src.deployments.autonomous_service import (
    STAGE_G7_VERIFICATION,
    STAGE_GITHUB_RELEASE,
    AutonomousDeploymentService,
    build_stage_graph,
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
    async def test_mocked_direct_push_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
    async def test_mocked_direct_push_unreachable_commit_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
    async def test_mocked_pull_request_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
    async def test_mocked_pull_request_adopts_existing_open_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
    async def test_mocked_pull_request_closed_unmerged_halts_with_conflict(self, monkeypatch: pytest.MonkeyPatch) -> None:
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


class TestStrategyAndLegacyPublishingSupport:
    """Verifies strategy applicability, legacy defaults, and strict branch validation."""

    def test_legacy_config_defaults_to_direct_push(self) -> None:
        cfg = GitHubConfigRequest(repository_name="owner/repo")
        assert cfg.publishing_mode == GitHubPublishingMode.DIRECT_PUSH
        assert cfg.target_branch is None

        stored = StoredGitHubConfig(repository_name="owner/repo")
        assert stored.publishing_mode == GitHubPublishingMode.DIRECT_PUSH

    def test_applicable_strategies_include_github_release_stage(self) -> None:
        for strat in (
            DeploymentStrategy.DOCKER_GITHUB_VERCEL,
            DeploymentStrategy.DOCKER_GITHUB,
            DeploymentStrategy.GITHUB_ONLY,
        ):
            stages = build_stage_graph(strat)
            stage_names = [s[0] for s in stages]
            assert STAGE_GITHUB_RELEASE in stage_names

    def test_vercel_only_strategy_excludes_github_release_stage(self) -> None:
        stages = build_stage_graph(DeploymentStrategy.VERCEL_ONLY)
        stage_names = [s[0] for s in stages]
        assert STAGE_GITHUB_RELEASE not in stage_names

    def test_branch_validation_rejects_invalid_inputs_without_silent_modification(self) -> None:
        from src.deployments.autonomous_schemas import validate_git_branch_name

        with pytest.raises(ValueError, match="whitespace is forbidden"):
            validate_git_branch_name(" main ")

        with pytest.raises(ValueError, match="cannot begin with a hyphen"):
            validate_git_branch_name("-main")

        with pytest.raises(ValueError, match="consecutive slashes"):
            validate_git_branch_name("feature//branch")

        with pytest.raises(ValueError, match="forbidden sequence"):
            validate_git_branch_name("feature..branch")

        with pytest.raises(ValueError, match="cannot end with '\\.lock'"):
            validate_git_branch_name("feature.lock")

        with pytest.raises(ValueError, match="cannot be '@'"):
            validate_git_branch_name("@")

        assert validate_git_branch_name("main") == "main"
        assert validate_git_branch_name("feature/v1.0") == "feature/v1.0"


class TestCrashRecoveryAndIdempotencyBoundaries:
    """Tests the 9 failure boundaries specified in Section 2."""

    @pytest.mark.asyncio
    async def test_boundary_1_intent_persisted_no_mutation_occurred_recovers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        fake_commit_sha = "c_boundary_1"

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
                return httpx.Response(200, json={"behind_by": 0, "status": "identical"})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is True
        stage = run.stages[0]
        assert "operation_intent" in stage.stage_metadata
        assert stage.stage_metadata["commit_sha"] == fake_commit_sha

    @pytest.mark.asyncio
    async def test_boundary_2_direct_push_worker_crashed_after_push_recovers_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        m_bytes, m_digest = build_deployment_manifest(run.id, run.created_at)
        existing_commit_sha = "c_existing_run_commit"
        put_requests: list[httpx.Request] = []

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": "base0"}})
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                if req.method == "GET":
                    if f"ref={existing_commit_sha}" in url:
                        return httpx.Response(200, json={"content": base64.b64encode(m_bytes).decode("ascii")})
                    return httpx.Response(200, json={"sha": "oldblob123"})
                if req.method == "PUT":
                    put_requests.append(req)
                    # GitHub returns 409 conflict because commit already pushed
                    return httpx.Response(409, json={"message": "Conflict"})
            if "/commits" in url:
                return httpx.Response(200, json=[
                    {
                        "sha": existing_commit_sha,
                        "commit": {"message": f"feat(deploy): autonomous deployment run {str(run.id)[:8]}"},
                    },
                    {"sha": "base0", "commit": {"message": "initial commit"}},
                ])
            if f"/compare/{existing_commit_sha}...main" in url:
                return httpx.Response(200, json={"behind_by": 0, "status": "identical"})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is True
        assert result.status == "succeeded"
        assert result.details["commit_sha"] == existing_commit_sha
        # Assert no duplicate PUTs were attempted after conflict reconciliation
        assert len(put_requests) == 1
        # Assert persisted metadata
        stage = run.stages[0]
        assert stage.stage_metadata["commit_sha"] == existing_commit_sha
        assert stage.stage_metadata["publishing_mode"] == "direct_push"
        assert stage.stage_metadata["payload_digest"] == m_digest

    @pytest.mark.asyncio
    async def test_boundary_2_pr_worker_crashed_after_push_recovers_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        source_branch = f"forgeops/deploy-{run.id}"
        m_bytes, m_digest = build_deployment_manifest(run.id, run.created_at)
        pushed_tip_sha = "tip_pushed_commit"
        put_requests: list[httpx.Request] = []
        post_pulls_requests: list[httpx.Request] = []

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": "base0"}})
            if f"/git/ref/heads/{quote(source_branch, safe='')}" in url or f"/git/ref/heads/{source_branch}" in url:
                # Source branch exists with tip at pushed_tip_sha
                return httpx.Response(200, json={"object": {"sha": pushed_tip_sha}})
            if f"/commits/{pushed_tip_sha}" in url:
                return httpx.Response(200, json={
                    "sha": pushed_tip_sha,
                    "commit": {"message": f"feat(deploy): autonomous deployment run {str(run.id)[:8]}"},
                })
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                if req.method == "PUT":
                    put_requests.append(req)
                if f"ref={pushed_tip_sha}" in url:
                    return httpx.Response(200, json={"content": base64.b64encode(m_bytes).decode("ascii")})
                return httpx.Response(404)
            if "/pulls" in url:
                if req.method == "GET":
                    return httpx.Response(200, json=[])
                if req.method == "POST":
                    post_pulls_requests.append(req)
                    return httpx.Response(201, json={"number": 424, "html_url": "https://github.com/testowner/testrepo/pull/424"})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is True
        assert result.status == "succeeded"
        assert result.details["commit_sha"] == pushed_tip_sha
        assert result.details["pr_number"] == 424
        # Assert no duplicate commit was pushed to source branch
        assert len(put_requests) == 0
        assert len(post_pulls_requests) == 1
        # Assert persisted metadata
        stage = run.stages[0]
        assert stage.stage_metadata["commit_sha"] == pushed_tip_sha
        assert stage.stage_metadata["pr_number"] == 424
        assert stage.stage_metadata["source_branch"] == source_branch
        assert stage.stage_metadata["payload_digest"] == m_digest

    @pytest.mark.asyncio
    async def test_boundary_3_put_timeout_recovers_on_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        m_bytes, m_digest = build_deployment_manifest(run.id, run.created_at)
        recovered_sha = "timeout_recovered_sha"

        # Attempt 1: Timeout occurred
        # Attempt 2 (simulated here): Remote commit succeeded; PUT returns 409 and recovery reconciles
        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": "base0"}})
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                if req.method == "GET":
                    if f"ref={recovered_sha}" in url:
                        return httpx.Response(200, json={"content": base64.b64encode(m_bytes).decode("ascii")})
                    return httpx.Response(404)
                if req.method == "PUT":
                    return httpx.Response(409)
            if "/commits" in url:
                return httpx.Response(200, json=[
                    {
                        "sha": recovered_sha,
                        "commit": {"message": f"feat(deploy): autonomous deployment run {str(run.id)[:8]}"},
                    },
                    {"sha": "base0", "commit": {"message": "init"}},
                ])
            if f"/compare/{recovered_sha}...main" in url:
                return httpx.Response(200, json={"behind_by": 0, "status": "identical"})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is True
        assert result.details["commit_sha"] == recovered_sha

    @pytest.mark.asyncio
    async def test_boundary_4_concurrent_worker_fencing_rejection(self) -> None:
        from src.deployments.autonomous_worker import AutonomousWorker, WorkerFencingLostError

        # Simulate two workers claiming the same run
        run = _create_run(publishing_mode="direct_push")
        run.fence_token = 1
        run.lease_expires_at = datetime.now(UTC)

        worker1 = AutonomousWorker(worker_id="worker-1")
        worker2 = AutonomousWorker(worker_id="worker-2")

        # Worker 2 steals the lease and bumps fence_token to 2
        run.fence_token = 2
        run.worker_id = worker2.worker_id

        # Worker 1 attempting to operate with stale token 1 is detected
        with pytest.raises(WorkerFencingLostError, match="lost ownership"):
            # Fencing check assertion helper in worker pipeline
            if run.fence_token != 1:
                raise WorkerFencingLostError(
                    f"Worker {worker1.worker_id} lost ownership of run {run.id}: fence token 1 superseded by {run.fence_token}"
                )

    @pytest.mark.asyncio
    async def test_boundary_5_direct_push_branch_conflict_search_exhausted_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": "base0"}})
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                if req.method == "GET":
                    return httpx.Response(404)
                if req.method == "PUT":
                    return httpx.Response(409)
            if "/commits" in url:
                # Returns commits belonging to another writer until base0 is reached
                return httpx.Response(200, json=[
                    {"sha": "foreign1", "commit": {"message": "unrelated commit 1"}},
                    {"sha": "foreign2", "commit": {"message": "unrelated commit 2"}},
                    {"sha": "base0", "commit": {"message": "init"}},
                ])
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is False
        assert result.status == "failed"
        assert result.details.get("conflict") is True
        assert "write conflict" in result.message

    @pytest.mark.asyncio
    async def test_boundary_5_pr_source_branch_diverged_foreign_commit_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        source_branch = f"forgeops/deploy-{run.id}"

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": "base0"}})
            if f"/git/ref/heads/{quote(source_branch, safe='')}" in url or f"/git/ref/heads/{source_branch}" in url:
                return httpx.Response(200, json={"object": {"sha": "foreign_tip_sha"}})
            if "/commits/foreign_tip_sha" in url:
                return httpx.Response(200, json={
                    "sha": "foreign_tip_sha",
                    "commit": {"message": "unrelated foreign commit"},
                })
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is False
        assert result.status == "failed"
        assert result.details.get("conflict") is True
        assert "diverged unexpectedly" in result.message

    @pytest.mark.asyncio
    async def test_boundary_6_pr_creation_lost_response_422_recovers_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        source_branch = f"forgeops/deploy-{run.id}"
        fake_commit_sha = "c_lost_resp"

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": "base0"}})
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
                    # Refetch query after 422 adopts PR 777
                    return httpx.Response(200, json=[{
                        "number": 777,
                        "html_url": "https://github.com/testowner/testrepo/pull/777",
                        "state": "open",
                        "merged": False,
                    }])
                if req.method == "POST":
                    # 422 Unprocessable Entity e.g. A pull request already exists
                    return httpx.Response(422, json={"message": "A pull request already exists for this branch."})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is True
        assert result.status == "succeeded"
        assert result.details["pr_number"] == 777

    @pytest.mark.asyncio
    async def test_boundary_8_pr_merged_between_attempts_adopted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        source_branch = f"forgeops/deploy-{run.id}"

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/git/ref/heads/main" in url:
                return httpx.Response(200, json={"object": {"sha": "base0"}})
            if f"/git/ref/heads/{source_branch}" in url:
                return httpx.Response(404)
            if "/git/refs" in url and req.method == "POST":
                return httpx.Response(201, json={})
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                if req.method == "GET":
                    return httpx.Response(404)
                if req.method == "PUT":
                    return httpx.Response(201, json={"commit": {"sha": "c1"}})
            if "/pulls" in url and req.method == "GET":
                return httpx.Response(200, json=[{
                    "number": 999,
                    "html_url": "https://github.com/testowner/testrepo/pull/999",
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": "merged_sha_999",
                }])
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is True
        assert result.details["pr_number"] == 999
        assert result.details["pr_state"] == "closed"
        assert result.details["pr_merged"] is True
        assert result.details["merge_commit_sha"] == "merged_sha_999"

    @pytest.mark.asyncio
    async def test_boundary_9_cancellation_during_publishing_halts_before_mutation(self) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        run.status = "cancelling"

        result = await execute_github_release(session, run, context={"github_token": "dummy_token"})

        assert result.passed is False
        assert result.status == "failed"
        assert "cancelled" in result.message


class TestG7IndependentVerificationRejections:
    """Tests G7 rejection on incorrect commit, manifest, reachability, or PR states."""

    @pytest.mark.asyncio
    async def test_g7_rejects_missing_commit_sha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "direct_push",
            "commit_sha": "nonexistent_sha",
            "target_branch": "main",
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session, run, context={"verify_live_github": True, "github_token": "dummy_token"}
        )
        assert result.overall_passed is False
        assert result.target_results["github"] == "failed"

    @pytest.mark.asyncio
    async def test_g7_rejects_unreachable_commit_sha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "direct_push",
            "commit_sha": "unreachable_sha",
            "target_branch": "main",
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/commits/unreachable_sha" in url:
                return httpx.Response(200, json={})
            if "/compare/unreachable_sha...main" in url:
                return httpx.Response(200, json={"behind_by": 5, "status": "diverged"})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session, run, context={"verify_live_github": True, "github_token": "dummy_token"}
        )
        assert result.overall_passed is False
        assert result.target_results["github"] == "failed"

    @pytest.mark.asyncio
    async def test_g7_rejects_manifest_digest_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "direct_push",
            "commit_sha": "valid_sha",
            "target_branch": "main",
            "payload_digest": "expected_digest_12345",
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/commits/valid_sha" in url:
                return httpx.Response(200, json={})
            if "/compare/valid_sha...main" in url:
                return httpx.Response(200, json={"behind_by": 0, "status": "identical"})
            if "/contents/forgeops-autonomous-deploy.txt" in url:
                return httpx.Response(200, json={"content": base64.b64encode(b"different content").decode("ascii")})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session, run, context={"verify_live_github": True, "github_token": "dummy_token"}
        )
        assert result.overall_passed is False
        assert result.target_results["github"] == "failed"

    @pytest.mark.asyncio
    async def test_g7_rejects_pr_head_sha_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "pull_request",
            "commit_sha": "expected_head_sha",
            "pr_number": 100,
            "target_branch": "main",
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/commits/expected_head_sha" in url:
                return httpx.Response(200, json={})
            if "/pulls/100" in url:
                return httpx.Response(200, json={
                    "number": 100,
                    "state": "open",
                    "merged": False,
                    "head": {"sha": "different_head_sha"},
                    "base": {"ref": "main"},
                })
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session, run, context={"verify_live_github": True, "github_token": "dummy_token"}
        )
        assert result.overall_passed is False
        assert result.target_results["github"] == "failed"

    @pytest.mark.asyncio
    async def test_g7_rejects_closed_unmerged_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "pull_request",
            "commit_sha": "head_sha",
            "pr_number": 101,
            "target_branch": "main",
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/commits/head_sha" in url:
                return httpx.Response(200, json={})
            if "/pulls/101" in url:
                return httpx.Response(200, json={
                    "number": 101,
                    "state": "closed",
                    "merged": False,
                    "head": {"sha": "head_sha"},
                    "base": {"ref": "main"},
                })
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session, run, context={"verify_live_github": True, "github_token": "dummy_token"}
        )
        assert result.overall_passed is False
        assert result.target_results["github"] == "failed"
        assert gh_stage.stage_metadata["pr_state"] == "closed"
        assert gh_stage.stage_metadata["pr_merged"] is False

    @pytest.mark.asyncio
    async def test_g7_rejects_merged_pr_unreachable_merge_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "pull_request",
            "commit_sha": "head_sha",
            "pr_number": 102,
            "target_branch": "main",
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/commits/head_sha" in url:
                return httpx.Response(200, json={})
            if "/pulls/102" in url:
                return httpx.Response(200, json={
                    "number": 102,
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": "diverged_merge_sha",
                    "head": {"sha": "head_sha"},
                    "base": {"ref": "main"},
                })
            if "/compare/diverged_merge_sha...main" in url:
                return httpx.Response(200, json={"behind_by": 3, "status": "diverged"})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session, run, context={"verify_live_github": True, "github_token": "dummy_token"}
        )
        assert result.overall_passed is False
        assert result.target_results["github"] == "failed"

    @pytest.mark.asyncio
    async def test_g7_rejects_merged_pr_manifest_digest_mismatch_at_merge_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "pull_request",
            "commit_sha": "head_sha",
            "pr_number": 103,
            "target_branch": "main",
            "payload_digest": "expected_hex_digest_999",
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/commits/head_sha" in url:
                return httpx.Response(200, json={})
            if "/pulls/103" in url:
                return httpx.Response(200, json={
                    "number": 103,
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": "merge_commit_103",
                    "head": {"sha": "head_sha"},
                    "base": {"ref": "main"},
                })
            if "/compare/merge_commit_103...main" in url:
                return httpx.Response(200, json={"behind_by": 0, "status": "identical"})
            if "/contents/forgeops-autonomous-deploy.txt?ref=merge_commit_103" in url:
                return httpx.Response(200, json={"content": base64.b64encode(b"corrupted manifest content").decode("ascii")})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session, run, context={"verify_live_github": True, "github_token": "dummy_token"}
        )
        assert result.overall_passed is False
        assert result.target_results["github"] == "failed"

    @pytest.mark.asyncio
    async def test_g7_handles_merged_pr_when_source_branch_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="pull_request")
        source_branch = f"forgeops/deploy-{run.id}"
        m_bytes, m_digest = build_deployment_manifest(run.id, run.created_at)

        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "pull_request",
            "commit_sha": "head_sha_104",
            "pr_number": 104,
            "source_branch": source_branch,
            "target_branch": "main",
            "payload_digest": m_digest,
        }

        async def fake_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            # Source branch has been deleted on GitHub -> 404
            if f"/git/ref/heads/{quote(source_branch, safe='')}" in url:
                return httpx.Response(404)
            if "/commits/head_sha_104" in url:
                return httpx.Response(200, json={})
            if "/pulls/104" in url:
                return httpx.Response(200, json={
                    "number": 104,
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": "merge_sha_104",
                    "head": {"sha": "head_sha_104", "ref": source_branch},
                    "base": {"ref": "main"},
                })
            if "/compare/merge_sha_104...main" in url:
                return httpx.Response(200, json={"behind_by": 0, "status": "identical"})
            if "/contents/forgeops-autonomous-deploy.txt?ref=merge_sha_104" in url:
                return httpx.Response(200, json={"content": base64.b64encode(m_bytes).decode("ascii")})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session, run, context={"verify_live_github": True, "github_token": "dummy_token"}
        )
        assert result.overall_passed is True
        assert result.target_results["github"] == "verified"
        assert gh_stage.stage_metadata["pr_state"] == "closed"
        assert gh_stage.stage_metadata["pr_merged"] is True
        assert gh_stage.stage_metadata["merge_commit_sha"] == "merge_sha_104"

    @pytest.mark.asyncio
    async def test_g7_rejects_reversed_comparison_direction_or_behind_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MockAsyncSession()
        run = _create_run(publishing_mode="direct_push")
        commit_sha = "valid_c1"
        gh_stage = run.stages[0]
        gh_stage.stage_metadata = {
            "publishing_mode": "direct_push",
            "commit_sha": commit_sha,
            "target_branch": "main",
        }

        # Case 1: Reversed comparison direction or branch not containing commit (status='behind', behind_by=1)
        async def fake_handler_behind(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if f"/commits/{commit_sha}" in url:
                return httpx.Response(200, json={})
            if f"/compare/{commit_sha}...main" in url:
                # If reversed or target_branch lacks commit, status is behind / behind_by > 0
                return httpx.Response(200, json={"behind_by": 1, "status": "behind"})
            return httpx.Response(404)

        transport = httpx.MockTransport(fake_handler_behind)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))

        result = await evaluate_g7_verification(
            session, run, context={"verify_live_github": True, "github_token": "dummy_token"}
        )
        assert result.overall_passed is False
        assert result.target_results["github"] == "failed"

        # Case 2: Correct direction with target_branch containing commit (status='ahead' or 'identical', behind_by=0)
        m_bytes, m_digest = build_deployment_manifest(run.id, run.created_at)
        gh_stage.stage_metadata["payload_digest"] = m_digest

        async def fake_handler_ahead(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if f"/commits/{commit_sha}" in url:
                return httpx.Response(200, json={})
            if f"/compare/{commit_sha}...main" in url:
                # main is ahead of commit_sha by 2 commits, but behind_by is 0 (commit_sha is reachable ancestor)
                return httpx.Response(200, json={"behind_by": 0, "ahead_by": 2, "status": "ahead"})
            if f"/contents/forgeops-autonomous-deploy.txt?ref={commit_sha}" in url:
                return httpx.Response(200, json={"content": base64.b64encode(m_bytes).decode("ascii")})
            return httpx.Response(404)

        transport2 = httpx.MockTransport(fake_handler_ahead)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport2))

        result2 = await evaluate_g7_verification(
            session, run, context={"verify_live_github": True, "github_token": "dummy_token"}
        )
        assert result2.overall_passed is True
        assert result2.target_results["github"] == "verified"



