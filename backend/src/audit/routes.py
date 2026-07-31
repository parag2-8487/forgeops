# SPDX-License-Identifier: FSL-1.1-ALv2
"""The audit read surface (design.md §11.9, §4.4, criterion 9).

Two endpoints, and the second is the point of the first existing at all:

* `GET /api/v1/audit/events` — the query API, filtered and cursor-paginated.
* `GET /api/v1/audit/verify` — chain verification, **admin only**. §11.9: exposed "so
  tamper-evidence is a product feature, not an internal helper". A verifier only an engineer with
  a database shell can run is a verifier nobody runs, and an audit log nobody verifies is a log
  whose integrity is an assumption.

Why the cursor is a `seq`, not an offset
---------------------------------------
`seq` is a `BIGSERIAL` total order that never changes, so `seq < cursor` is stable under
concurrent appends: a row inserted while a client pages cannot shift a later page's contents.
`OFFSET` would, and on an append-only table that grows during paging it would silently skip rows —
in an audit log, a silently skipped row is the whole failure.

There is deliberately no write endpoint. Records are produced by governance transits (§11.6) and
by the hub for agent-reported results; a route that could post an audit record would be a route
that could forge one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal, require_role
from ..auth.models import UserRole
from ..auth.principal import Principal
from ..core.db import get_session
from .writer import ACTOR_KINDS, OUTCOMES, AuditWriter

router = APIRouter(
    prefix="/api/v1/audit",
    tags=["audit"],
    # Deny by default (§4.4), attached at the router so a route added here is protected the
    # moment it is declared. `/verify` narrows further to admin below.
    dependencies=[Depends(require_principal)],
)

#: The largest page a caller may ask for. A cap rather than a default, because an audit table is
#: the one table in the schema designed to grow without bound (§11.9) and an uncapped `limit` is a
#: denial of service with a friendly name.
MAX_PAGE_SIZE = 200

#: Query-parameter types derived from the writer's closed vocabularies, so the API and the writer
#: cannot disagree about what a valid `actor_kind` or `outcome` is. Spelled as `Literal` over the
#: same tuples rather than retyped: a retyped list is one edit away from accepting a value the
#: writer would refuse, and the filter would then return an empty page instead of an error.
ActorKindQuery = Literal[ACTOR_KINDS]  # type: ignore[valid-type]
OutcomeQuery = Literal[OUTCOMES]  # type: ignore[valid-type]


class AuditEventOut(BaseModel):
    """One record, as the API renders it.

    `prev_hash` and `hash` are hex, so a caller can verify the chain independently without this
    service being trusted to answer honestly about itself — which is the only kind of tamper
    evidence worth having.
    """

    seq: int
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    project_id: uuid.UUID | None
    actor_kind: str
    actor_user_id: uuid.UUID | None
    actor_device_id: uuid.UUID | None
    action: str
    resource_kind: str
    resource_id: str | None
    reason: str
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    outcome: str
    trace_id: str | None
    prev_hash: str
    hash: str
    created_at: datetime


class AuditPage(BaseModel):
    """A page plus the cursor that fetches the next one.

    `next_cursor` is `None` exactly when the page is the last one, so a client's loop terminates
    on a value rather than on a count comparison it has to get right.
    """

    events: list[AuditEventOut]
    next_cursor: int | None


class DivergenceOut(BaseModel):
    seq: int
    kind: str
    detail: str
    expected_hash: str
    stored_hash: str


class ChainVerificationOut(BaseModel):
    """`ok` is derived from `divergence`, never a stored or separately-computed flag."""

    ok: bool
    tenant_id: uuid.UUID | None
    from_seq: int
    rows_checked: int
    divergence: DivergenceOut | None


def _writer(request: Request) -> AuditWriter:
    """The composed writer, or a loud failure.

    A missing writer is a composition error in the app factory, not a fact about the caller — the
    same reasoning `require_principal` applies to a missing verifier. Reporting it as a 404 or an
    empty page would let a broken deployment look like an empty audit log, which is the most
    dangerous thing this surface could ever say.
    """
    writer = getattr(request.app.state, "audit_writer", None)
    if writer is None:
        raise RuntimeError(
            "app.state.audit_writer is not composed; the audit surface depends on it "
            "(design §11.1, §11.9). create_app() must build it in the lifespan."
        )
    return writer


@router.get("/events", response_model=AuditPage, summary="Query audit records")
async def list_events(
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    actor_user_id: Annotated[uuid.UUID | None, Query()] = None,
    actor_kind: Annotated[ActorKindQuery | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    resource_kind: Annotated[str | None, Query()] = None,
    resource_id: Annotated[str | None, Query()] = None,
    outcome: Annotated[OutcomeQuery | None, Query()] = None,
    cursor: Annotated[int | None, Query(ge=1, description="Return records with seq < cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> AuditPage:
    """Newest first, filtered, cursor-paginated.

    **Tenant scope is taken from the principal, never from a parameter.** A `tenant_id` query
    argument would be a cross-tenant read waiting for a caller to try it, and D-35 leaves the
    column nullable in Phase 1 with no RLS policy behind it — so the confinement has to be here.

    `actor_kind` and `outcome` are typed `Literal`, so an unknown value is refused by FastAPI's
    own validation. Raising a hand-made problem here instead would mean inventing a type Appendix
    C.1 does not register, which is exactly what the registry exists to prevent.
    """
    clauses = ["tenant_id IS NOT DISTINCT FROM :tenant"]
    params: dict[str, Any] = {"tenant": principal.tenant_id, "limit": limit}
    for column, value in (
        ("project_id", project_id),
        ("actor_user_id", actor_user_id),
        ("actor_kind", actor_kind),
        ("action", action),
        ("resource_kind", resource_kind),
        ("resource_id", resource_id),
        ("outcome", outcome),
    ):
        if value is not None:
            # Bound as parameters, never interpolated. The column names come from this closed
            # tuple and the values never touch the SQL text, so there is no injection surface
            # even though the WHERE clause is assembled.
            clauses.append(f"{column} = :{column}")
            params[column] = value
    if cursor is not None:
        clauses.append("seq < :cursor")
        params["cursor"] = cursor

    # One row more than asked for, so "is there a next page" is answered by the data rather than
    # by a second COUNT query that could disagree with it.
    result = await session.execute(
        text(
            "SELECT seq, id, tenant_id, project_id, actor_user_id, actor_device_id, actor_kind, "
            "action, resource_kind, resource_id, reason, before_state, after_state, outcome, "
            "trace_id, prev_hash, hash, created_at FROM audit_events "
            f"WHERE {' AND '.join(clauses)} ORDER BY seq DESC LIMIT :limit + 1"
        ),
        params,
    )
    rows = result.mappings().all()
    has_more = len(rows) > limit
    page = rows[:limit]
    events = [
        AuditEventOut(
            **{key: value for key, value in row.items() if key not in ("prev_hash", "hash")},
            prev_hash=bytes(row["prev_hash"]).hex(),
            hash=bytes(row["hash"]).hex(),
        )
        for row in page
    ]
    return AuditPage(events=events, next_cursor=int(page[-1]["seq"]) if has_more and page else None)


@router.get(
    "/verify",
    response_model=ChainVerificationOut,
    summary="Verify the audit hash chain",
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)
async def verify_chain(
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    request: Request,
    since_seq: Annotated[int, Query(ge=0, description="Recompute from this seq onward")] = 0,
) -> ChainVerificationOut:
    """Recompute the caller's tenant chain and report the first divergence.

    Admin only. Not because the result is sensitive — it is a hash comparison — but because the
    operation reads every row from `since_seq` and an unbounded recomputation available to any
    authenticated caller is a cheap way to make the database everybody's problem. `since_seq` is
    what makes an incremental check possible on a large table.

    A **200 with `ok: false`** rather than an error status. A divergence is a successful
    verification that found something; returning 5xx would make "the chain is broken"
    indistinguishable from "the verifier is broken", and those need different responses.
    """
    verification = await _writer(request).verify_chain(session, tenant_id=principal.tenant_id, since_seq=since_seq)
    divergence = verification.divergence
    return ChainVerificationOut(
        ok=verification.ok,
        tenant_id=verification.tenant_id,
        from_seq=verification.from_seq,
        rows_checked=verification.rows_checked,
        divergence=(
            DivergenceOut(
                seq=divergence.seq,
                kind=divergence.kind,
                detail=divergence.detail,
                expected_hash=divergence.expected_hash.hex(),
                stored_hash=divergence.stored_hash.hex(),
            )
            if divergence is not None
            else None
        ),
    )


__all__ = ["AuditEventOut", "AuditPage", "ChainVerificationOut", "MAX_PAGE_SIZE", "router"]
