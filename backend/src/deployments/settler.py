# SPDX-License-Identifier: FSL-1.1-ALv2
"""Settling a deployment when its change set settles, and telling somebody. §2.2, §2.6.

WHY THIS FILE EXISTS. `DeploymentService.complete` had no production caller: only tests invoked it, so a real
deployment stayed `applying` for ever, `healthy` and `stable` were never written, and `rollback_target` could
never find anything to offer. The mechanism was correct and nothing reached it — this repository's most
repeated defect. This is the runtime path.

It satisfies `governance.chokepoint.ChangeSetSettler` structurally, so `governance/` never learns what a
deployment is, and it is composed at the app factory like every other collaborator.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .service import DeploymentService


@runtime_checkable
class NotificationRaiser(Protocol):
    """How this module tells somebody, without importing the domain that does the telling.

    `notifications` is a banned cross-domain import and the parse-based boundary check enforces that even
    where the linter is exempted -- which it caught when the first version of this file imported
    `NotificationService` directly. A Protocol the service satisfies structurally keeps the boundary and the
    behaviour.
    """

    async def raise_notification(
        self,
        session: Any,
        *,
        project_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        kind: str,
        values: dict[str, Any],
        resource_kind: str | None = ...,
        resource_id: str | None = ...,
    ) -> Any: ...


@dataclass(slots=True)
class DeploymentSettler:
    """Completes the deployment a change set carried, then raises a notification about it."""

    deployments: DeploymentService
    notifications: NotificationRaiser

    async def settle(
        self,
        session: AsyncSession,
        *,
        change_set_id: uuid.UUID,
        succeeded: bool,
        report: Mapping[str, Any] | None,
    ) -> None:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT d.id, d.project_id, d.tenant_id, d.manifests, e.name AS environment "
                        "FROM deployments d JOIN environments e ON e.id = d.environment_id "
                        "WHERE d.change_set_id = :cs"
                    ),
                    {"cs": change_set_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            # Not every change set is a deployment — an apply, a clone and a dashboard action all reach
            # here. Nothing to settle is the ordinary case, not a failure.
            return

        deployment_id = row["id"]
        manifests = list(row["manifests"] or [])
        environment = str(row["environment"])
        project_name = await self._project_name(session, row["project_id"])

        if not succeeded:
            record = await self.deployments.fail(
                session, deployment_id=deployment_id, reason="the agent reported the command failed"
            )
            await self._notify_failure(
                session,
                project_id=row["project_id"],
                tenant_id=row["tenant_id"],
                project_name=project_name,
                environment=environment,
                deployment_id=deployment_id,
                status=record.status,
                detail="the agent reported the command failed",
            )
            return

        # HEALTH COMES FROM THE AGENT'S REPORT, and its ABSENCE is not health. A report that does not say
        # whether the workloads converged leaves `healthy` null, which is the state the schema has for
        # "nothing verified it" — inferring true from a successful apply is exactly the conflation
        # `degraded` exists to prevent.
        healthy = None
        detail = "the agent reported no workload health"
        if report is not None:
            if "healthy" in report:
                healthy = bool(report["healthy"])
            workloads = report.get("workloads")
            if isinstance(workloads, list) and workloads:
                unready = [
                    str(item.get("name", "?"))
                    for item in workloads
                    if isinstance(item, dict) and item.get("ready") is not True
                ]
                healthy = not unready
                detail = (
                    "every workload became ready"
                    if not unready
                    else f"these workloads did not become ready: {', '.join(unready)}"
                )

        record = await self.deployments.complete(
            session,
            deployment_id=deployment_id,
            healthy=healthy,
            report=dict(report) if report is not None else None,
        )

        if healthy:
            await self.notifications.raise_notification(
                session,
                project_id=row["project_id"],
                tenant_id=row["tenant_id"],
                kind="deploy_completed",
                values={
                    "project": project_name,
                    "environment": environment,
                    "deployment_id": str(deployment_id),
                    "manifest_count": len(manifests),
                },
                resource_kind="deployment",
                resource_id=str(deployment_id),
            )
            return

        await self._notify_failure(
            session,
            project_id=row["project_id"],
            tenant_id=row["tenant_id"],
            project_name=project_name,
            environment=environment,
            deployment_id=deployment_id,
            status=record.status,
            detail=detail,
        )

    async def _notify_failure(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        project_name: str,
        environment: str,
        deployment_id: uuid.UUID,
        status: str,
        detail: str,
    ) -> None:
        await self.notifications.raise_notification(
            session,
            project_id=project_id,
            tenant_id=tenant_id,
            kind="deploy_failed",
            values={
                "project": project_name,
                "environment": environment,
                "deployment_id": str(deployment_id),
                "status": status,
                "detail": detail,
            },
            resource_kind="deployment",
            resource_id=str(deployment_id),
        )

    async def _project_name(self, session: AsyncSession, project_id: uuid.UUID) -> str:
        name = (
            await session.execute(text("SELECT name FROM projects WHERE id = :id"), {"id": project_id})
        ).scalar_one_or_none()
        # The id rather than a placeholder when the row is gone: a template that required a name and got
        # "unknown" would send a message naming nothing an operator can look up.
        return str(name) if name else str(project_id)
