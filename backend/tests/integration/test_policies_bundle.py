import io
import tarfile
from pathlib import Path

import pytest
from src.policies.bundle import PolicyBundleService
from src.policies.models import Policy


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
