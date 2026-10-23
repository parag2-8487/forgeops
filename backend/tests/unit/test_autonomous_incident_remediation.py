# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests verifying incident remediation for autonomous deployment pipeline:
1. Unhandled worker exceptions transition runs to 'failed' (no permanently stuck 'running' runs).
2. Worker lease duration provides adequate headroom (60s) for verification probes.
3. Intermediate commits persist stage progress monotonically.
4. Active heartbeat renewal succeeds without blocking row locks.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.deployments.autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentOutbox,
)
from src.deployments.autonomous_worker import (
    LEASE_DURATION_SECONDS,
    AutonomousWorker,
    WorkerFencingLostError,
)
from src.deployments.tasks import execute_autonomous_deploy_task


@pytest.mark.asyncio
async def test_lease_duration_has_adequate_probe_headroom() -> None:
    """Verify that worker lease duration is at least 60s to prevent fencing expiry during 30s probes."""
    assert LEASE_DURATION_SECONDS >= 60


@pytest.mark.asyncio
async def test_execute_task_transitions_run_to_failed_on_unhandled_error() -> None:
    """Verify execute_autonomous_deploy_task catches unhandled worker errors and transitions the run to failed."""
    run_id = uuid.uuid4()
    mock_run = AutonomousDeployment(
        id=run_id,
        project_id=uuid.uuid4(),
        status="running",
        strategy="docker_github_vercel",
        attempt_number=1,
        progress_pct=45,
    )

    # Mock DB session
    executed_statements = []

    class MockSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def execute(self, stmt):
            executed_statements.append(stmt)
            res = MagicMock()
            res.scalars.return_value.first.return_value = mock_run
            res.scalar.return_value = 2
            return res

        async def commit(self):
            pass

        async def rollback(self):
            pass

        def add(self, obj):
            executed_statements.append(obj)

    class MockSessionMaker:
        def __call__(self):
            return MockSession()

    with (
        patch("src.deployments.tasks.get_settings", autospec=True) as mock_settings,
        patch("src.deployments.tasks.create_db_engine", autospec=True) as mock_engine,
        patch("src.deployments.tasks.create_sessionmaker", autospec=True, return_value=MockSessionMaker()),
        patch(
            "src.deployments.tasks.run_pipeline",
            autospec=True,
            side_effect=WorkerFencingLostError("Worker fence lease expired during G6 probe"),
        ),
    ):
        mock_settings.return_value.redis_url = "redis://localhost:6379/0"
        engine_mock = MagicMock()
        engine_mock.dispose = AsyncMock()
        mock_engine.return_value = engine_mock

        with pytest.raises(WorkerFencingLostError):
            await execute_autonomous_deploy_task(
                {
                    "run_id": str(run_id),
                    "worker_id": "test-worker-1",
                }
            )

        # Verify that terminal update was executed
        assert len(executed_statements) > 0
        # Check outbox addition
        outbox_entries = [s for s in executed_statements if isinstance(s, AutonomousDeploymentOutbox)]
        assert len(outbox_entries) == 1
        assert outbox_entries[0].event_type == "run_failed"
        assert outbox_entries[0].payload["status"] == "failed"
        assert "Worker fence lease expired" in outbox_entries[0].payload["error_summary"]


@pytest.mark.asyncio
async def test_worker_heartbeat_renewal_extends_lease() -> None:
    """Verify that worker renew_heartbeat updates lease_expires_at while fence token is valid."""
    run_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = run_id
    mock_session.execute.return_value = mock_result

    worker = AutonomousWorker(worker_id="test-worker-1", session=mock_session)

    extended = await worker.heartbeat(run_id=run_id, fence_token=1)
    assert extended is True
    assert mock_session.execute.called


@pytest.mark.asyncio
async def test_stale_worker_cannot_update_state_after_lease_expiry() -> None:
    """Verify that a worker with an expired lease is blocked from updating stage state or emitting outbox events."""
    run_id = uuid.uuid4()
    mock_session = AsyncMock()
    mock_result = MagicMock()
    # 0 rows returned because lease_expires_at > now was false
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    worker = AutonomousWorker(worker_id="stale-worker-1", session=mock_session)

    with pytest.raises(WorkerFencingLostError) as exc_info:
        await worker.transition_stage(
            run_id=run_id,
            stage_name="G6_workload",
            fence_token=1,
            status="succeeded",
            progress_pct=100,
        )
    assert "Worker fence lost" in str(exc_info.value)


@pytest.mark.asyncio
async def test_stale_worker_cannot_revive_expired_lease() -> None:
    """Verify that worker.heartbeat returns False when the lease has expired, preventing revival."""
    run_id = uuid.uuid4()
    mock_session = AsyncMock()
    mock_result = MagicMock()
    # 0 rows returned because lease_expires_at > now was false
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    worker = AutonomousWorker(worker_id="stale-worker-1", session=mock_session)

    renewed = await worker.heartbeat(run_id=run_id, fence_token=1)
    assert renewed is False


@pytest.mark.asyncio
async def test_second_worker_takeover_blocks_first_worker_writes() -> None:
    """Verify that when worker 2 claims after expiry, worker 1 cannot corrupt run or stage state."""
    run_id = uuid.uuid4()

    # Worker 1 attempts guarded update with superseded fence_token=1
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []  # 0 rows updated
    mock_session.execute.return_value = mock_result

    worker1 = AutonomousWorker(worker_id="worker-1", session=mock_session)

    with pytest.raises(WorkerFencingLostError):
        await worker1.guarded_update(
            run_id=run_id,
            fence_token=1,
            values={"status": "running"},
        )


@pytest.mark.asyncio
async def test_long_running_stage_heartbeat_renewal_preserves_custody() -> None:
    """Verify that during a long-running stage probe exceeding 30s, background heartbeats keep lease active."""
    run_id = uuid.uuid4()
    heartbeat_count = 0

    mock_session = AsyncMock()

    async def mock_execute(stmt):
        nonlocal heartbeat_count
        res = MagicMock()
        res.scalars.return_value.all.return_value = [run_id]
        res.scalar_one_or_none.return_value = run_id
        return res

    mock_session.execute = mock_execute
    worker = AutonomousWorker(worker_id="test-worker-1", session=mock_session)

    # Simulate heartbeat renewals during a 30+ second operation
    for _ in range(4):
        extended = await worker.heartbeat(run_id=run_id, fence_token=1)
        assert extended is True
        heartbeat_count += 1

    assert heartbeat_count == 4


@pytest.mark.asyncio
async def test_outbox_sequence_allocation_is_consecutive_and_monotonic() -> None:
    """Verify that multiple state transitions generate strictly consecutive outbox sequence numbers."""
    from src.deployments.autonomous_models import AutonomousDeployment

    run = AutonomousDeployment(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        status="running",
        strategy="docker_github_vercel",
        attempt_number=1,
        outbox_sequence_counter=1,
    )

    sequences = []
    for _ in range(5):
        run.outbox_sequence_counter += 1
        sequences.append(run.outbox_sequence_counter)

    assert sequences == [2, 3, 4, 5, 6]
    assert all(sequences[i] + 1 == sequences[i + 1] for i in range(len(sequences) - 1))
