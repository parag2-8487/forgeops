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
    # `publish` asks whether a bundle with this digest already exists, because the digest is
    # content-addressed and `uq_policy_bundles_digest` is unique table-wide. `None` is what a
    # first-ever publish sees; without it this double reports a MagicMock, the idempotent branch is
    # taken, and the assertion below fails on a path the test is not about.
    result_mock.scalar_one_or_none.return_value = None
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
        payload={
            "bundle_id": str(bundle.id),
            "project_id": str(project_id),
        },
    )


@pytest.mark.asyncio
async def test_republishing_identical_content_activates_the_existing_row():
    """Publishing unchanged content twice must succeed, and must not INSERT a second row.

    The digest is sha256 over the archive, so an unchanged policy set produces an unchanged digest,
    and `uq_policy_bundles_digest` is unique across the table. The first version of `publish` always
    inserted, so the second call raised

        duplicate key value violates unique constraint "uq_policy_bundles_digest"

    which surfaced as a 500 from `POST /policies/publish` -- for an operator, or a re-run of the
    end-to-end journey, doing something entirely reasonable.
    """
    project_id = uuid.uuid4()
    existing = PolicyBundle(digest="sha256:67890", bundle=b"tar", project_id=project_id)
    existing.active = False

    session_mock = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = existing
    session_mock.execute.return_value = result_mock

    service = PolicyBundleService(session_mock, Path("dummy"))
    tasks_mock = AsyncMock()
    service._tasks = tasks_mock

    incoming = PolicyBundle(digest="sha256:67890", bundle=b"tar", project_id=project_id)
    await service.publish(incoming, actor=MagicMock())

    assert session_mock.add.call_count == 0, "an identical digest must not be inserted again"
    assert existing.active is True, "the row already holding those bytes becomes the active one"
    # The notification names the row that IS active, not the transient object handed in.
    tasks_mock.enqueue.assert_called_once_with(
        "policy.bundle.publish",
        payload={"bundle_id": str(existing.id), "project_id": str(project_id)},
    )


@pytest.mark.asyncio
async def test_publishing_one_digest_into_two_projects_is_refused():
    """The digest is unique table-wide, so identical content cannot belong to two scopes.

    Said plainly rather than by silently moving the existing row into a different project, which
    would leave the first project pointing at a bundle it no longer owns.
    """
    existing = PolicyBundle(digest="sha256:abc", bundle=b"tar", project_id=uuid.uuid4())

    session_mock = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = existing
    session_mock.execute.return_value = result_mock

    service = PolicyBundleService(session_mock, Path("dummy"))
    incoming = PolicyBundle(digest="sha256:abc", bundle=b"tar", project_id=uuid.uuid4())

    with pytest.raises(ValueError, match="already published for project"):
        await service.publish(incoming, actor=MagicMock())
