import gzip
import io
import tarfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.policies.bundle import PolicyBundle, PolicyBundleService
from src.policies.models import Policy


@pytest.mark.asyncio
async def test_bundle_build_canonical_digest():
    project_id = uuid.uuid4()

    # Mock Policy row
    policy = Policy(project_id=project_id, name="test-policy", rego_rules="deny { true }", enabled=True)

    # Mock DB session
    session_mock = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [policy]
    session_mock.execute.return_value = result_mock

    # Needs to know where the agent policies live
    repo_root = Path(__file__).parent.parent.parent.parent
    agent_policies_dir = repo_root / "policies" / "agent"

    service = PolicyBundleService(session_mock, agent_policies_dir)

    bundle1 = await service.build(project_id=project_id)
    bundle2 = await service.build(project_id=project_id)

    # They must have the exact same digest
    assert bundle1.digest == bundle2.digest
    assert bundle1.digest.startswith("sha256:")

    # The payload must be a gzip file containing a tar
    with gzip.GzipFile(fileobj=io.BytesIO(bundle1.bundle), mode="rb") as gz:
        with tarfile.open(fileobj=gz, mode="r:") as tar:
            names = tar.getnames()
            # Must include agent policies and data.json
            assert "data.json" in names
            assert "governance.rego" in names

            for tarinfo in tar.getmembers():
                # Canonical properties
                assert tarinfo.mtime == 0
                assert tarinfo.uid == 0
                assert tarinfo.gid == 0
                assert tarinfo.uname == ""
                assert tarinfo.gname == ""
                if tarinfo.isdir():
                    assert tarinfo.mode == 0o755
                else:
                    assert tarinfo.mode == 0o644


@pytest.mark.asyncio
async def test_bundle_publish_and_active_digest():
    project_id = uuid.uuid4()

    session_mock = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar.return_value = "sha256:12345"
    session_mock.execute.return_value = result_mock

    service = PolicyBundleService(session_mock, Path("dummy"))

    digest = await service.active_digest(project_id=project_id)
    assert digest == "sha256:12345"

    bundle = PolicyBundle(digest="sha256:67890", bundle=b"tar", project_id=project_id)
    actor = MagicMock()
    actor.id = uuid.uuid4()

    # Needs tasks queue mock
    tasks_mock = AsyncMock()
    service._tasks = tasks_mock

    await service.publish(bundle, actor=actor)

    # Verify DB calls
    assert session_mock.add.call_count == 1
    # Verify task enqueued
    tasks_mock.enqueue.assert_called_once_with(
        "policy.bundle.publish",
        bundle_id=bundle.id,
        project_id=project_id,
    )
