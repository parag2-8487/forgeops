"""Seed a project, publish its policy bundle, and mint a pairing code.

WHY THIS SCRIPT EXISTS. The host-apply proof needs a project and a live pairing code, and both sit
behind `require_principal` over HTTP — which means a full OIDC round trip through Authentik. This
reaches the same rows by calling the REAL services with a real `Principal`, which is what those
routes do once they have one.

NOTHING HERE IS A STAND-IN. The services are the ones `create_app` composed: the same
`DeviceService`, the same internal CA, the same Redis, the same pepper. Building a `DeviceService` by
hand would risk issuing a client certificate from a DIFFERENT CA than the one the mTLS listener
serves, and the resulting handshake failure would look like a trust bug rather than a setup mistake.

Run inside the backend container, which already has the settings and the database URL:

    docker compose exec -T backend python /tmp/seed_host_apply.py [workspace-path]
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.principal import Principal
from src.main import create_app
from src.policies.bundle import PolicyBundleService
from src.auth.models import UserRole

#: The path the AGENT resolves. Recorded on the project so both sides agree what a change item's
#: relative path is relative to.
DEFAULT_WORKSPACE = "/workspace"


async def main(workspace: str) -> int:
    app = create_app()

    # The lifespan is what composes `device_service`, the CA and the Redis client. Entering it is
    # how this script gets the same objects the running API uses rather than look-alikes.
    async with app.router.lifespan_context(app):
        maker = app.state.sessionmaker
        devices = app.state.device_service

        async with maker() as session:  # type: AsyncSession
            project_id, principal = await make_project(session, workspace)

            # PUBLISH BEFORE PAIRING. Pairing pins the device to the project's active bundle digest,
            # so a device paired while nothing is published is pinned to nothing and every later
            # command is refused with `policy-bundle-stale` — a correct refusal about a fact that was
            # set up wrongly. §1.7's order is publish, then pair.
            # Constructed the way `get_bundle_service` does, including the rego directory: a
            # bundle built from the wrong directory would publish a digest over the wrong rules.
            bundles = PolicyBundleService(
                session, Path(getattr(app.state.settings, "agent_policies_dir", "policies/agent"))
            )
            bundle = await bundles.build(project_id=project_id)
            try:
                await bundles.publish(bundle, actor=principal)
                await session.commit()
            except ValueError as exc:
                # A digest is content-addressed and unique table-wide, so an identical rule set
                # already published — commonly the installation-wide bundle — cannot be published
                # again for this project. That is not a setup failure: the project's ACTIVE digest
                # already resolves to these very rules, which is the condition pairing needs.
                if "already published" not in str(exc):
                    raise
                await session.rollback()

            code = await devices.issue_pairing_code(
                session, project_id=project_id, actor=principal
            )
            await session.commit()

            print(
                json.dumps(
                    {
                        "project_id": str(project_id),
                        "user_id": str(principal.user_id),
                        "code": code.code,
                        "bundle_digest": bundle.digest,
                        "workspace": workspace,
                    }
                )
            )
    return 0


async def make_project(session: AsyncSession, workspace: str) -> tuple[uuid.UUID, Principal]:
    """One project and one admin principal, committed."""
    project_id, user_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, :path)"),
        {"id": project_id, "name": f"host-apply-{project_id.hex[:8]}", "path": workspace},
    )
    await session.execute(
        text(
            "INSERT INTO users (id, email, name, role, idp_subject, is_active) "
            "VALUES (:id, :email, 'Host Apply Operator', 'admin', :sub, true)"
        ),
        {
            "id": user_id,
            "email": f"host-{user_id.hex[:8]}@example.invalid",
            "sub": f"sub-{user_id.hex}",
        },
    )
    await session.commit()
    return project_id, Principal.for_user(
        user_id=user_id,
        subject=f"sub-{user_id.hex}",
        email=f"host-{user_id.hex[:8]}@example.invalid",
        role=UserRole.ADMIN,
    )


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WORKSPACE))
    )
