# SPDX-License-Identifier: FSL-1.1-ALv2
"""Reads over the real change-set tables (design.md §3.6, §6.5 revision `0004`, §11.6).

**What this replaces.** The previous `ApprovalService` held `self._store: dict[str, ChangeSetResponse]`
on a module-level singleton and called the contents persisted. Three consequences, all of which the
`/approvals` UI panel named as its reason for not existing: state vanished on restart; two workers
disagreed, since each had its own dict; and nothing was auditable, because a dict has no log.

**Why there is no `create`, `approve` or `reject` here.** Those transitions already exist, in
`GovernanceChokepoint`, over these same tables, with optimistic concurrency on
`change_sets.version`, an `approvals` row per decision and an audit event per transit. A second
implementation of a state transition is a second state machine, and §3.6's edges are asserted by
Q-22 against the chokepoint's — so this module is reads only, and the routes delegate every write.
That is the whole reason the old module was a hazard rather than merely incomplete: it was a
parallel implementation of a governed path, and the governed path is the product.

Every query is **tenant-scoped**, and that is not defensive habit. `change_sets.tenant_id` exists
because §6.7 makes tenancy a row-level property, and an approvals list that forgot the predicate
would show one customer another customer's file contents — `change_items.new_content` is the actual
source being changed.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..governance.models import CHANGE_SET_STATUSES
from .schemas import (
    ApprovalRead,
    ChangeItemRead,
    ChangeSetDetail,
    ChangeSetPage,
    ChangeSetSummary,
)

#: Page size ceiling. A reviewer's list is not a bulk export, and `change_items.new_content` can be
#: a whole file, so an unbounded page is a memory hazard as much as a latency one.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25

_SUMMARY_COLUMNS = (
    "id, project_id, status, origin, blast_radius_score, blast_radius_verdict, "
    "version, generation_run_id, created_at, applied_at"
)


def encode_cursor(created_at: Any, change_set_id: uuid.UUID) -> str:
    """A URL-safe keyset cursor over `(created_at, id)`.

    Both halves, because `created_at` alone is not unique: two change sets submitted in the same
    transaction share a timestamp, and a cursor on the timestamp alone would either skip one or
    repeat it. The id breaks the tie and is itself unique, so the pair is a total order.

    Base64url rather than the raw `"<iso>|<uuid>"`, because an ISO-8601 timestamp contains `+00:00`
    and `+` in a query string decodes to a space — a raw cursor round-tripped through a URL comes
    back as `...19.866059 00:00` and no longer parses. Found by the projects list's own test rather
    than reasoned about in advance, and fixed in both places for the same reason.
    """
    raw = f"{created_at.isoformat()}|{change_set_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Split a cursor, raising `ValueError` on anything malformed.

    Deliberately strict. A cursor that failed to parse and silently became "start from the
    beginning" would make a paging client loop forever over page one.

    Returns a real `datetime`: asyncpg binds a timestamp parameter by type and refuses a `str`, so
    parsing belongs here, at the edge where the caller's input is validated.
    """
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - any decode failure is one malformed-cursor answer
        raise ValueError("a cursor must be base64url of '<created_at>|<id>'") from exc
    timestamp, _, raw_id = raw.partition("|")
    if not timestamp or not raw_id:
        raise ValueError("a cursor must be base64url of '<created_at>|<id>'")
    return datetime.fromisoformat(timestamp), uuid.UUID(raw_id)


class ApprovalService:
    """Reads of `change_sets` and their items and decisions.

    Stateless: every method takes the caller's `AsyncSession`. That is the same discipline
    `AuditWriter` follows and it exists so a read and the write it accompanies can share one
    transaction — and so this object cannot accumulate the per-process state that was the previous
    version's defect.
    """

    async def list_change_sets(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID | None,
        project_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> ChangeSetPage:
        """A tenant-scoped page of change sets, newest first.

        `status` is validated against `CHANGE_SET_STATUSES` rather than interpolated, so an unknown
        state is a 400 from the caller's perspective instead of a query that quietly matches
        nothing — an empty list and an invalid filter are different answers.
        """
        if status is not None and status not in CHANGE_SET_STATUSES:
            raise ValueError(f"unknown change-set status {status!r}; expected one of {CHANGE_SET_STATUSES}")

        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        params: dict[str, Any] = {"limit": limit + 1}
        clauses: list[str] = []

        # Tenant scoping. `IS NULL` is matched explicitly rather than skipped: a principal with no
        # tenant must see only rows with no tenant, never every row.
        if tenant_id is None:
            clauses.append("tenant_id IS NULL")
        else:
            clauses.append("tenant_id = :tenant_id")
            params["tenant_id"] = tenant_id

        if project_id is not None:
            clauses.append("project_id = :project_id")
            params["project_id"] = project_id
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status
        if cursor is not None:
            timestamp, last_id = decode_cursor(cursor)
            # Row-value comparison, which Postgres evaluates lexicographically — the exact
            # semantics keyset pagination needs, in one predicate an index can serve.
            clauses.append("(created_at, id) < (:cursor_ts, :cursor_id)")
            params["cursor_ts"] = timestamp
            params["cursor_id"] = last_id

        where = " AND ".join(clauses)
        result = await session.execute(
            text(
                f"SELECT {_SUMMARY_COLUMNS} FROM change_sets WHERE {where} "
                "ORDER BY created_at DESC, id DESC LIMIT :limit"
            ),
            params,
        )
        rows = list(result.mappings())

        # One row over the page size was requested, so "is there a next page" is answered by
        # observation rather than by a second COUNT query that could disagree with the first.
        has_more = len(rows) > limit
        rows = rows[:limit]
        summaries = [ChangeSetSummary(**dict(row)) for row in rows]
        next_cursor = encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None
        return ChangeSetPage(change_sets=summaries, next_cursor=next_cursor)

    async def get_change_set(
        self,
        session: AsyncSession,
        *,
        change_set_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
    ) -> ChangeSetDetail | None:
        """One change set with its items and decisions, or `None` if the tenant cannot see it.

        `None` covers both "does not exist" and "belongs to another tenant", and the caller turns
        both into the same answer. Distinguishing them would make this an existence oracle for
        other tenants' change-set ids, which is the enumeration problem §4.2 closes for projects.
        """
        head = await session.execute(
            text(
                f"SELECT {_SUMMARY_COLUMNS} FROM change_sets WHERE id = :id AND "
                + ("tenant_id IS NULL" if tenant_id is None else "tenant_id = :tenant_id")
            ),
            {"id": change_set_id, **({} if tenant_id is None else {"tenant_id": tenant_id})},
        )
        row = head.mappings().first()
        if row is None:
            return None

        items = await session.execute(
            text(
                "SELECT id, file_path, action, old_content, new_content, old_hash, new_hash, ordinal "
                "FROM change_items WHERE change_set_id = :id ORDER BY ordinal"
            ),
            {"id": change_set_id},
        )
        decisions = await session.execute(
            text(
                "SELECT id, approver_id, status, comment, created_at FROM approvals "
                "WHERE change_set_id = :id ORDER BY created_at"
            ),
            {"id": change_set_id},
        )
        return ChangeSetDetail(
            **dict(row),
            items=[ChangeItemRead(**dict(i)) for i in items.mappings()],
            approvals=[ApprovalRead(**dict(a)) for a in decisions.mappings()],
        )


#: A single stateless instance. Safe to share precisely because it holds nothing — which is the
#: property the previous module-level singleton lacked, since its state was the bug.
_approval_service = ApprovalService()


def get_approval_service() -> ApprovalService:
    return _approval_service
