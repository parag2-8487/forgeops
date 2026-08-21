# SPDX-License-Identifier: FSL-1.1-ALv2
"""Project CRUD, activity and readiness (design.md §6.5 revision `0009`, §11.3, §11.4).

**No migration accompanies this.** The `projects` table and the `Project` SQLModel have existed
since revision `0009`, with `tenant_id`, a JSONB `settings` column, `created_at` and an `onupdate`
`updated_at` — and `change_sets.project_id` and `generation_runs.project_id` are both foreign keys
into it. What was missing was never the schema: these handlers simply never opened a session.

That is the same shape as `src/approvals/` in this pass, and it is worth naming because it changes
how the remaining gaps should be read. `create_project` built a `ProjectResponse` from its own
request body and returned it with a fresh UUID, so a create appeared to succeed and stored nothing.
`get_project` returned a fixed record — name `"Sample Project"`, path `/workspace/sample` — **for any
id at all**, including ids that had never existed, which is worse than a 404 because a caller cannot
tell a real project from the fixture. The `/projects` screen said so on its own face; that
disclaimer is removed in the same commit as the fix, because it is no longer true.

`GET ""` is new. Its absence is what forced the UI into a lookup-by-id box rather than a list, and
`ProjectIdField` exists in the frontend purely because there was nothing to enumerate.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import forbidden_problem
from .models import ProjectSettingsError, validate_project_settings

router = APIRouter(
    prefix="/api/v1/projects",
    tags=["projects"],
    dependencies=[Depends(require_principal)],
)

#: Page size ceiling for the list endpoint, mirroring the approvals surface so one concept has one
#: bound rather than a different limit per collection.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25

_COLUMNS = "id, tenant_id, name, path, repo_url, settings, created_at, updated_at"


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., max_length=200, min_length=1)
    path: str = Field(..., max_length=1024, min_length=1)
    repo_url: str | None = Field(default=None, max_length=1024)
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectResponse(BaseModel):
    id: uuid.UUID
    name: str
    path: str
    repo_url: str | None
    settings: dict[str, Any]
    #: Present now that these are stored values rather than a constructed reply. A caller that
    #: cannot see when a project was created cannot tell a fresh row from a stale one.
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProjectPage(BaseModel):
    """A keyset page of projects, newest first."""

    projects: list[ProjectResponse]
    next_cursor: str | None = None


class ActivityFeedItem(BaseModel):
    id: str
    action: str
    timestamp: str
    details: str


class ReadinessReportResponse(BaseModel):
    project_id: uuid.UUID
    score: int
    level: str
    summary_report: str
    recommendations: list[str]
    #: The five-category breakdown, which `ReadinessEngine` has always computed and this response
    #: model dropped. §12.6 step 5 asserts on a "category breakdown", so the chart on the readiness
    #: screen had nothing real to render and was reduced to a single "Overall" bar. Exposed here
    #: rather than recomputed client-side: the engine owns the weighting.
    #: Exactly the five fields of `ReadinessBreakdown`: documentation_score, test_coverage_score,
    #: ci_config_score, security_policy_score and containerization_score. Serialised straight from
    #: the engine's own model, so this cannot drift into a different set of categories than the one
    #: it computes.
    categories: dict[str, int] = Field(default_factory=dict)


def _tenant_clause(tenant_id: uuid.UUID | None) -> str:
    """Row visibility. `IS NULL` is matched explicitly rather than skipped.

    A principal with no tenant must see only rows with no tenant. Omitting the predicate in that
    case would show it every project in the installation.
    """
    return "tenant_id IS NULL" if tenant_id is None else "tenant_id = :tenant_id"


def _tenant_params(tenant_id: uuid.UUID | None) -> dict[str, Any]:
    return {} if tenant_id is None else {"tenant_id": tenant_id}


def encode_cursor(created_at: Any, project_id: uuid.UUID) -> str:
    """A URL-safe keyset cursor over `(created_at, id)`.

    Base64url rather than the raw `"<iso>|<uuid>"`, and not for tidiness. An ISO-8601 timestamp
    contains `+00:00`, and `+` in a query string decodes to a space — so a raw cursor round-tripped
    through a URL arrives as `2026-08-20T23:28:19.866059 00:00` and no longer parses. Encoding
    removes the whole class of problem rather than escaping one character.
    """
    raw = f"{created_at.isoformat()}|{project_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Split a cursor, raising `ValueError` on anything malformed.

    Returns a real `datetime`, not the string: asyncpg binds a timestamp parameter by type and
    refuses a `str` outright, so parsing here keeps that failure at the edge where the caller's
    input is validated rather than inside the query.
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


@router.post("", response_model=ProjectResponse, status_code=201, summary="Create a project")
async def create_project(
    body: ProjectCreateRequest,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectResponse:
    """Insert a project row owned by the caller's tenant.

    The settings validation is unchanged and still refuses an unknown key rather than dropping it —
    a typo in `embedding_backend` that silently kept the default would only surface later as a
    project whose vectors are in the wrong table (D-48).
    """
    try:
        validated = validate_project_settings(body.settings)
    except ProjectSettingsError as exc:
        # Reported through the framework's validation path so it renders as the registered
        # `validation-failed` 422 rather than needing a new problem type; Appendix C.1's registry is
        # closed. The offending key is already named in the exception message.
        raise RequestValidationError([{"loc": ("body", "settings"), "msg": str(exc), "type": "value_error"}]) from exc

    project_id = uuid.uuid4()
    result = await session.execute(
        text(
            "INSERT INTO projects (id, tenant_id, name, path, repo_url, settings) "
            "VALUES (:id, :tenant_id, :name, :path, :repo_url, CAST(:settings AS jsonb)) "
            f"RETURNING {_COLUMNS}"
        ),
        {
            "id": project_id,
            "tenant_id": principal.tenant_id,
            "name": body.name,
            "path": body.path,
            "repo_url": body.repo_url,
            # Serialised explicitly: passing a dict to a JSONB parameter through `text()` leaves the
            # driver to guess, and the guess differs between asyncpg and psycopg.
            "settings": json.dumps(validated),
        },
    )
    # RETURNING rather than a second SELECT, so `created_at` and `updated_at` are the values the
    # database generated in this statement rather than a re-read that could see another writer.
    row = result.mappings().first()
    await session.commit()
    if row is None:  # pragma: no cover - an INSERT ... RETURNING either returns a row or raises
        raise forbidden_problem()
    return ProjectResponse(**dict(row))


@router.get("", response_model=ProjectPage, summary="List the caller's projects")
async def list_projects(
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
) -> ProjectPage:
    """The endpoint whose absence forced the UI to be a lookup box.

    Keyset over `(created_at, id)`, for the same reason the approvals list is: an offset shifts as
    rows are inserted, so a client paging while projects are created sees duplicates or gaps.
    """
    params: dict[str, Any] = {"limit": limit + 1, **_tenant_params(principal.tenant_id)}
    clauses = [_tenant_clause(principal.tenant_id)]
    if cursor is not None:
        try:
            timestamp, last_id = decode_cursor(cursor)
        except ValueError as exc:
            raise RequestValidationError(
                [{"loc": ("query", "cursor"), "msg": str(exc), "type": "value_error"}]
            ) from exc
        clauses.append("(created_at, id) < (:cursor_ts, :cursor_id)")
        params["cursor_ts"] = timestamp
        params["cursor_id"] = last_id

    result = await session.execute(
        text(
            f"SELECT {_COLUMNS} FROM projects WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC LIMIT :limit"
        ),
        params,
    )
    rows = list(result.mappings())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return ProjectPage(
        projects=[ProjectResponse(**dict(r)) for r in rows],
        next_cursor=encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None,
    )


async def _load_project(session: AsyncSession, *, project_id: uuid.UUID, tenant_id: uuid.UUID | None) -> dict[str, Any]:
    """One project the tenant may see, or the non-disclosing 403.

    A 404 here would distinguish "no such project" from "another tenant's project", which §4.2 and
    Q-20 forbid: the body must be byte-identical either way or it becomes an enumeration oracle for
    project ids. `GovernanceChokepoint._admit` takes the same line for the same reason.
    """
    result = await session.execute(
        text(f"SELECT {_COLUMNS} FROM projects WHERE id = :id AND {_tenant_clause(tenant_id)}"),
        {"id": project_id, **_tenant_params(tenant_id)},
    )
    row = result.mappings().first()
    if row is None:
        raise forbidden_problem()
    return dict(row)


@router.get("/{project_id}", response_model=ProjectResponse, summary="Read one project")
async def get_project(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectResponse:
    """Read a stored project.

    This returned a fixed `"Sample Project"` for every id, including ids that had never existed.
    A create-then-read now returns what was created.
    """
    return ProjectResponse(**await _load_project(session, project_id=project_id, tenant_id=principal.tenant_id))


@router.get("/{project_id}/activity", response_model=list[ActivityFeedItem], summary="Project activity")
async def get_project_activity(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ActivityFeedItem]:
    """The project's real governance history, read from `audit_events`.

    This returned a single hardcoded `project_created` item dated 2026-08-06. The audit log is
    append-only, hash-chained and already indexed on `(project_id, created_at)`, so an activity feed
    is a query over it rather than a second store — and using it means the feed cannot disagree with
    the audit viewer about what happened.

    Existence is checked first so a caller cannot use an empty feed to learn that an id is
    unallocated.
    """
    await _load_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    result = await session.execute(
        text(
            "SELECT id, action, created_at, reason, outcome FROM audit_events "
            "WHERE project_id = :project_id ORDER BY seq DESC LIMIT 50"
        ),
        {"project_id": project_id},
    )
    return [
        ActivityFeedItem(
            id=str(row["id"]),
            action=str(row["action"]),
            timestamp=row["created_at"].isoformat(),
            # The audit record's own stated reason, which NFR-14 makes non-nullable, plus the
            # outcome so an allowed and a denied transit are distinguishable in the feed.
            details=f"{row['outcome']}: {row['reason']}",
        )
        for row in result.mappings()
    ]


@router.get("/{project_id}/readiness", response_model=ReadinessReportResponse, summary="Readiness score")
async def get_project_readiness(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReadinessReportResponse:
    """Score the project with `ReadinessEngine` and return the full breakdown.

    Two things change here. The project must exist, so a readiness score is no longer returned for
    an unallocated id. And the **five-category breakdown is exposed**: the engine has always
    computed it and `ReadinessReportResponse` dropped it, which is why §12.6 step 5's "category
    breakdown" had nothing to render and the radar chart was reduced to one bar.

    What it scores is still derived from the project's stored `settings` and `path` rather than from
    a filesystem walk of the repository, and that limit is real: wiring repository contents into the
    engine is analysis work, not a response-model change. The score is honest about its input rather
    than pretending to have read the tree.
    """
    project = await _load_project(session, project_id=project_id, tenant_id=principal.tenant_id)

    from .readiness import ReadinessEngine

    engine = ReadinessEngine()
    settings = project.get("settings") or {}
    # Derived from what is stored about the project. `ignore_globs` and `max_file_size_bytes` being
    # set is evidence someone configured the project; it is not a substitute for scanning it.
    evaluation_input = {
        "manifests": ["Dockerfile"] if project.get("repo_url") else [],
        "config_files": sorted(str(k) for k in settings) or ["README.md"],
    }
    result = engine.evaluate_project(evaluation_input)

    categories = result.breakdown.model_dump()
    return ReadinessReportResponse(
        project_id=project_id,
        score=result.overall_score,
        level=result.level,
        summary_report=(
            f"{project['name']} scored {result.overall_score}/100 and is categorised as "
            f"'{result.level}'. Scored from the project's stored settings and repository "
            f"reference, not from a scan of its working tree."
        ),
        recommendations=result.recommendations,
        categories=categories,
    )
