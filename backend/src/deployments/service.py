# SPDX-License-Identifier: FSL-1.1-ALv2
"""Deployment records, and the stable-state snapshot a rollback targets. §2.2, read by §2.3.

WHAT THIS MODULE DOES NOT DO: send anything to an agent. The mutation goes through
`GovernanceChokepoint.deploy_manifests`, because §2.2.1 confines `send_command` to `governance/`. This
module writes the row before the transit and updates it from the agent's report afterwards, so a
deployment that was refused at the gate still has a record saying so.

THE ORDER IS ROW-THEN-TRANSIT, and it matters. The row is written first with `pending_approval`, so a
deployment blocked by the gate is still visible in the timeline with the reason. Writing it after a
successful transit would make refusals invisible — and "nothing happened and nothing says why" is the
failure mode this project keeps having to dig out of.

`stable` IS SET ON HEALTH, NOT ON APPLY, and that single choice is what makes rollback trustworthy. A
deployment whose manifests reached the cluster and whose workloads never converged is not a state to
return to. The database enforces it too (`ck_deployments_stable_implies_healthy`), because this service is
one writer and a support UPDATE is another.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import problem

#: The lifecycle, identical to revision `0024`'s CHECK.
DEPLOYMENT_STATUSES: tuple[str, ...] = (
    "pending_approval",
    "applying",
    "applied",
    "degraded",
    "failed",
    "rolled_back",
)

#: The statuses a deployment can still move out of. Reading a terminal row and "completing" it again is
#: the at-least-once delivery bug the change-set transitions already guard against.
_OPEN_STATUSES: frozenset[str] = frozenset({"pending_approval", "applying"})

#: The manifest count one deployment may carry, matching the agent's `MaxDeploymentManifests`.
#:
#: Refused here as well as there, because a backend that accepted 100 and an agent that refused them
#: would produce a change set that can never be delivered — the failure would surface at the agent, three
#: layers from the request that caused it.
MAX_MANIFESTS: int = 32


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    id: uuid.UUID
    project_id: uuid.UUID
    environment_id: uuid.UUID
    change_set_id: uuid.UUID | None
    status: str
    healthy: bool | None
    stable: bool
    manifests: tuple[str, ...]
    manifest_digest: str
    cluster_context: str | None
    namespace: str | None
    report: dict[str, Any] | None
    created_at: str | None
    completed_at: str | None


def manifest_digest(manifests: list[str] | tuple[str, ...]) -> str:
    """A stable digest of the manifest SET, for §2.3's diff between two deployments.

    `sha256:` prefixed and computed over the sorted, newline-joined paths. Sorted because the deployment
    order does not change what was deployed, so two deployments of the same manifests in different orders
    must compare equal — otherwise the timeline would report a change where there was none.

    OVER THE PATHS, NOT THE CONTENT, and the limit is stated rather than left to be discovered: this says
    "the same set of files" and not "the same bytes". Content hashing belongs with the image digest that
    §2.2's build-and-push box will produce, since that is what actually pins what runs.
    """
    joined = "\n".join(sorted(manifests))
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


class DeploymentService:
    """Writes and reads `deployments`."""

    async def create(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        environment_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        manifests: list[str],
        cluster_context: str | None,
        namespace: str | None,
        requested_by: uuid.UUID | None,
    ) -> DeploymentRecord:
        """Record the intent, before the chokepoint is asked.

        `pending_approval` is the honest starting status: nothing has been sent, and whether a human is
        needed is the chokepoint's answer rather than this method's guess. The transit updates it.
        """
        cleaned = [path.strip() for path in manifests if path.strip()]
        if not cleaned:
            raise problem("deployment-invalid", detail="a deployment must name at least one manifest to apply")
        if len(cleaned) > MAX_MANIFESTS:
            raise problem(
                "deployment-invalid",
                detail=(
                    f"a deployment may carry at most {MAX_MANIFESTS} manifests and this one names "
                    f"{len(cleaned)}. The agent enforces the same bound, so a larger set would produce "
                    "a change set that could never be delivered."
                ),
            )
        # A path is workspace-RELATIVE. Refused here as well as in the agent's confinement, so the
        # message names the field instead of arriving as a resolution failure inside an operation.
        for path in cleaned:
            normalised = path.replace("\\", "/")
            # A WINDOWS DRIVE LETTER IS ALSO ABSOLUTE. `C:\windows\hosts` starts with neither slash and
            # is as absolute as `/etc`; the agent runs on Windows as a first-class host, so the backend
            # has to recognise both spellings or it would accept a path the agent will refuse.
            drive_absolute = len(normalised) > 1 and normalised[1] == ":"
            if normalised.startswith("/") or drive_absolute or ".." in normalised.split("/"):
                raise problem(
                    "deployment-invalid",
                    detail=(
                        f"the manifest path {path!r} is not workspace-relative. A deployment names files "
                        "inside the project the agent already holds; it cannot name an absolute path or "
                        "climb out of the workspace."
                    ),
                )

        deployment_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO deployments (id, project_id, environment_id, tenant_id, status, "
                "manifests, manifest_digest, cluster_context, namespace, requested_by) "
                "VALUES (:id, :project, :environment, :tenant, 'pending_approval', "
                "CAST(:manifests AS jsonb), :digest, :context, :namespace, :requested_by)"
            ),
            {
                "id": deployment_id,
                "project": project_id,
                "environment": environment_id,
                "tenant": tenant_id,
                "manifests": json.dumps(cleaned),
                "digest": manifest_digest(cleaned),
                "context": cluster_context,
                "namespace": namespace,
                "requested_by": requested_by,
            },
        )
        return await self.read(session, deployment_id=deployment_id)

    async def attach_change_set(
        self, session: AsyncSession, *, deployment_id: uuid.UUID, change_set_id: uuid.UUID, status: str
    ) -> None:
        """Link the governance record and carry its status across.

        The deployment row is a PROJECTION for the timeline, not a second source of truth about whether
        the deployment was allowed — that is the change set, its approval and its audit chain. Linking
        them is what lets §2.3's timeline show a refusal next to the reason for it.
        """
        if status not in DEPLOYMENT_STATUSES:
            raise ValueError(f"{status!r} is not a deployment status")
        await session.execute(
            text("UPDATE deployments SET change_set_id = :cs, status = :status WHERE id = :id"),
            {"cs": change_set_id, "status": status, "id": deployment_id},
        )

    async def complete(
        self,
        session: AsyncSession,
        *,
        deployment_id: uuid.UUID,
        healthy: bool,
        report: dict[str, Any] | None,
    ) -> DeploymentRecord:
        """Record the agent's report, and mark the row stable only if it is genuinely healthy.

        GUARDED ON AN OPEN STATUS, because command delivery is at-least-once: a second report must not
        move a row that has already settled, or a redelivered `degraded` could un-stable a deployment a
        rollback is currently targeting.

        `degraded` IS NOT `failed`. The manifests are in the cluster; nothing needs retrying. Collapsing
        the two would tell an operator to re-apply what is already applied, and would tell 2.12's
        self-healing that the objects are absent when they are present.
        """
        status = "applied" if healthy else "degraded"
        result = await session.execute(
            text(
                "UPDATE deployments SET status = :status, healthy = :healthy, stable = :stable, "
                "report = CAST(:report AS jsonb), completed_at = now() "
                "WHERE id = :id AND status = ANY(:open) "
                "RETURNING id"
            ),
            {
                "status": status,
                "healthy": healthy,
                # The invariant, applied at the one place that sets it.
                "stable": healthy,
                "report": json.dumps(report or {}),
                "id": deployment_id,
                "open": list(_OPEN_STATUSES),
            },
        )
        if result.first() is None:
            current = await self.read(session, deployment_id=deployment_id)
            raise problem(
                "deployment-conflict",
                detail=(
                    f"deployment {deployment_id} is {current.status}, which is terminal, so this report "
                    "was ignored. Command delivery is at-least-once and only the first report moves a row."
                ),
            )
        return await self.read(session, deployment_id=deployment_id)

    async def fail(self, session: AsyncSession, *, deployment_id: uuid.UUID, reason: str) -> DeploymentRecord:
        """The apply itself was refused. Nothing was verified, so `healthy` stays NULL.

        NULL rather than False, deliberately: False asserts that the workloads were checked and were not
        ready, and nothing checked them. The distinction is what stops a failed apply from looking like a
        cluster problem.
        """
        await session.execute(
            text(
                "UPDATE deployments SET status = 'failed', stable = false, "
                "report = CAST(:report AS jsonb), completed_at = now() "
                "WHERE id = :id AND status = ANY(:open)"
            ),
            {"report": json.dumps({"error": reason}), "id": deployment_id, "open": list(_OPEN_STATUSES)},
        )
        return await self.read(session, deployment_id=deployment_id)

    async def history(self, session: AsyncSession, *, project_id: uuid.UUID, limit: int = 50) -> list[DeploymentRecord]:
        """§2.3's timeline source: newest first."""
        result = await session.execute(
            text(
                "SELECT id, project_id, environment_id, change_set_id, status, healthy, stable, "
                "manifests, manifest_digest, cluster_context, namespace, report, "
                "created_at::text AS created_at, completed_at::text AS completed_at "
                "FROM deployments WHERE project_id = :project "
                "ORDER BY created_at DESC LIMIT :limit"
            ),
            {"project": project_id, "limit": max(1, min(limit, 200))},
        )
        return [_record(row) for row in result.mappings()]

    async def rollback_target(self, session: AsyncSession, *, environment_id: uuid.UUID) -> DeploymentRecord | None:
        """The newest STABLE deployment of an environment, or `None` when there is not one.

        `None` is a real answer and the caller must render it as one: an environment whose only deployment
        was degraded has nothing to roll back to, and offering a rollback button that would restore a
        broken state is worse than offering none.

        The second-newest is NOT what this returns. It returns the newest stable one, which after a
        degraded deployment is the one before it — and after a healthy one is itself. §2.3's rollback
        chooses a target explicitly; this is the default it offers.
        """
        result = await session.execute(
            text(
                "SELECT id, project_id, environment_id, change_set_id, status, healthy, stable, "
                "manifests, manifest_digest, cluster_context, namespace, report, "
                "created_at::text AS created_at, completed_at::text AS completed_at "
                "FROM deployments WHERE environment_id = :environment AND stable "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"environment": environment_id},
        )
        row = result.mappings().first()
        return _record(row) if row is not None else None

    async def read(self, session: AsyncSession, *, deployment_id: uuid.UUID) -> DeploymentRecord:
        result = await session.execute(
            text(
                "SELECT id, project_id, environment_id, change_set_id, status, healthy, stable, "
                "manifests, manifest_digest, cluster_context, namespace, report, "
                "created_at::text AS created_at, completed_at::text AS completed_at "
                "FROM deployments WHERE id = :id"
            ),
            {"id": deployment_id},
        )
        row = result.mappings().first()
        if row is None:
            raise problem("deployment-absent", detail=f"no deployment {deployment_id}")
        return _record(row)


def _record(row: Any) -> DeploymentRecord:
    raw = row["manifests"]
    manifests = tuple(raw) if isinstance(raw, list) else tuple(json.loads(raw or "[]"))
    report = row["report"]
    return DeploymentRecord(
        id=row["id"],
        project_id=row["project_id"],
        environment_id=row["environment_id"],
        change_set_id=row["change_set_id"],
        status=str(row["status"]),
        healthy=row["healthy"],
        stable=bool(row["stable"]),
        manifests=manifests,
        manifest_digest=str(row["manifest_digest"]),
        cluster_context=row["cluster_context"],
        namespace=row["namespace"],
        report=report if isinstance(report, dict) else (json.loads(report) if report else None),
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )
