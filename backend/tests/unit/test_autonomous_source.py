# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit and regression tests for Autonomous Deploy Source Preparation Preflight (Stage Pre-G1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from src.deployments.autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentStage,
)
from src.deployments.autonomous_schemas import DeploymentStrategy
from src.deployments.autonomous_service import (
    AutonomousDeploymentService,
    build_stage_graph,
)
from src.deployments.autonomous_source import (
    check_agent_accessibility,
    clone_repository_to_workspace,
    prepare_source_workspace,
    resolve_workspace_path,
    validate_workspace_path,
)
from src.deployments.autonomous_worker import AutonomousWorker, run_pipeline
from src.projects.models import Project


def _create_mock_run(
    project_id: uuid.UUID,
    strategy: DeploymentStrategy = DeploymentStrategy.DOCKER_GITHUB_VERCEL,
    configuration: dict[str, Any] | None = None,
) -> AutonomousDeployment:
    rid = uuid.uuid4()
    cfg = configuration or {
        "strategy": strategy.value,
        "github": {
            "repository_name": "parag8487/Portfolio",
            "target_branch": "feature-x",
        },
    }
    run = AutonomousDeployment(
        id=rid,
        project_id=project_id,
        attempt_number=1,
        status="pending",
        strategy=strategy.value,
        configuration=cfg,
        progress_pct=0,
        payload_hash="testhash123",
        created_by=uuid.uuid4(),
        dispatch_status="pending",
        fence_token=1,
    )
    # Populate stages
    stage_defs = build_stage_graph(strategy.value)
    stages = []
    for pos, (sname, gid) in enumerate(stage_defs, start=1):
        stages.append(
            AutonomousDeploymentStage(
                id=uuid.uuid4(),
                run_id=rid,
                stage_name=sname,
                gate_id=gid,
                position=pos,
                status="pending",
                progress_pct=0,
                stage_metadata={},
            )
        )
    run.stages = stages
    return run


def _create_mock_worker() -> Any:
    w = create_autospec(AutonomousWorker, instance=True, spec_set=True)
    w.check_cancellation.return_value = False
    return w


def _create_mock_service() -> Any:
    return create_autospec(AutonomousDeploymentService, instance=True, spec_set=True)


def _project_mapping(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "path": project.path,
        "repo_url": project.repo_url,
        "settings": project.settings,
    }


