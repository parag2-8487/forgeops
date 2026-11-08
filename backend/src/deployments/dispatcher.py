# SPDX-License-Identifier: FSL-1.1-ALv2
"""The approval-time half of a deployment's lifecycle.

TWO TRANSITIONS, AND ONLY THE SECOND EXISTED
--------------------------------------------
A deployment row is written `pending_approval` by `DeploymentService.request`, from the chokepoint's verdict
at REQUEST time. `DeploymentSettler` then writes the terminal status from the agent's report. Between them
nothing moved the row: approval minted the authority, delivered the signed envelope, and left the row saying
a human had not decided yet.

WHAT THAT COST. Every read of the deployment reported it as waiting: the §2.3 timeline, the logs -- whose
own docstring notes that a `pending_approval` deployment shows an empty log an operator reads as "nothing
happened" -- and `rollback_target`, which looks for a deployment that actually went out. The
criterion-3 E2E spec found it from the outside: it approved the change set, waited four minutes, and the row
never left `pending_approval`, so the failure that files an incident never occurred.

It is the same shape as the three defects already recorded in this codebase: `DeploymentService.complete`
with no production caller, `record_command_result`'s unreachable failure branch, and `evaluate_policy`'s
unsupplied argument. **A status written from a verdict needs a listener for the verdict CHANGING, not only
for its first value.**
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class DeploymentDispatcher:
    """Advances the deployment a change set carried from `pending_approval` to `applying`.

    Satisfies `governance.chokepoint.ChangeSetDispatcher`. Named for what it does at the transition rather
    than for the caller, because the same hook serves approval and the explicit deliver route -- both mint
    and send, and both mean the deployment is now in flight.
    """

    async def dispatched(self, session: AsyncSession, *, change_set_id: uuid.UUID) -> None:
        """Move the deployment this change set carries into `applying`.

        SCOPED BY STATUS, NOT ONLY BY CHANGE SET. `WHERE status = 'pending_approval'` makes this idempotent
        and refuses to reopen a settled row: a redelivery of an already-completed change set must not drag a
        `failed` deployment back into `applying`, which would make the timeline claim a second attempt that
        never happened. `_OPEN_STATUSES` in the service exists for the same reason on the other side.

        A change set carrying no deployment updates nothing and is not an error -- most change sets are
        generated artifacts, not deployments, and this hook runs for all of them.
        """
        await session.execute(
            text(
                "UPDATE deployments SET status = 'applying' WHERE change_set_id = :cs AND status = 'pending_approval'"
            ),
            {"cs": change_set_id},
        )
