# SPDX-License-Identifier: FSL-1.1-ALv2
"""The recorders other domains file incidents through. Phase 2 §2.11.

WHY THIS FILE EXISTS RATHER THAN CALLERS IMPORTING `ingestion` DIRECTLY. `deployments` may not import
`incidents` -- `check-chokepoint` enforces it and is right to, since the two domains have no other business
together. Each caller therefore depends on a narrow Protocol declared in ITS OWN module, and this file holds
the implementations. Composition happens once, in the app factory.

WHY ANY OF THIS MATTERS: an ingestion path with no production caller is the defect this project keeps
finding. `DeploymentService.complete` had none. `record_command_result`'s failure branch could not run.
`evaluate_policy` took an argument nobody passed. So ingestion is wired to the sites that already fail --
`DeploymentService.fail`, which is reached only when the agent ran a command and it failed -- rather than
waiting for a future feed. A test drives that real chain and reads the row back.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from .ingestion import from_circuit_breaker, from_deployment_failure, ingest


class DeploymentIncidents:
    """Files incidents for deployment failures. Satisfies `deployments.DeploymentIncidentRecorder`."""

    async def record_deployment_failure(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        deployment_id: uuid.UUID,
        environment: str,
        reason: str,
    ) -> None:
        """File it, and DO NOT COMMIT.

        The caller is inside a transaction that is also writing the deployment's `failed` status, and the two
        must land together: an incident recorded for a deployment whose failure was rolled back would be an
        incident about something that did not happen. Committing here would break that, and it would also
        commit whatever else the caller had pending.
        """
        await ingest(
            session,
            from_deployment_failure(
                project_id=project_id,
                deployment_id=deployment_id,
                environment=environment,
                operation="deployment apply",
                reason=reason,
            ),
        )

    async def record_breaker_opened(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        environment: str,
        failures: int,
    ) -> None:
        """A breaker opening is its own signal, not merely a consequence of the failures that opened it.

        Filed separately because an operator seeing three deployment failures and an open breaker is looking
        at a different situation from one seeing three failures: the second means further attempts are being
        refused, which is why their next deploy will not even be tried.
        """
        await ingest(
            session,
            from_circuit_breaker(project_id=project_id, environment=environment, failures=failures),
        )
