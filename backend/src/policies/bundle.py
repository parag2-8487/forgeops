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

from .models import Policy, PolicyBundle


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
        # `.is_(True)`, NOT `is True`. `PolicyBundle.active is True` is a PYTHON identity test
        # between an InstrumentedAttribute and the singleton `True`, so it evaluates to the bool
        # `False` before SQLAlchemy sees anything -- and `where(False)` compiles to `WHERE false`,
        # which matches no row ever. This method therefore returned "" for every project even with an
        # active bundle published, and it does so silently: no error, no warning, just an empty
        # answer that reads like "nothing has been published yet".
        stmt = select(PolicyBundle.digest).where(PolicyBundle.active.is_(True))
        if project_id:
            stmt = stmt.where(PolicyBundle.project_id == project_id)
        else:
            stmt = stmt.where(PolicyBundle.project_id.is_(None))
        result = await self._session.execute(stmt.limit(1))
        return result.scalar() or ""

    async def publish(self, bundle: PolicyBundle, *, actor: Any) -> None:
        """Activate `bundle` for its scope, replacing whatever was active there.

        IDEMPOTENT, because the digest is content-addressed and `uq_policy_bundles_digest` is unique
        across the table. Publishing twice without editing a policy produces the same bytes and
        therefore the same digest, and the second call used to reach the INSERT and fail with

            duplicate key value violates unique constraint "uq_policy_bundles_digest"

        surfacing as a 500 from `POST /policies/publish`. Republishing an unchanged bundle is a
        perfectly reasonable thing for an operator -- or a re-run of the end-to-end journey -- to do,
        and the honest answer is "that bundle is now active", not an internal error.
        """
        # Deactivate whatever is active in this scope FIRST. The partial unique indexes
        # `uq_policy_bundles_one_active_global` and `uq_policy_bundles_one_active_per_project` allow
        # exactly one active row per scope, so activating before clearing would violate them.
        #
        # `.is_(True)` for the reason given in `active_digest`: with `is True` this UPDATE matched
        # nothing, so every published bundle stayed active and "the active bundle" became whichever
        # row a later `ORDER BY` happened to put first.
        stmt = update(PolicyBundle).where(PolicyBundle.active.is_(True)).values(active=False)
        if bundle.project_id:
            stmt = stmt.where(PolicyBundle.project_id == bundle.project_id)
        else:
            stmt = stmt.where(PolicyBundle.project_id.is_(None))

        await self._session.execute(stmt)

        existing = (
            await self._session.execute(select(PolicyBundle).where(PolicyBundle.digest == bundle.digest))
        ).scalar_one_or_none()

        if existing is None:
            bundle.active = True
            self._session.add(bundle)
            published = bundle
        else:
            # The digest is unique table-wide, so identical content cannot belong to two scopes. Say
            # so rather than silently moving the existing row into a different project.
            if existing.project_id != bundle.project_id:
                raise ValueError(
                    f"bundle {bundle.digest} is already published for project "
                    f"{existing.project_id!s}, so it cannot also be published for "
                    f"{bundle.project_id!s}: the digest is content-addressed and unique table-wide"
                )
            existing.active = True
            published = existing

        await self._session.commit()

        if self._tasks:
            await self._tasks.enqueue(
                "policy.bundle.publish",
                payload={
                    "bundle_id": str(published.id),
                    "project_id": str(published.project_id) if published.project_id else None,
                },
            )

    async def build(self, *, project_id: uuid.UUID | None) -> PolicyBundle:
        # 1. Query policies.
        #
        # `.is_(True)`, NOT `is True`. The identity form evaluated to the bool `False` in Python, so
        # this compiled to `WHERE false` and selected NO policies -- and the bundle was still built,
        # still digested, still published. The result was a well-formed, correctly-digested archive
        # whose data.json contained an empty policy list: an agent evaluating against it would allow
        # or deny on nothing at all, and nothing about the digest or the publish response hinted at
        # it. A bundle that is empty for a reason nobody can see is worse than a publish that fails.
        stmt = select(Policy).where(Policy.enabled.is_(True))
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
