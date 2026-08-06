import gzip
import hashlib
import io
import json
import tarfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, update
from src.core.tasks import TaskDispatcher
from src.policies.models import Policy, PolicyBundle


class PolicyBundleService:
    """Builds, digests and publishes the bundle both sides evaluate.

    A bundle is a gzip tar of policies/agent/**.rego plus a data document derived
    from the project's policy rows. Its digest is sha256 over a CANONICAL archive
    (sorted paths, fixed mtimes, fixed permissions) so the same inputs always yield
    the same digest.
    """

    def __init__(self, session: AsyncSession, agent_policies_dir: Path, tasks: TaskDispatcher | None = None):
        self._session = session
        self._agent_policies_dir = agent_policies_dir
        self._tasks = tasks

    async def active_digest(self, *, project_id: uuid.UUID | None) -> str:
        stmt = select(PolicyBundle.digest).where(PolicyBundle.active == True)
        if project_id:
            stmt = stmt.where(PolicyBundle.project_id == project_id)
        else:
            stmt = stmt.where(PolicyBundle.project_id.is_(None))
        result = await self._session.execute(stmt.limit(1))
        return result.scalar() or ""

    async def publish(self, bundle: PolicyBundle, *, actor: Any) -> None:
        # Mark all other bundles as inactive
        stmt = update(PolicyBundle).where(PolicyBundle.active == True).values(active=False)
        if bundle.project_id:
            stmt = stmt.where(PolicyBundle.project_id == bundle.project_id)
        else:
            stmt = stmt.where(PolicyBundle.project_id.is_(None))

        await self._session.execute(stmt)

        # Insert the new active bundle
        bundle.active = True
        self._session.add(bundle)
        await self._session.commit()

        if self._tasks:
            await self._tasks.enqueue(
                "policy.bundle.publish",
                bundle_id=bundle.id,
                project_id=bundle.project_id,
            )

    async def build(self, *, project_id: uuid.UUID | None) -> PolicyBundle:
        # 1. Query policies
        stmt = select(Policy).where(Policy.enabled == True)
        if project_id:
            stmt = stmt.where(Policy.project_id == project_id)
        else:
            stmt = stmt.where(Policy.project_id.is_(None))

        result = await self._session.execute(stmt)
        policies: Sequence[Policy] = result.scalars().all()

        # 2. Construct data.json
        data = {
            "forgeops": {
                "governance": {
                    "policies": [
                        {
                            "id": str(p.id),
                            "name": p.name,
                            "rego_rules": p.rego_rules,
                            "engine": p.engine,
                        }
                        for p in policies
                    ]
                }
            }
        }
        data_bytes = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")

        # 3. Read rego files and build canonical tar
        tar_buf = io.BytesIO()

        # We need paths to be sorted for canonical output
        entries = []
        for path in self._agent_policies_dir.glob("*.rego"):
            if not path.is_file():
                continue
            entries.append((path.name, path.read_bytes()))

        entries.append(("data.json", data_bytes))
        entries.sort(key=lambda x: x[0])

        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            for name, content in entries:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content))

        # 4. Gzip the tar deterministically
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
            gz.write(tar_buf.getvalue())

        payload = gz_buf.getvalue()

        # 5. Compute sha256
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        return PolicyBundle(digest=digest, bundle=payload, project_id=project_id)
