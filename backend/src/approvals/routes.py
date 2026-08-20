# SPDX-License-Identifier: FSL-1.1-ALv2
"""The change-approval HTTP surface (design.md §3.6, §4.4, §11.6, criterion 10 steps 8-9).

This router existed and was deliberately absent from `create_app` for two reasons, both now fixed
rather than tolerated.

**It required no authentication.** Neither the router nor any route depended on
`require_principal`, so mounting it would have exposed change-set approval — the one control the
whole governance design funnels through — to anonymous callers. Worse than anonymous: `approve`
took `approver: str = "admin"` as a **query parameter**, so the caller supplied the identity the
record would be attributed to, and it defaulted to an administrator. That inverts the rule the rest
of the system is built on, that a principal is constructed only by a verifier and never from
request data. `scripts/check-route-auth.py` would have failed the build, correctly, and that gate is
why this was never mounted.

**Its store was a dictionary.** See `service.py`.

Authentication is at **router level**, so it cannot be forgotten on a route added later, and there
are no public exemptions here — every route in this file requires a verified principal, and
`PUBLIC_ROUTES` is not touched.

**Authorisation is not repeated here, and that is deliberate.** The mutating routes delegate to
`GovernanceChokepoint`, whose `_admit` resolves the principal against the project and tenant and
whose stage 1 asks OPA. Adding a role check in this module would be a second policy that nobody
reviews alongside the first — exactly what `require_permission`'s docstring warns against. The read
routes are tenant-scoped in SQL, which is where row visibility belongs.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import forbidden_problem
from ..governance.chokepoint import GovernanceChokepoint, Submission
from ..governance.models import CHANGE_SET_STATUSES
from .schemas import (
    ApprovalDecisionRequest,
    ChangeSetDetail,
    ChangeSetPage,
)
from .service import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApprovalService, get_approval_service

#: The status filter's type, GENERATED from §3.6's vocabulary rather than restated.
#:
#: Two things follow from deriving it. FastAPI rejects an unknown value with the registered
#: `validation-failed` 422 before the handler runs, so no new problem type has to be invented at a
#: raise site — which is what Appendix C.1's closed registry exists to prevent. And the allowed
#: values appear in the OpenAPI document automatically, so `api.md` can be generated from the
#: schema instead of listing them by hand and drifting.
ChangeSetStatusFilter = StrEnum("ChangeSetStatusFilter", {status.upper(): status for status in CHANGE_SET_STATUSES})

router = APIRouter(
    prefix="/api/v1/approvals",
    tags=["approvals"],
    dependencies=[Depends(require_principal)],
)


def _chokepoint(request: Request) -> GovernanceChokepoint:
    """The composed chokepoint, or a loud failure.

    A `RuntimeError` rather than a 503, following `require_principal`'s reasoning: an unassembled
    chokepoint is a composition error in the app factory, not a fact about the caller, and
    reporting it as a service outage would let a broken deployment look like a working one
    refusing work.
    """
    chokepoint = getattr(request.app.state, "governance_chokepoint", None)
    if chokepoint is None:
        raise RuntimeError(
            "app.state.governance_chokepoint is not composed; the approvals surface depends on it "
            "(design §11.6). create_app() must build it in the lifespan."
        )
    return chokepoint


@router.get("", response_model=ChangeSetPage, summary="List change sets awaiting or past decision")
async def list_change_sets(
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
    project_id: uuid.UUID | None = None,
    status: ChangeSetStatusFilter | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
) -> ChangeSetPage:
    """A keyset page of the caller's tenant's change sets, newest first."""
    try:
        return await service.list_change_sets(
            session,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            status=status.value if status is not None else None,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        # A malformed cursor is the caller's mistake, and it is reported through the framework's
        # own validation path so it renders as the registered `validation-failed` 422 with the
        # offending parameter named — rather than as an empty page, which would look like an
        # answer and send a paging client round the same page forever.
        raise RequestValidationError([{"loc": ("query", "cursor"), "msg": str(exc), "type": "value_error"}]) from exc


@router.get("/{change_set_id}", response_model=ChangeSetDetail, summary="Read one change set and its diff")
async def get_change_set(
    change_set_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> ChangeSetDetail:
    """One change set with its `change_items` and every decision recorded against it."""
    detail = await service.get_change_set(session, change_set_id=change_set_id, tenant_id=principal.tenant_id)
    if detail is None:
        # The non-disclosing 403, not a 404, and the same body whether the change set does not
        # exist or belongs to another tenant. §4.2 and Q-20 require a forbidden response that is
        # byte-identical in both cases; a 404 here would be an enumeration oracle for change-set
        # ids, exactly as it would be for project ids. `_admit` takes the same line.
        raise forbidden_problem()
    return detail


@router.post("/{change_set_id}/approve", summary="Approve a pending change set")
async def approve_change_set(
    change_set_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Approve, then mint authority and deliver the envelope — the full §11.6 transit.

    The approver is `principal.user_id` and cannot be supplied by the caller. Delegated to the
    chokepoint rather than reimplemented, so approval keeps its optimistic-concurrency check, its
    `approvals` row, its audit event and its re-evaluation of policy at apply time.
    """
    submission = await _chokepoint(request).approve(
        session,
        change_set_id=change_set_id,
        principal=principal,
        comment=body.comment,
        expected_version=body.expected_version,
    )
    return _decision_response(submission)


@router.post("/{change_set_id}/reject", summary="Reject a pending change set")
async def reject_change_set(
    change_set_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Refuse a pending change set: §3.6's `pending_approval → rejected` edge.

    Nothing is minted and nothing is delivered, because there is nothing to apply. The refusal is
    still a governance event with a row and an audit record — under the old dictionary it was
    neither.
    """
    submission = await _chokepoint(request).reject(
        session,
        change_set_id=change_set_id,
        principal=principal,
        comment=body.comment,
        expected_version=body.expected_version,
    )
    return _decision_response(submission)


@router.post("/{change_set_id}/revert", summary="Revert an applied change set")
async def revert_change_set(
    change_set_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """Compile and authorise the reverse change set (§11.6, D-66).

    Named `revert`, not `rollback`, because §3.6's edge out of `applied` is `applied → reverted` and
    the old handler's name matched no edge in the state machine. `rolled_back` is a different state
    reached from `applying` when an apply fails — conflating them would make the two
    indistinguishable on the record.

    A revert is a mutation and goes through all six stages with its own fresh authority: reusing the
    original's would make rollback a privileged back door.
    """
    submission = await _chokepoint(request).revert(session, change_set_id=change_set_id, principal=principal)
    return _decision_response(submission)


def _decision_response(submission: Submission) -> dict[str, object]:
    """Project a `Submission` onto the wire.

    `command_delivered` is a boolean rather than the envelope: a signed command carries an
    authority token and a nonce, and echoing those to a browser would hand a reviewer material that
    only the agent should hold.
    """
    return {
        "change_set_id": str(submission.change_set_id) if submission.change_set_id else None,
        "status": submission.status,
        "outcome": submission.outcome,
        "audit_seq": submission.audit_seq,
        "approval_id": str(submission.approval_id) if submission.approval_id else None,
        "blast_radius_score": submission.blast_radius_score,
        "blast_radius_verdict": submission.blast_radius_verdict,
        "reverse_change_set_id": (str(submission.reverse_change_set_id) if submission.reverse_change_set_id else None),
        "command_delivered": submission.command is not None,
    }
