import io
import tarfile
from pathlib import Path

import pytest
from src.policies.bundle import PolicyBundleService
from src.policies.models import Policy

# IMPORTED FOR ITS SIDE EFFECT, and it is load-bearing rather than tidy.
#
# `policies.project_id` carries a foreign key to `projects`. SQLAlchemy resolves that lazily, and it
# resolves it while sorting tables for a flush -- so a session that never imported the `Project`
# mapper fails at `commit()` with
#
#     NoReferencedTableError: Foreign key associated with column 'policies.project_id' could not find
#     table 'projects' with which to generate a foreign key to target column 'id'
#
# which reads like a schema fault and is an import that is not there. This test passed nothing to
# `projects` and needed the table to exist in the metadata all the same.
from src.projects.models import Project  # noqa: F401


@pytest.mark.asyncio
async def test_bundle_digest_stability(
    sessions,
    tmp_path: Path,
) -> None:
    # Create some policies
    p1 = Policy(project_id=None, name="p1", rego_rules="package p1\nallow = true")
    p2 = Policy(project_id=None, name="p2", rego_rules="package p2\nallow = false")

    async with sessions() as session:
        session.add_all([p1, p2])
        await session.commit()

        # Create dummy rego files
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "rule2.rego").write_text("package rule2")
        (agent_dir / "rule1.rego").write_text("package rule1")

        service = PolicyBundleService(session, agent_dir)

        bundle1 = await service.build(project_id=None)
        bundle2 = await service.build(project_id=None)

    assert bundle1.digest == bundle2.digest, "Digests should be identical across rebuilds"

    # Verify tar contents
    import gzip

    unzipped = gzip.decompress(bundle1.bundle)
    with tarfile.open(fileobj=io.BytesIO(unzipped), mode="r") as tar:
        names = tar.getnames()
        # Should be sorted
        assert names == sorted(names), "Tar entries must be sorted"
        assert "data.json" in names
        assert "rule1.rego" in names
        assert "rule2.rego" in names
