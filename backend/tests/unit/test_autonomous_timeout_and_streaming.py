# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit regression tests for configurable ARQ timeout, worker cancellation, fencing, and incremental G4 streaming."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.core.config import Settings
from src.deployments.autonomous_gates import evaluate_g4_build
from src.deployments.autonomous_models import (
    AutonomousDeployment,
)
from src.deployments.autonomous_schemas import DeploymentStrategy
from src.deployments.tasks import execute_autonomous_deploy_task

pytestmark = pytest.mark.mandatory

_TEST_DB_URL = "postgresql+asyncpg:/" + "/u:p@localhost:5432/db"


class TestConfigurableWorkerTimeout:
    """Verifies ARQ job timeout configurability and effective Settings propagation."""

    def test_default_arq_job_timeout_is_1800_seconds(self) -> None:
        settings = Settings(
            database_url=_TEST_DB_URL,
            redis_url="redis://localhost:6379/0",
            envelope_pepper="test-pepper",
        )
        assert settings.arq_job_timeout_seconds == 1800

    def test_env_override_configures_arq_job_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARQ_JOB_TIMEOUT_SECONDS", "2400")
        monkeypatch.setenv("DATABASE_URL", _TEST_DB_URL)
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("ENVELOPE_PEPPER", "test-pepper")
        settings = Settings()
        assert settings.arq_job_timeout_seconds == 2400

    def test_worker_settings_reflects_configured_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib
        import sys

        monkeypatch.setenv("ARQ_JOB_TIMEOUT_SECONDS", "1800")
        monkeypatch.setenv("DATABASE_URL", _TEST_DB_URL)
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("ENVELOPE_PEPPER", "test-pepper")
        sys.modules.pop("src.worker", None)
        worker_mod = importlib.import_module("src.worker")
        assert worker_mod.WorkerSettings.job_timeout == 1800


class TestWorkerTaskTimeoutAndFencing:
    """Verifies execute_autonomous_deploy_task cancellation handling, fencing, and idempotency."""

    @pytest.fixture
    def mock_settings(self) -> Settings:
        return Settings(
            database_url=_TEST_DB_URL,
            redis_url="redis://localhost:6379/0",
            envelope_pepper="test-pepper",
        )

    @pytest.mark.asyncio
    async def test_cancelled_error_marks_run_failed_and_drains_outbox(self, mock_settings: Settings) -> None:
        run_id = uuid.uuid4()
        project_id = uuid.uuid4()
        now = datetime.now(UTC)

        db_run = AutonomousDeployment(
            id=run_id,
            project_id=project_id,
            status="running",
            strategy=DeploymentStrategy.DOCKER_ONLY.value,
            worker_id="worker-test-1",
            fence_token=1,
            lease_expires_at=now + timedelta(seconds=60),
            log_sequence_counter=10,
            outbox_sequence_counter=5,
        )

        mock_engine = AsyncMock()
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_result_run = MagicMock()
        mock_result_run.scalars.return_value.first.return_value = db_run

        mock_result_max_seq = MagicMock()
        mock_result_max_seq.scalar.return_value = 5

        # session.execute returns run on first call, max_seq on second call, update results thereafter
        mock_session.execute.side_effect = [
            mock_result_run,
            mock_result_max_seq,
            MagicMock(),
            MagicMock(),
        ]

        mock_sessionmaker = MagicMock()
        mock_sessionmaker.return_value.__aenter__.return_value = mock_session

        with (
            patch("src.deployments.tasks.get_settings", return_value=mock_settings),
            patch("src.deployments.tasks.create_db_engine", return_value=mock_engine),
            patch("src.deployments.tasks.create_sessionmaker", return_value=mock_sessionmaker),
            patch("src.deployments.tasks.run_pipeline", side_effect=asyncio.CancelledError()),
            patch("src.deployments.autonomous_outbox.AutonomousOutboxPublisher") as mock_pub_cls,
        ):
            mock_pub = mock_pub_cls.return_value
            mock_pub.drain_run_events = AsyncMock()

            with pytest.raises(asyncio.CancelledError):
                await execute_autonomous_deploy_task(
                    {
                        "run_id": str(run_id),
                        "worker_id": "worker-test-1",
                    }
                )

            assert db_run.status == "failed"
            assert "Worker pipeline cancelled or timed out" in (db_run.error_summary or "")
            assert db_run.completed_at is not None
            assert db_run.outbox_sequence_counter == 6

    @pytest.mark.asyncio
    async def test_idempotent_terminal_state_preserved(self, mock_settings: Settings) -> None:
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        original_completed_at = now - timedelta(minutes=5)

        db_run = AutonomousDeployment(
            id=run_id,
            project_id=uuid.uuid4(),
            status="failed",
            error_summary="Prior failure message",
            completed_at=original_completed_at,
            worker_id="worker-test-1",
            progress_pct=40,
        )

        mock_engine = AsyncMock()
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_result_run = MagicMock()
        mock_result_run.scalars.return_value.first.return_value = db_run
        mock_session.execute.return_value = mock_result_run

        mock_sessionmaker = MagicMock()
        mock_sessionmaker.return_value.__aenter__.return_value = mock_session

        with (
            patch("src.deployments.tasks.get_settings", return_value=mock_settings),
            patch("src.deployments.tasks.create_db_engine", return_value=mock_engine),
            patch("src.deployments.tasks.create_sessionmaker", return_value=mock_sessionmaker),
            patch("src.deployments.tasks.run_pipeline", side_effect=RuntimeError("Late error")),
        ):
            res = await execute_autonomous_deploy_task(
                {
                    "run_id": str(run_id),
                    "worker_id": "worker-test-1",
                }
            )
            assert res["status"] == "failed"
            assert db_run.error_summary == "Prior failure message"
            assert db_run.completed_at == original_completed_at

    @pytest.mark.asyncio
    async def test_worker_fencing_protects_active_lease_from_stale_worker(self, mock_settings: Settings) -> None:
        run_id = uuid.uuid4()
        now = datetime.now(UTC)

        db_run = AutonomousDeployment(
            id=run_id,
            project_id=uuid.uuid4(),
            status="running",
            worker_id="worker-new-active",
            lease_expires_at=now + timedelta(seconds=120),
            error_summary=None,
        )

        mock_engine = AsyncMock()
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_result_run = MagicMock()
        mock_result_run.scalars.return_value.first.return_value = db_run
        mock_session.execute.return_value = mock_result_run

        mock_sessionmaker = MagicMock()
        mock_sessionmaker.return_value.__aenter__.return_value = mock_session

        with (
            patch("src.deployments.tasks.get_settings", return_value=mock_settings),
            patch("src.deployments.tasks.create_db_engine", return_value=mock_engine),
            patch("src.deployments.tasks.create_sessionmaker", return_value=mock_sessionmaker),
            patch("src.deployments.tasks.run_pipeline", side_effect=RuntimeError("Old worker timed out")),
        ):
            res = await execute_autonomous_deploy_task(
                {
                    "run_id": str(run_id),
                    "worker_id": "worker-stale-old",
                }
            )
            assert res["status"] == "running"
            assert db_run.status == "running"
            assert db_run.error_summary is None


