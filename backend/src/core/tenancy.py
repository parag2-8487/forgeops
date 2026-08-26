# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tenant context — middleware row 6 (design.md §4.3, §6.7, §7.12, §17.1 D-35).

What this is, and deliberately is not
-------------------------------------
Phase 0 left `tenant_id` on the tables as a nullable seam with no RLS policies and no
`NOT NULL` (OQ-15). D-35 keeps that decision: Phase 1 fills middleware row 6 and issues
`SET LOCAL app.tenant_id` per transaction, and **creates no RLS policy and sets no
column NOT NULL**. Those are Phase 2 work, and doing them early would be an
irreversible schema commitment made before there is a multi-tenant product to shape it.

So what does this buy now? The database-side variable exists and is correct, which
means a Phase 2 RLS policy can be switched on without touching a single query, and any
tenant-scoped query written between now and then can be audited against a real value
rather than a plan.

Why `SET LOCAL` and not `SET`
----------------------------
`SET` persists for the life of the *connection*. With a pooled connection — which is
the only configuration this runs in — the next request to borrow that connection
inherits the previous tenant's id. That is a cross-tenant data leak whose likelihood
rises with traffic, and it is invisible in any single-request test.

`SET LOCAL` is scoped to the transaction and reverts at COMMIT or ROLLBACK. The
integration test asserts exactly the property that distinguishes the two: the variable
is visible inside the transaction and **absent in the next transaction on the same
pooled connection**.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

#: The current request's tenant, or None when the caller is unauthenticated or the
#: route is public.
#:
#: A `ContextVar` rather than a request attribute because `get_session` is a FastAPI
#: dependency that does not receive the tenant, and threading it through every call
#: signature would put a tenancy argument on functions that have no business knowing
#: about tenancy. `contextvars` is also the only mechanism that survives `await`
#: correctly under asyncio, which is why Phase 0's trace id uses it too.
tenant_id_var: ContextVar[str | None] = ContextVar("forgeops_tenant_id", default=None)


def current_tenant_id() -> str | None:
    """The tenant resolved for the current request, or None."""
    return tenant_id_var.get()


class TenantContextMiddleware(BaseHTTPMiddleware):
    """Row 6 of the §4.3 stack: resolve the tenant from the verified principal.

    Placement matters. This runs INSIDE authentication, so by the time it executes the
    principal has already been verified — it reads an established identity rather than
    trusting a request header. A `X-Tenant-Id` header is never consulted, because a
    client-supplied tenant is a client-chosen authorization scope.

    Phase 1 has no principal on `request.state` until task 6.1 lands
    `require_principal`, so the resolution is written to read whatever is there and
    fall back to None. That is not a stub: None is the correct answer for a public
    route and for an unauthenticated request, and `SET LOCAL` is simply skipped in that
    case rather than being issued with a placeholder that a later RLS policy would
    match.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        token = tenant_id_var.set(_resolve_tenant(request))
        try:
            return await call_next(request)
        finally:
            # Reset rather than leaving the value set: under an event loop the same
            # context can be reused, and a leaked tenant is the failure this whole
            # module exists to prevent.
            tenant_id_var.reset(token)


def _resolve_tenant(request: Request) -> str | None:
    """Read the tenant from the VERIFIED principal, never from the request.

    Kept separate from `dispatch` so task 6.1 has one obvious place to extend when
    `Principal` arrives, and so the "never from a header" rule is testable in isolation.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return None
    tenant = getattr(principal, "tenant_id", None)
    return str(tenant) if tenant else None


# ─── Project row visibility (§4.2, Q-20) ──────────────────────────────────────
#
# WHY THIS IS HERE AND NOT IN `projects`
#
# Two domains scope by project: `projects` serves the project itself, and `analysis` serves the
# codebase index for one. §2.2.1 bans them from importing each other, so a rule both must obey
# cannot live in either — and this rule in particular must not be duplicated, because the whole
# point of it is that two endpoints answer IDENTICALLY. It is a tenancy rule, which is what this
# module is for.

import uuid  # noqa: E402
from typing import Any  # noqa: E402

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from .errors import forbidden_problem  # noqa: E402

#: The columns every project read returns. One list, so two endpoints cannot return different
#: shapes for the same row.
PROJECT_COLUMNS = "id, tenant_id, name, path, repo_url, settings, created_at, updated_at"


def tenant_clause(tenant_id: uuid.UUID | None) -> str:
    """Row visibility. `IS NULL` is matched explicitly rather than skipped.

    A principal with no tenant must see only rows with no tenant. Omitting the predicate in that
    case would show it every project in the installation.
    """
    return "tenant_id IS NULL" if tenant_id is None else "tenant_id = :tenant_id"


def tenant_params(tenant_id: uuid.UUID | None) -> dict[str, Any]:
    return {} if tenant_id is None else {"tenant_id": tenant_id}


async def load_visible_project(
    session: AsyncSession, *, project_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> dict[str, Any]:
    """One project the tenant may see, or the non-disclosing 403.

    A 404 here would distinguish "no such project" from "another tenant's project", which §4.2 and
    Q-20 forbid: the body must be byte-identical either way or it becomes an enumeration oracle for
    project ids. `GovernanceChokepoint._admit` takes the same line for the same reason.
    """
    result = await session.execute(
        text(f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id = :id AND {tenant_clause(tenant_id)}"),  # noqa: S608
        {"id": project_id, **tenant_params(tenant_id)},
    )
    row = result.mappings().first()
    if row is None:
        raise forbidden_problem()
    return dict(row)
