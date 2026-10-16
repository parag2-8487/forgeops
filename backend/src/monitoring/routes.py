# SPDX-License-Identifier: FSL-1.1-ALv2
"""The monitoring read surface -- Phase 2 §2.10.

WHY THE BROWSER MUST NOT TALK TO PROMETHEUS DIRECTLY, which is the whole reason this file exists.

Prometheus has no authentication. Pointing a panel at it means either exposing it publicly -- every series in
the deployment, readable by anyone -- or proxying it, and a transparent proxy is the same hole with an extra
hop. Neither passes `require_principal`, so neither can enforce which tenant is asking. And since a PromQL
string can select any series by many independent routes, a proxy that forwards the query cannot scope it
either.

So the route runs NAMED queries from `queries.CATALOGUE` and composes the tenant matcher itself from the
verified principal. A caller chooses which reviewed query runs and supplies validated label values; it never
supplies PromQL. See the module docstring in `queries.py` for why the alternative is unfixable rather than
merely risky.

THESE ARE READS AND DO NOT TOUCH THE CHOKEPOINT. Nothing here mutates anything, no agent command is minted,
no change set is compiled. What they do need is a principal and a role check, which the router-level
dependency provides.

WHY UNSCOPED QUERIES REQUIRE A HIGHER ROLE. A handful of catalogue entries have no tenant dimension to scope
by -- collector internals, HTTP server metrics recorded before a principal exists -- and each states that
reason. They cannot leak one tenant's data into another's view because the series does not distinguish
tenants at all, but they do expose deployment-wide traffic shape and telemetry health. That is operator
information, so those entries require an operator role while the tenant-scoped ones do not.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..auth.dependencies import require_principal
from ..auth.principal import Principal, UserRole
from ..core.errors import problem
from .queries import ALLOWED_WINDOWS, CATALOGUE, catalogue_listing
from .reader import MetricsReader, QueryResult

router = APIRouter(tags=["monitoring"])

# Roles permitted to run an entry that carries no tenant dimension. Stated as a constant so the test can
# assert on the same tuple the route uses rather than restating it.
OPERATOR_ROLES = (UserRole.ADMIN, UserRole.DEVELOPER)


class MetricsQueryRequest(BaseModel):
    """What a panel sends. Note the absence of any field that could carry PromQL.

    `model_config` forbids extra fields, so a client that sent `query="..."` hoping for a passthrough gets a
    422 naming the field rather than having it silently ignored -- an ignored field is indistinguishable from
    an honoured one from the caller's side.
    """

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, str] = Field(default_factory=dict)
    window: str = "1h"


def _reader(request: Request) -> MetricsReader:
    """The reader from app state, or an unconfigured one.

    An unconfigured reader is a real object that answers `unconfigured` rather than None. A None here would
    make every call site test for it, and the one that forgot would raise a 500 on a deployment that had
    simply chosen not to run monitoring.
    """
    existing = getattr(request.app.state, "metrics_reader", None)
    if isinstance(existing, MetricsReader):
        return existing
    return MetricsReader(base_url="")


def _serialise(result: QueryResult) -> dict[str, Any]:
    """The wire shape.

    `verdict` and `explanation` are NOT optional. A panel cannot receive a number from this route without
    also receiving how much to trust it, which is the point: the plausible-figure-from-a-dead-source failure
    happens when a response makes it possible to render a value while ignoring its provenance.
    """
    return {
        "verdict": result.verdict,
        "explanation": result.explanation,
        "has_numbers": result.has_numbers,
        "newest_sample_age_seconds": result.newest_sample_age_seconds,
        "series": [
            {
                "labels": one.labels,
                "points": [{"at": point.at, "value": point.value} for point in one.points],
            }
            for one in result.series
        ],
    }


@router.get("/monitoring/queries")
async def list_queries(
    principal: Annotated[Principal, Depends(require_principal)],
) -> dict[str, Any]:
    """The catalogue, so the frontend does not restate query names.

    Also the honest answer to "what can I ask for": there is no hidden query, and a name absent here will be
    refused by the route below.
    """
    return {
        "queries": catalogue_listing(),
        "windows": list(ALLOWED_WINDOWS),
        "tenant_id": str(principal.tenant_id) if principal.tenant_id else None,
    }


@router.post("/monitoring/query")
async def run_query(
    body: MetricsQueryRequest,
    principal: Annotated[Principal, Depends(require_principal)],
    reader: Annotated[MetricsReader, Depends(_reader)],
) -> dict[str, Any]:
    """Run one catalogue entry, scoped to the caller's tenant."""
    entry = CATALOGUE.get(body.name)
    if entry is None:
        # Names the catalogue rather than echoing the requested name back, so this cannot be used to probe
        # for which metric names exist in the store.
        raise problem(
            "monitoring-query-unknown",
            detail=(
                "No such query. This route runs only reviewed queries from a fixed catalogue; it does not "
                "accept PromQL. GET /monitoring/queries lists every query that exists."
            ),
        )

    if not entry.tenant_scoped and principal.role not in OPERATOR_ROLES:
        raise problem(
            "monitoring-query-forbidden",
            detail=(
                "This query covers the whole deployment rather than one tenant, because the series it "
                "reads carries no tenant dimension. It is available to operators only."
            ),
        )

    try:
        result = await reader.run(
            entry,
            tenant_id=principal.tenant_id,
            arguments=body.arguments,
            window=body.window,
        )
    except ValueError as exc:
        raise problem("monitoring-query-invalid", detail=str(exc)) from exc

    payload = _serialise(result)
    payload["name"] = entry.name
    payload["describes"] = entry.describes
    payload["kind"] = entry.kind
    # The rendered PromQL is returned for tenant-scoped queries so an operator can SEE the scoping that was
    # applied. Withholding it would make the security property unverifiable from outside, and it discloses
    # nothing the caller did not already cause: they chose the entry, and the tenant is their own.
    payload["promql"] = result.query
    return payload


@router.get("/monitoring/readiness")
async def monitoring_readiness(
    principal: Annotated[Principal, Depends(require_principal)],
    reader: Annotated[MetricsReader, Depends(_reader)],
) -> dict[str, Any]:
    """Whether this deployment can answer monitoring questions at all.

    Separate from the application's own `/readyz`, deliberately: monitoring being absent must not make the
    application look unhealthy, because it is an optional tier. What it must do is let a panel say "not
    configured" instead of drawing an empty chart that reads as "nothing is happening".
    """
    telemetry = getattr(reader, "configured", False)
    return {
        "metrics_store_configured": bool(telemetry),
        "explanation": (
            "A metrics store is configured and this deployment can answer monitoring queries."
            if telemetry
            else "No metrics store is configured. Monitoring panels have no source and will say so rather "
            "than showing zeros. Start the `observability` compose profile and set the Prometheus URL "
            "to change that."
        ),
    }