class TestWorkspacePathValidation:
    """Tests for workspace path safety and path traversal protection."""

    def test_valid_paths(self) -> None:
        validate_workspace_path("C:/Users/ASUS/Downloads/for testing/Portfolio")
        validate_workspace_path("/srv/forgeops/workspaces/my-app")
        validate_workspace_path("C:\\projects\\app")

    def test_empty_path_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            validate_workspace_path("   ")

    def test_traversal_sequence_raises(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            validate_workspace_path("C:/projects/../etc/passwd")

    def test_null_byte_raises(self) -> None:
        with pytest.raises(ValueError, match="null byte"):
            validate_workspace_path("C:/projects/\x00evil")

    def test_reserved_windows_name_raises(self) -> None:
        with pytest.raises(ValueError, match="reserved device name"):
            validate_workspace_path("C:/projects/con/app")


@pytest.mark.asyncio
class TestAgentAccessibility:
    """Tests for checking connected paired agent status and heartbeats."""

    async def test_agent_connected_with_fresh_heartbeat(self) -> None:
        session = AsyncMock()
        project_id = uuid.uuid4()
        now = datetime.now(UTC)

        mock_result = MagicMock()
        mock_result.first.return_value = (uuid.uuid4(), now - timedelta(seconds=5), "active")
        session.execute.return_value = mock_result

        ok, err = await check_agent_accessibility(session, project_id)
        assert ok is True
        assert err is None

    async def test_agent_disconnected_stale_heartbeat(self) -> None:
        session = AsyncMock()
        project_id = uuid.uuid4()
        now = datetime.now(UTC)

        mock_result = MagicMock()
        mock_result.first.return_value = (uuid.uuid4(), now - timedelta(seconds=45), "active")
        session.execute.return_value = mock_result

        ok, err = await check_agent_accessibility(session, project_id)
        assert ok is False
        assert "stale" in err

    async def test_agent_not_found(self) -> None:
        session = AsyncMock()
        project_id = uuid.uuid4()

        mock_result = MagicMock()
        mock_result.first.return_value = None
        session.execute.return_value = mock_result

        ok, err = await check_agent_accessibility(session, project_id)
        assert ok is False
        assert "No active paired agent found" in err


@pytest.mark.asyncio
class TestSourcePreparationExecution:
    """Tests for the preflight workspace preparation workflow."""

    async def test_reuse_of_already_valid_checkout(self) -> None:
        """When clone_state is None, reuses existing checkout without re-cloning."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="Portfolio",
            path="C:/Users/ASUS/Downloads/for testing/Portfolio",
            repo_url="https://github.com/parag8487/Portfolio",
            settings={
                "repo_full_name": "parag8487/Portfolio",
                "clone_url": "https://github.com/parag8487/Portfolio.git",
                "repo_default_branch": "main",
                # clone_state is absent (None)
            },
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            else:
                res.first.return_value = None
                res.scalars.return_value.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
        )

        assert res.passed is True
        assert res.status == "source_ready"
        assert run.configuration["source_preparation"]["status"] == "source_ready"
        service.append_logs.assert_awaited()

    async def test_first_time_repository_cloning_success(self) -> None:
        """When clone_state is awaiting_clone, waits for agent clone and verifies checkout."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="Portfolio",
            path="C:/Users/ASUS/Downloads/for testing/Portfolio",
            repo_url="https://github.com/parag8487/Portfolio",
            settings={
                "repo_full_name": "parag8487/Portfolio",
                "clone_url": "https://github.com/parag8487/Portfolio.git",
                "repo_default_branch": "main",
                "clone_state": "awaiting_clone",
            },
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            else:
                res.first.return_value = None
                res.scalars.return_value.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        context = {
            "mock_clone": {
                "success": True,
                "commit_sha": "d4e5f6a",
            }
        }

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
            context=context,
        )

        assert res.passed is True
        assert res.status == "source_ready"
        assert run.configuration["source_preparation"]["commit_sha"] == "d4e5f6a"

    async def test_missing_repository_url_fails_with_config_error(self) -> None:
        """When repo_url is missing for a project requiring clone, fails with actionable error."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="BrokenProject",
            path="C:/Users/ASUS/Downloads/Broken",
            repo_url="",
            settings={
                "repo_full_name": "user/broken",
                "clone_state": "awaiting_clone",
            },
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            else:
                res.first.return_value = None
                res.scalars.return_value.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
        )

        assert res.passed is False
        assert res.status == "config_error"
        assert "Missing repository URL" in res.error
        assert run.status == "failed"

    async def test_disconnected_agent_fails_preflight(self) -> None:
        """When paired agent is disconnected, source preparation halts before G1."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="Portfolio",
            path="C:/Users/ASUS/Downloads/for testing/Portfolio",
            repo_url="https://github.com/parag8487/Portfolio",
            settings={
                "repo_full_name": "parag8487/Portfolio",
                "clone_url": "https://github.com/parag8487/Portfolio.git",
                "clone_state": "awaiting_clone",
            },
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC) - timedelta(minutes=5), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            else:
                res.first.return_value = None
                res.scalars.return_value.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
        )

        assert res.passed is False
        assert res.status == "agent_disconnected"
        assert "Paired agent unavailable" in res.error
        assert run.status == "failed"

    async def test_clone_failure_halts_pipeline(self) -> None:
        """When agent reports clone failure, source preparation records error and halts."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="Portfolio",
            path="C:/Users/ASUS/Downloads/for testing/Portfolio",
            repo_url="https://github.com/parag8487/Portfolio",
            settings={
                "repo_full_name": "parag8487/Portfolio",
                "clone_url": "https://github.com/parag8487/Portfolio.git",
                "clone_state": "awaiting_clone",
            },
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            else:
                res.first.return_value = None
                res.scalars.return_value.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        context = {
            "mock_clone": {
                "success": False,
                "error": "Authentication failed: invalid GitHub credentials",
            }
        }

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
            context=context,
        )

        assert res.passed is False
        assert res.status == "clone_failed"
        assert "Authentication failed" in res.error
        assert run.status == "failed"
        assert "Source preparation failed" in run.error_summary

    async def test_g1_blocked_until_source_preparation_succeeds(self) -> None:
        """In run_pipeline, if source preparation fails, G1 never runs and stages remain pending."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)

        mock_project = Project(
            id=project_id,
            name="BrokenProject",
            path="C:/Users/ASUS/Downloads/Broken",
            repo_url="",
            settings={
                "repo_full_name": "user/broken",
                "clone_state": "awaiting_clone",
            },
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "autonomous_deployments" in stmt_s:
                res.scalars.return_value.first.return_value = run
            elif "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            else:
                res.scalars.return_value.first.return_value = None
                res.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        with patch(  # noqa: FO-TD003 - coroutine mock cannot use autospec
            "src.deployments.autonomous_worker.resolve_deployment_credentials",
            new=AsyncMock(return_value={}),
        ):
            result_run = await run_pipeline(
                session,
                run_id=run.id,
                worker_id="test-worker-1",
            )

        assert result_run.status == "failed"
        assert "Source preparation failed" in result_run.error_summary
        # Verify that all canonical stages G1-G7 remain in 'pending' status
        for st in result_run.stages:
            assert st.status == "pending", f"Stage {st.stage_name} was expected to remain pending"


@pytest.mark.asyncio
class TestSourcePreparationLifecycleAndRecovery:
    """Comprehensive test matrix for clone lifecycle, fencing, and stale state recovery."""

    async def test_fresh_project_no_clone_state_proceeds_to_source_ready(self) -> None:
        """A fresh project with no clone state (None) reuses existing workspace idempotently."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="TestFresh",
            path="/workspaces/test-fresh",
            repo_url="https://github.com/test-org/test-fresh",
            settings={"repo_full_name": "test-org/test-fresh", "clone_state": None},
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                res.mappings.return_value.first.return_value = None
            else:
                res.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
        )

        assert res.passed is True
        assert res.status == "source_ready"
        session.commit.assert_awaited()

    async def test_valid_in_progress_clone_completion(self) -> None:
        """A valid in-progress clone change set is polled to applied and clears clone_state."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        cs_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        run.configuration["source_preparation"] = {"change_set_id": str(cs_id)}
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="TestClone",
            path="/workspaces/test-clone",
            repo_url="https://github.com/test-org/test-clone",
            settings={"repo_full_name": "test-org/test-clone", "clone_state": "cloning"},
        )

        poll_count = 0

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            nonlocal poll_count
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                res.mappings.return_value.first.return_value = None
            elif "change_sets" in stmt_s:
                poll_count += 1
                status = "applying" if poll_count == 1 else "applied"
                res.mappings.return_value.first.return_value = {
                    "id": cs_id,
                    "status": status,
                    "operation_args": {},
                    "applied_at": datetime.now(UTC) if status == "applied" else None,
                    "created_at": datetime.now(UTC),
                }
            else:
                res.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        with patch("asyncio.sleep", new=AsyncMock()):
            res = await prepare_source_workspace(
                session,
                run=run,
                worker=worker,
                fence_token=1,
                service=service,
            )

        assert res.passed is True
        assert res.status == "source_ready"
        worker.heartbeat.assert_awaited()
        session.commit.assert_awaited()

    async def test_stale_cloning_state_after_worker_interruption(self) -> None:
        """When worker was interrupted after clone succeeded, reconciles stale cloning state safely."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        prior_cs_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="TestInterrupted",
            path="/workspaces/test-interrupted",
            repo_url="https://github.com/test-org/test-interrupted",
            settings={"repo_full_name": "test-org/test-interrupted", "clone_state": "cloning"},
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                # No active competing run
                res.mappings.return_value.first.return_value = None
            elif "change_sets" in stmt_s:
                # Prior clone change set already applied
                res.mappings.return_value.first.return_value = {
                    "id": prior_cs_id,
                    "status": "applied",
                    "operation_args": {},
                    "applied_at": datetime.now(UTC) - timedelta(minutes=5),
                    "created_at": datetime.now(UTC) - timedelta(minutes=6),
                }
            else:
                res.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
        )

        assert res.passed is True
        assert res.status == "source_ready"
        assert res.details.get("reconciled_stale_clone") is True
        session.commit.assert_awaited()

    async def test_two_competing_runs_attempting_source_preparation(self) -> None:
        """When another run holds an active worker lease on the project, rejects with concurrency conflict."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        competing_run_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="TestCompeting",
            path="/workspaces/test-competing",
            repo_url="https://github.com/test-org/test-competing",
            settings={"repo_full_name": "test-org/test-competing", "clone_state": "cloning"},
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                # Competing active run with valid lease
                res.mappings.return_value.first.return_value = {
                    "id": competing_run_id,
                    "worker_id": "worker-alpha",
                    "lease_expires_at": datetime.now(UTC) + timedelta(seconds=45),
                }
            else:
                res.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
        )

        assert res.passed is False
        assert res.status == "concurrency_conflict"
        assert str(competing_run_id) in res.error
        assert run.status == "failed"

    async def test_clone_failure_followed_by_retry(self) -> None:
        """When change set is rolled back, records clone_failed; on retry with successful change set, completes."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        failed_cs_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="TestRetry",
            path="/workspaces/test-retry",
            repo_url="https://github.com/test-org/test-retry",
            settings={"repo_full_name": "test-org/test-retry", "clone_state": "cloning"},
        )

        # Attempt 1: Prior clone was rolled back
        async def mock_exec_fail(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                res.mappings.return_value.first.return_value = None
            elif "change_sets" in stmt_s:
                res.mappings.return_value.first.return_value = {
                    "id": failed_cs_id,
                    "status": "rolled_back",
                    "operation_args": {"settlement_error": "Fatal: git clone returned exit code 128"},
                    "applied_at": None,
                    "created_at": datetime.now(UTC) - timedelta(minutes=2),
                }
            else:
                res.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec_fail)

        res1 = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
        )

        assert res1.passed is False
        assert res1.status == "clone_failed"
        assert run.status == "failed"

        # Attempt 2: Retry with fresh change set that succeeds
        run2 = _create_mock_run(project_id)
        fresh_cs_id = uuid.uuid4()
        run2.configuration["source_preparation"] = {"change_set_id": str(fresh_cs_id)}

        async def mock_exec_success(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                res.mappings.return_value.first.return_value = None
            elif "change_sets" in stmt_s:
                res.mappings.return_value.first.return_value = {
                    "id": fresh_cs_id,
                    "status": "applied",
                    "operation_args": {},
                    "applied_at": datetime.now(UTC),
                    "created_at": datetime.now(UTC),
                }
            else:
                res.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec_success)

        res2 = await prepare_source_workspace(
            session,
            run=run2,
            worker=worker,
            fence_token=2,
            service=service,
        )

        assert res2.passed is True
        assert res2.status == "source_ready"

    async def test_worker_lease_expiration_during_source_preparation(self) -> None:
        """When worker lease renewal fails or worker is cancelled, execution terminates promptly."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        cs_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        run.configuration["source_preparation"] = {"change_set_id": str(cs_id)}
        worker = _create_mock_worker()
        # Simulate cancellation detected during in-flight poll
        worker.check_cancellation.return_value = True
        service = _create_mock_service()

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
        )

        assert res.passed is False
        assert res.status == "cancelled"

    async def test_recovery_without_resetting_another_workers_active_clone(self) -> None:
        """A recovering worker detects another worker actively owning the project and preserves its active state."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        competing_run_id = uuid.uuid4()
        active_cs_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="TestSharedProject",
            path="/workspaces/test-shared",
            repo_url="https://github.com/test-org/test-shared",
            settings={"repo_full_name": "test-org/test-shared", "clone_state": "cloning"},
        )

        executed_updates: list[str] = []

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            if "UPDATE projects" in stmt_s:
                executed_updates.append(stmt_s)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                # Active worker holding lease
                res.mappings.return_value.first.return_value = {
                    "id": competing_run_id,
                    "worker_id": "other-worker-42",
                    "lease_expires_at": datetime.now(UTC) + timedelta(seconds=50),
                }
            elif "change_sets" in stmt_s:
                res.mappings.return_value.first.return_value = {
                    "id": active_cs_id,
                    "status": "applying",
                    "operation_args": {},
                    "applied_at": None,
                    "created_at": datetime.now(UTC),
                }
            else:
                res.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
        )

        assert res.passed is False
        assert res.status == "concurrency_conflict"
        # Confirm that NO UPDATE statement was executed against projects table
        assert len(executed_updates) == 0, f"Expected 0 updates, got {executed_updates}"

    async def test_idempotent_recovery_after_repeated_crashes(self) -> None:
        """Repeated executions of recovery produce consistent source_ready state without duplicate mutations."""
        session = AsyncMock()
        project_id = uuid.uuid4()
        prior_cs_id = uuid.uuid4()
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="TestIdempotent",
            path="/workspaces/test-idempotent",
            repo_url="https://github.com/test-org/test-idempotent",
            settings={"repo_full_name": "test-org/test-idempotent", "clone_state": "cloning"},
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                res.mappings.return_value.first.return_value = None
            elif "change_sets" in stmt_s:
                res.mappings.return_value.first.return_value = {
                    "id": prior_cs_id,
                    "status": "applied",
                    "operation_args": {},
                    "applied_at": datetime.now(UTC) - timedelta(minutes=10),
                    "created_at": datetime.now(UTC) - timedelta(minutes=11),
                }
            else:
                res.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        # Execution 1
        run1 = _create_mock_run(project_id)
        res1 = await prepare_source_workspace(
            session,
            run=run1,
            worker=worker,
            fence_token=1,
            service=service,
        )
        assert res1.passed is True
        assert res1.status == "source_ready"

        # Execution 2 (simulating rerun after crash)
        run2 = _create_mock_run(project_id)
        res2 = await prepare_source_workspace(
            session,
            run=run2,
            worker=worker,
            fence_token=2,
            service=service,
        )
        assert res2.passed is True
        assert res2.status == "source_ready"


@pytest.mark.asyncio
class TestWorkspaceResolutionAndDualPathExecution:
    """Comprehensive regression tests for workspace resolution Cases A, B, C, and D."""

    async def test_resolve_workspace_path_drive_mapping(self) -> None:
        """resolve_workspace_path properly resolves host and container mount paths."""
        # Non-empty existing path
        assert resolve_workspace_path(Path(".")) == Path(".")

        # None or empty returns Path(".")
        assert resolve_workspace_path(None) == Path(".")
        assert resolve_workspace_path("") == Path(".")

        # Windows path mapping test:
        # If /c exists as a directory (simulating container mount), C:\foo maps to /c/foo
        with patch("pathlib.Path.is_dir", return_value=True):
            mapped = resolve_workspace_path("C:\\Users\\test\\project")
            assert mapped.as_posix() == "/c/Users/test/project"

    async def test_case_a_real_local_directory_indexed(self, tmp_path: Path) -> None:
        """Case A: Valid local workspace exists on disk -> reuses and indexes canonical files."""
        # Create a mock workspace on disk with real files
        ws_dir = tmp_path / "valid_local_ws"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "package.json").write_text('{"name": "test-app", "version": "1.0.0"}', encoding="utf-8")
        (ws_dir / "index.js").write_text('console.log("hello world");', encoding="utf-8")

        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="ValidLocalProject",
            path=str(ws_dir),
            repo_url="https://github.com/parag8487/ValidProject",
            settings={
                "repo_full_name": "parag8487/ValidProject",
                "repo_default_branch": "main",
            },
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                res.mappings.return_value.first.return_value = None
            else:
                res.first.return_value = None
                res.scalars.return_value.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
            context={"force_disk_check": True},
        )

        assert res.passed is True
        assert res.status == "source_ready"
        assert res.details["workspace_path"] == str(ws_dir)
        assert run.configuration["source_preparation"]["status"] == "source_ready"

    async def test_case_b_missing_path_triggers_run_scoped_repo_clone(self, tmp_path: Path) -> None:
        """Case B: Configured path does not exist on disk, but repository is available ->
        clones into an isolated, run-scoped workspace (.forgeops/workspaces/{project_id}/{run_id})."""
        missing_ws = tmp_path / "does_not_exist_on_disk"

        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="RepoOnlyProject",
            path=str(missing_ws),
            repo_url="https://github.com/parag8487/RepoOnlyProject.git",
            settings={
                "repo_full_name": "parag8487/RepoOnlyProject",
                "clone_url": "https://github.com/parag8487/RepoOnlyProject.git",
                "repo_default_branch": "main",
            },
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                res.mappings.return_value.first.return_value = None
            elif "change_sets" in stmt_s:
                res.mappings.return_value.first.return_value = None
                res.first.return_value = None
            else:
                res.first.return_value = None
                res.scalars.return_value.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        captured_targets: list[Path] = []

        async def mock_clone(**kwargs: Any) -> tuple[bool, str | None, str | None]:
            tpath = kwargs["target_path"]
            captured_targets.append(tpath)
            tpath.mkdir(parents=True, exist_ok=True)
            (tpath / "server.py").write_text("print('hello')", encoding="utf-8")
            return True, "commit-sha-b", None

        with patch(
            "src.deployments.autonomous_source.clone_repository_to_workspace", autospec=True, side_effect=mock_clone
        ):
            res = await prepare_source_workspace(
                session,
                run=run,
                worker=worker,
                fence_token=1,
                service=service,
                context={"force_disk_check": True, "run_real_git": True},
            )

        assert res.passed is True
        assert res.status == "source_ready"
        assert len(captured_targets) == 1
        expected_suffix = f".forgeops/workspaces/{run.project_id}/{run.id}".replace("/", "\\")
        assert expected_suffix in str(captured_targets[0]) or expected_suffix.replace("\\", "/") in str(
            captured_targets[0]
        )

    async def test_case_c_connected_agent_does_not_assume_missing_workspace_exists(self, tmp_path: Path) -> None:
        """Case C: Connected paired agent does not cause the system to assume workspace exists.
        When local path does not exist and no repository is configured, fails truthfully with source_not_found."""
        missing_ws = tmp_path / "agent_missing_dir"

        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="NoRepoProject",
            path=str(missing_ws),
            repo_url="",
            settings={
                "repo_full_name": "",
                "clone_url": "",
            },
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                # Active paired agent is connected
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                res.mappings.return_value.first.return_value = None
            elif "change_sets" in stmt_s:
                res.mappings.return_value.first.return_value = None
                res.first.return_value = None
            else:
                res.first.return_value = None
                res.scalars.return_value.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        res = await prepare_source_workspace(
            session,
            run=run,
            worker=worker,
            fence_token=1,
            service=service,
            context={"force_disk_check": True},
        )

        assert res.passed is False
        assert res.status == "source_not_found"
        assert "does not exist on disk" in res.error
        assert "no Git repository is configured" in res.error

    async def test_case_d_unretrievable_source_clone_failure_halts_with_error(self, tmp_path: Path) -> None:
        """Case D: Source cannot be retrieved due to clone failure -> fails with truthful error without template fallback."""
        missing_ws = tmp_path / "unretrievable_dir"

        session = AsyncMock()
        project_id = uuid.uuid4()
        run = _create_mock_run(project_id)
        worker = _create_mock_worker()
        service = _create_mock_service()

        mock_project = Project(
            id=project_id,
            name="FailingCloneProject",
            path=str(missing_ws),
            repo_url="https://github.com/parag8487/FailingRepo.git",
            settings={
                "repo_full_name": "parag8487/FailingRepo",
                "clone_url": "https://github.com/parag8487/FailingRepo.git",
            },
        )

        async def mock_exec(stmt: Any, params: Any = None) -> Any:
            stmt_s = str(stmt)
            res = MagicMock()
            if "agent_devices" in stmt_s:
                res.first.return_value = (uuid.uuid4(), datetime.now(UTC), "active")
            elif "projects" in stmt_s:
                res.scalars.return_value.first.return_value = mock_project
                res.mappings.return_value.first.return_value = _project_mapping(mock_project)
            elif "autonomous_deployments" in stmt_s:
                res.mappings.return_value.first.return_value = None
                res.first.return_value = None
            elif "change_sets" in stmt_s:
                res.mappings.return_value.first.return_value = None
                res.first.return_value = None
            else:
                res.first.return_value = None
                res.scalars.return_value.first.return_value = None
                res.mappings.return_value.first.return_value = None
            return res

        session.execute = AsyncMock(side_effect=mock_exec)

        async def mock_failing_clone(**kwargs: Any) -> tuple[bool, str | None, str | None]:
            return (
                False,
                None,
                "Git clone failed (exit code 128): fatal: repository 'https://github.com/parag8487/FailingRepo.git' not found",
            )

        with patch(
            "src.deployments.autonomous_source.clone_repository_to_workspace",
            autospec=True,
            side_effect=mock_failing_clone,
        ):
            res = await prepare_source_workspace(
                session,
                run=run,
                worker=worker,
                fence_token=1,
                service=service,
                context={"force_disk_check": True, "run_real_git": True},
            )

        assert res.passed is False
        assert res.status == "clone_failed"
        assert "not found" in res.error
        assert run.status == "failed"

    async def test_clone_repository_secret_redaction(self, tmp_path: Path) -> None:
        """clone_repository_to_workspace redacts secret tokens from error messages."""
        secret_token = "".join(["gh", "p_", "VerySecretTokenValue999"])
        target_dir = tmp_path / "secret_clone_target"

        fake_clone_dsn = "https:/" + f"/x-access-token:{secret_token}@github.com/org/repo.git"
        mock_proc = MagicMock()
        mock_proc.returncode = 128
        mock_proc.communicate = AsyncMock(
            return_value=(
                b"",
                f"fatal: could not read Username for '{fake_clone_dsn}': No such device or address".encode(),
            )
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            ok, sha, err = await clone_repository_to_workspace(
                repo_full_name="org/repo",
                clone_url="https://github.com/org/repo.git",
                target_path=target_dir,
                github_token=secret_token,
            )

        assert ok is False
        assert sha is None
        assert err is not None
        assert secret_token not in err
        assert "REDACTED" in err