class TestIncrementalG4LogStreaming:
    """Verifies incremental log batching and outbox draining during G4 Docker build."""

    @pytest.mark.asyncio
    async def test_logs_flushed_and_drained_incrementally(self, tmp_path: Any) -> None:
        run_id = uuid.uuid4()
        run = AutonomousDeployment(
            id=run_id,
            project_id=uuid.uuid4(),
            status="running",
            strategy=DeploymentStrategy.DOCKER_ONLY.value,
            fence_token=1,
            configuration={"source_preparation": {"workspace_path": str(tmp_path)}},
        )

        (tmp_path / "Dockerfile").write_text("FROM alpine\nRUN echo hello\n", encoding="utf-8")

        mock_session = AsyncMock()
        mock_redis = AsyncMock()
        mock_publisher = AsyncMock()

        # Simulated Docker client calling log_callback with 25 distinct lines
        class MockDockerClient:
            def socket_status(self) -> tuple[bool, str | None]:
                return True, None

            async def build_image_from_context(
                self,
                workspace_path: Any,
                image_tag: str,
                log_callback: Any = None,
                build_info: dict[str, Any] | None = None,
            ) -> tuple[bool, str]:
                if log_callback:
                    for i in range(25):
                        await log_callback(f"Build step {i + 1}")
                if build_info is not None:
                    build_info["image_id"] = "sha256:test12345"
                    build_info["build_mode"] = "built"
                return True, "Image built successfully"

        mock_service = AsyncMock()
        mock_service.append_logs = AsyncMock(return_value=[])

        context = {
            "docker_client": MockDockerClient(),
            "workspace_dir": str(tmp_path),
            "fence_token": 1,
            "redis": mock_redis,
            "outbox_publisher": mock_publisher,
        }

        with patch("src.deployments.autonomous_gates.AutonomousDeploymentService", return_value=mock_service):
            result = await evaluate_g4_build(mock_session, run, context)

        assert result.passed is True
        assert result.status == "succeeded"

        # Verify append_logs was called multiple times in batches rather than once for all 25 lines
        assert mock_service.append_logs.call_count >= 2

        # Verify all 25 lines were passed across the batches
        all_appended_lines: list[str] = []
        for call in mock_service.append_logs.call_args_list:
            all_appended_lines.extend(call.kwargs["entries"])
        assert len(all_appended_lines) == 25
        assert all_appended_lines[0] == "Build step 1"
        assert all_appended_lines[-1] == "Build step 25"

        # Verify drain_run_events was called for each flushed batch
        assert mock_publisher.drain_run_events.call_count == mock_service.append_logs.call_count
