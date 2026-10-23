# SPDX-License-Identifier: FSL-1.1-ALv2
"""A deployment's row traverses `pending_approval` -> `applying` -> settled. Phase 2 2.2.

THE DEFECT THIS CLOSES
----------------------
`DeploymentService.request` writes `pending_approval` from the chokepoint's verdict at REQUEST time.
`DeploymentSettler` writes the terminal status from the agent's report. **Nothing wrote the middle one.**
Approval minted the authority, delivered the signed envelope, and left the row claiming a human had not
decided yet.

Every reader was wrong about it: the §2.3 timeline, the deployment logs -- whose own docstring notes that a
`pending_approval` deployment shows an empty log an operator reads as "nothing happened" -- and
`rollback_target`, which looks for a deployment that actually went out.

It was found from the outside. The criterion-3 E2E spec approved the change set, waited four minutes for the
row to leave `pending_approval`, and it never did -- so the apply never happened, the failure never
happened, and the incident that the criterion is about was never filed.

WHY THE TEST IS SHAPED LIKE THIS
--------------------------------
It asserts the ROW at each of the three states, read back from a real PostgreSQL, because that is what every
production reader consults. A test that asserted the dispatcher's method was called would have passed
throughout the defect -- the method did not exist. `test_wiring_coverage`'s whole argument is that a
collaborator nothing drives is the recurring defect of this codebase, and a status nothing advances is the
same thing in the data.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.deployments.dispatcher import DeploymentDispatcher

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


async def _deployment(session: AsyncSession, *, status: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A real `deployments` row with the real columns its FKs require."""
    project_id = uuid.uuid4()
    environment_id = uuid.uuid4()
    change_set_id = uuid.uuid4()
    deployment_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :n, '/tmp/x')"),
        {"id": project_id, "n": f"disp-{project_id.hex[:8]}"},
    )
    await session.execute(
        text(
            "INSERT INTO environments (id, project_id, name, kind, position) VALUES (:id, :p, 'staging', 'staging', 1)"
        ),
        {"id": environment_id, "p": project_id},
    )
    await session.execute(
        text(
            "INSERT INTO change_sets (id, project_id, status, origin, blast_radius_score, "
            "blast_radius_verdict, policy_bundle_digest) "
            "VALUES (:id, :p, 'approved', 'manual', 1, 'allow', 'sha256:t')"
        ),
        {"id": change_set_id, "p": project_id},
    )
    await session.execute(
        text(
            "INSERT INTO deployments (id, project_id, environment_id, change_set_id, status, "
            "manifests, manifest_digest, namespace) "
            "VALUES (:id, :p, :e, :cs, :status, CAST(:m AS jsonb), 'sha256:m', 'default')"
        ),
        {
            "id": deployment_id,
            "p": project_id,
            "e": environment_id,
            "cs": change_set_id,
            "status": status,
            "m": '["k8s/deployment.yaml"]',
        },
    )
    return deployment_id, change_set_id


async def _status(session: AsyncSession, deployment_id: uuid.UUID) -> str:
    return (
        await session.execute(text("SELECT status FROM deployments WHERE id = :id"), {"id": deployment_id})
    ).scalar_one()


class TestTheApprovalTimeTransition:
    async def test_a_pending_deployment_becomes_applying(self, conn: AsyncSession) -> None:
        """THE TRANSITION THAT DID NOT EXIST."""
        deployment_id, change_set_id = await _deployment(conn, status="pending_approval")
        assert await _status(conn, deployment_id) == "pending_approval"

        await DeploymentDispatcher().dispatched(conn, change_set_id=change_set_id)

        assert await _status(conn, deployment_id) == "applying", (
            "the deployment is still waiting for approval after its command was minted and sent"
        )

    async def test_it_is_idempotent(self, conn: AsyncSession) -> None:
        """A redelivery must not be a second transition."""
        deployment_id, change_set_id = await _deployment(conn, status="pending_approval")
        await DeploymentDispatcher().dispatched(conn, change_set_id=change_set_id)
        await DeploymentDispatcher().dispatched(conn, change_set_id=change_set_id)
        assert await _status(conn, deployment_id) == "applying"

    # The real vocabulary, read from `DEPLOYMENT_STATUSES` rather than guessed: there is no
    # `succeeded`, and `degraded` is a settled state too -- the manifests are in the cluster and
    # something did not converge, which is not a reason to reopen it.
    @pytest.mark.parametrize("settled", ["applied", "degraded", "failed", "rolled_back"])
    async def test_it_refuses_to_reopen_a_settled_deployment(self, conn: AsyncSession, settled: str) -> None:
        """SCOPED BY STATUS, NOT ONLY BY CHANGE SET.

        A redelivery of an already-completed change set must not drag a settled deployment back into
        `applying`: the timeline would claim a second attempt that never happened, and `rollback_target`
        would offer a deployment that is not a target. `_OPEN_STATUSES` guards the other side for the same
        reason.
        """
        deployment_id, change_set_id = await _deployment(conn, status=settled)

        await DeploymentDispatcher().dispatched(conn, change_set_id=change_set_id)

        assert await _status(conn, deployment_id) == settled

    async def test_a_change_set_carrying_no_deployment_is_not_an_error(self, conn: AsyncSession) -> None:
        """Most change sets are generated artifacts rather than deployments, and this hook runs for all of
        them. Raising here would make every artifact approval fail."""
        project_id = uuid.uuid4()
        change_set_id = uuid.uuid4()
        await conn.execute(
            text("INSERT INTO projects (id, name, path) VALUES (:id, :n, '/tmp/y')"),
            {"id": project_id, "n": f"nodep-{project_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO change_sets (id, project_id, status, origin, blast_radius_score, "
                "blast_radius_verdict, policy_bundle_digest) "
                "VALUES (:id, :p, 'approved', 'generation', 1, 'allow', 'sha256:t')"
            ),
            {"id": change_set_id, "p": project_id},
        )

        await DeploymentDispatcher().dispatched(conn, change_set_id=change_set_id)


class TestTheDispatcherSatisfiesTheProtocolTheChokepointDeclares:
    async def test_it_is_accepted_where_the_chokepoint_expects_one(self) -> None:
        """Structural rather than nominal: the chokepoint may not import `deployments/`, so the only thing
        binding the two is the Protocol's shape."""
        from src.governance.chokepoint import ChangeSetDispatcher

        assert isinstance(DeploymentDispatcher(), ChangeSetDispatcher)

    async def test_the_chokepoint_still_cannot_import_deployments(self) -> None:
        """The reason the Protocol exists at all. A hook that reached the domain directly would satisfy
        every test above and break §2.2.1's boundary."""
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parent.parent.parent / "src" / "governance" / "chokepoint.py"
        ).read_text(encoding="utf-8")
        assert "from ..deployments" not in source
        assert "from src.deployments" not in source
        assert "import deployments" not in source
