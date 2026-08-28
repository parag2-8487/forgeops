# SPDX-License-Identifier: FSL-1.1-ALv2
"""Project CRUD, activity and readiness (design.md Â§6.5 revision `0009`, Â§11.3, Â§11.4).

**No migration accompanies this.** The `projects` table and the `Project` SQLModel have existed
since revision `0009`, with `tenant_id`, a JSONB `settings` column, `created_at` and an `onupdate`
`updated_at` â€” and `change_sets.project_id` and `generation_runs.project_id` are both foreign keys
into it. What was missing was never the schema: these handlers simply never opened a session.

That is the same shape as `src/approvals/` in this pass, and it is worth naming because it changes
how the remaining gaps should be read. `create_project` built a `ProjectResponse` from its own
request body and returned it with a fresh UUID, so a create appeared to succeed and stored nothing.
`get_project` returned a fixed record â€” name `"Sample Project"`, path `/workspace/sample` â€” **for any
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
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit.writer import AuditDraft
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

_COLUMNS = "id, tenant_id, name, path, repo_url, settings, created_at, updated_at, archived_at"

#: The most tags one project may carry. A ceiling rather than a default: tags are a filter, and a
#: project with a thousand of them is not being filtered, it is being used as a free-text field.
MAX_TAGS_PER_PROJECT = 50


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
    #: NULL while the project is active (revision `0013`, PRD FR-05). Soft and reversible.
    archived_at: datetime | None = None
    #: PRD FR-02. Read from `project_tags`, which revision `0009` created and nothing wrote to.
    tags: list[str] = Field(default_factory=list)
    #: PRD FR-03, and it is THIS caller's favourite rather than the project's. See
    #: `models.ProjectFavourite` for why that distinction needed a table.
    favourite: bool = False
    #: How many files this project has in the codebase index.
    #:
    #: HERE RATHER THAN A READINESS SCORE, deliberately. The projects screen used to render
    #: `readinessScore: 0` as a hardcoded literal for every project, so every project displayed a
    #: zero regardless of its real score. The fix is not to compute the score for each row: a real
    #: score is a `ReadinessEngine` evaluation over the project's whole index, and running one per
    #: row would make a 25-project page do 25 index walks. So the list reports whether there is
    #: anything to score — `0` means "never scanned", which the UI says in words — and the score
    #: itself stays on the detail screen where exactly one is computed.
    indexed_file_count: int = 0


class ProjectPage(BaseModel):
    """A keyset page of projects, newest first."""

    projects: list[ProjectResponse]
    next_cursor: str | None = None


class ActivityFeedItem(BaseModel):
    id: str
    action: str
    timestamp: str
    details: str


class ReadinessCheckResponse(BaseModel):
    """One checklist item, with the indexed path that satisfied it.

    Â§1.4 asks for "checklist checks" and a report that says "why it matters"; this is that, on the
    wire. `evidence` is what makes a score auditable â€” a category at 40 with no evidence anywhere is
    indistinguishable from a bug in the scorer.
    """

    id: str
    category: str
    passed: bool
    points: int
    max_points: int
    evidence: str
    why_it_matters: str


class ReadinessReportResponse(BaseModel):
    project_id: uuid.UUID
    score: int
    level: str
    summary_report: str
    recommendations: list[str]
    #: The Â§1.4 category breakdown: Containerization, CI/CD, Orchestration, Env Config, Security,
    #: IaC â€” each 0-100. Â§12.6 step 5 asserts on a "category breakdown", so the chart on the
    #: readiness screen had nothing real to render while this was dropped. Serialised straight from
    #: the engine's own model, so it cannot drift into a different set of categories than the one it
    #: computes.
    #:
    #: The set CHANGED with this commit. It was five categories â€” documentation, test coverage, CI
    #: config, security policy, containerisation â€” which omitted three of Â§1.4's six and scored two
    #: that Â§1.4 does not name. Test evidence is now a check inside CI/CD rather than a category of
    #: its own, which is also what removes the old `has_tests` default of true.
    categories: dict[str, int] = Field(default_factory=dict)
    #: False when the project has no indexed files. A caller must be able to tell "scored zero" from
    #: "never scanned", and a score alone cannot.
    indexed: bool = True
    #: How many indexed paths the score was computed from.
    evaluated_paths: int = 0
    checks: list[ReadinessCheckResponse] = Field(default_factory=list)


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
    contains `+00:00`, and `+` in a query string decodes to a space â€” so a raw cursor round-tripped
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

    The settings validation is unchanged and still refuses an unknown key rather than dropping it â€”
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


async def hydrate_projects(
    session: AsyncSession,
    rows: list[dict[str, Any]],
    *,
    user_id: uuid.UUID,
) -> list[ProjectResponse]:
    """Attach tags, this user's favourite flag and the index file count to a page of rows.

    THREE QUERIES FOR THE WHOLE PAGE, not three per row. The obvious shape — a helper that loads one
    project's tags and is called in a loop — is a 3N query pattern, and on a 25-row page that is 75
    round trips to render a list. `= ANY(:ids)` collapses each of the three to one.

    Returns rows in the order given. The caller's ordering is the keyset order the cursor depends on,
    so re-sorting here would break paging in a way that only shows up on page two.
    """
    if not rows:
        return []
    ids = [row["id"] for row in rows]

    tag_rows = await session.execute(
        text("SELECT project_id, tag FROM project_tags WHERE project_id = ANY(:ids) ORDER BY tag"),
        {"ids": ids},
    )
    tags: dict[uuid.UUID, list[str]] = {}
    for tag_row in tag_rows.mappings():
        tags.setdefault(tag_row["project_id"], []).append(str(tag_row["tag"]))

    favourite_rows = await session.execute(
        text("SELECT project_id FROM project_favourites WHERE user_id = :user_id AND project_id = ANY(:ids)"),
        {"user_id": user_id, "ids": ids},
    )
    favourites = {row["project_id"] for row in favourite_rows.mappings()}

    count_rows = await session.execute(
        text("SELECT project_id, count(*) AS files FROM file_tree WHERE project_id = ANY(:ids) GROUP BY project_id"),
        {"ids": ids},
    )
    counts = {row["project_id"]: int(row["files"]) for row in count_rows.mappings()}

    return [
        ProjectResponse(
            **row,
            tags=tags.get(row["id"], []),
            favourite=row["id"] in favourites,
            indexed_file_count=counts.get(row["id"], 0),
        )
        for row in rows
    ]


@router.get("", response_model=ProjectPage, summary="List the caller's projects")
async def list_projects(
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
    search: str | None = Query(default=None, max_length=200, description="Substring of the name or path"),
    tag: list[str] | None = Query(default=None, description="Repeatable. A project must carry EVERY tag given."),
    favourite: bool | None = Query(default=None, description="Restrict to (or exclude) this caller's favourites"),
    archived: bool = Query(default=False, description="Show archived projects instead of active ones"),
) -> ProjectPage:
    """The endpoint whose absence forced the UI to be a lookup box, now filterable server-side.

    Keyset over `(created_at, id)`, for the same reason the approvals list is: an offset shifts as
    rows are inserted, so a client paging while projects are created sees duplicates or gaps.

    **THE FILTERS RUN IN SQL, WHICH IS THE POINT OF THEM EXISTING.** PRD FR-02 and FR-03 ask for
    search, tags and favourites, and the cheap version is to fetch every project and filter in the
    browser. That is wrong in a way that is invisible while a tenant has six projects and total once
    it has six hundred: it pages the wrong set, so `next_cursor` describes the unfiltered sequence
    and the second page of a search silently omits matches.

    `search` matches the name or the path, case-insensitively, and `%` and `_` in the term are
    LITERAL characters rather than wildcards the caller did not ask for.

    `tag` is repeatable and conjunctive: `?tag=prod&tag=eu` means both, not either. Conjunctive
    because that is what narrowing means — a filter that widened as you added terms would be
    surprising in exactly the situation you were trying to narrow.

    `archived` is a separate view rather than an inclusive flag: the default excludes archived
    projects, and asking for them shows those and only those. An "include archived" flag would mix a
    project you have finished with the ones you have not, in one list, which is the state archiving
    exists to end.
    """
    params: dict[str, Any] = {"limit": limit + 1, **_tenant_params(principal.tenant_id)}
    clauses = [_tenant_clause(principal.tenant_id)]
    clauses.append("archived_at IS NOT NULL" if archived else "archived_at IS NULL")

    if search:
        # `position(... in lower(...)) > 0` rather than LIKE, so a `%` typed into the search box is
        # the character the operator typed. Same reasoning as `query_symbols` in analysis/routes.py.
        clauses.append("(position(lower(:search) in lower(name)) > 0 OR position(lower(:search) in lower(path)) > 0)")
        params["search"] = search.strip()

    if tag:
        # Every tag, not any: `HAVING count(DISTINCT tag) = :tag_count` over the requested set is
        # what makes it conjunctive without one subquery per tag.
        wanted = sorted({t.strip() for t in tag if t.strip()})
        if wanted:
            clauses.append(
                "id IN (SELECT project_id FROM project_tags WHERE tag = ANY(:tags) "
                "GROUP BY project_id HAVING count(DISTINCT tag) = :tag_count)"
            )
            params["tags"] = wanted
            params["tag_count"] = len(wanted)

    if favourite is not None:
        predicate = "EXISTS" if favourite else "NOT EXISTS"
        clauses.append(
            f"{predicate} (SELECT 1 FROM project_favourites f "
            "WHERE f.project_id = projects.id AND f.user_id = :favourite_user)"
        )
        params["favourite_user"] = principal.user_id

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
    rows = [dict(r) for r in result.mappings()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    return ProjectPage(
        projects=await hydrate_projects(session, rows, user_id=principal.user_id),
        next_cursor=encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None,
    )


@router.get("/tags", response_model=list[str], summary="Every tag in use in this tenant")
async def list_tags(
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[str]:
    """The distinct tags across the caller's visible projects, so the filter can be a chooser.

    Declared BEFORE `/{project_id}` on purpose: FastAPI matches in declaration order, and a literal
    path registered after a parameterised one would be swallowed by it and answer
    `422 value is not a valid uuid` for a request that is perfectly well formed.
    """
    result = await session.execute(
        text(
            "SELECT DISTINCT t.tag FROM project_tags t JOIN projects p ON p.id = t.project_id "
            f"WHERE {_tenant_clause(principal.tenant_id)} ORDER BY t.tag"
        ),
        _tenant_params(principal.tenant_id),
    )
    return [str(row[0]) for row in result]


async def load_visible_project(
    session: AsyncSession, *, project_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> dict[str, Any]:
    """One project the tenant may see, or the non-disclosing 403.

    A 404 here would distinguish "no such project" from "another tenant's project", which Â§4.2 and
    Q-20 forbid: the body must be byte-identical either way or it becomes an enumeration oracle for
    project ids. `GovernanceChokepoint._admit` takes the same line for the same reason.

    Public rather than `_load_project` because `analysis/routes.py` scopes the codebase index by
    project too, and a second copy of this rule is a second place for it to drift.
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

    Hydrated through the same helper the list uses, so the detail screen and the list row cannot
    disagree about a project's tags or whether it is a favourite.
    """
    row = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    hydrated = await hydrate_projects(session, [row], user_id=principal.user_id)
    return hydrated[0]


@router.get("/{project_id}/activity", response_model=list[ActivityFeedItem], summary="Project activity")
async def get_project_activity(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ActivityFeedItem]:
    """The project's real governance history, read from `audit_events`.

    This returned a single hardcoded `project_created` item dated 2026-08-06. The audit log is
    append-only, hash-chained and already indexed on `(project_id, created_at)`, so an activity feed
    is a query over it rather than a second store â€” and using it means the feed cannot disagree with
    the audit viewer about what happened.

    Existence is checked first so a caller cannot use an empty feed to learn that an id is
    unallocated.
    """
    await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
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
    """Score the project from its INDEX and return the Â§1.4 category breakdown.

    What this used to score was `projects.settings`: `config_files` was literally
    `sorted(settings.keys())` and `manifests` was `["Dockerfile"] if repo_url else []`, with
    `"README.md"` substituted when the settings were empty. So the number described what an operator
    had typed into the create form â€” it moved when the settings changed and stayed still when the
    repository did â€” and a project with a `favourite` flag scored points for documentation.

    It now reads `file_tree` and `file_contents`: the rows an agent scan persisted through
    `POST /analysis/codebase/{project_id}/index`. `projects.settings` still participates, but only as
    a REFINEMENT â€” `ignore_globs` removes paths from the evidence, because a path the operator has
    declared out of scope is not evidence about the deployment. It can no longer stand in for the
    repository.

    A project with no indexed files scores zero, says `indexed: false`, and recommends running a
    scan. That is the honest answer; scoring the settings instead would produce a number that looks
    like a measurement of a repository nobody has read.
    """
    project = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)

    from ..core.index_evidence import load_index_evidence
    from .readiness import ReadinessEngine, apply_ignore_globs

    evidence = await load_index_evidence(session, project_id=project_id)
    settings = project.get("settings") or {}
    ignore_globs = settings.get("ignore_globs") if isinstance(settings, dict) else None
    refined = evidence.model_copy(update={"paths": apply_ignore_globs(evidence.paths, ignore_globs)})

    result = ReadinessEngine().evaluate(refined)

    if result.indexed:
        summary = (
            f"{project['name']} scored {result.overall_score}/100 and is categorised as "
            f"'{result.level}', from {result.evaluated_paths} indexed file(s) in this project's "
            f"codebase index."
        )
    else:
        summary = (
            f"{project['name']} has no indexed files, so there is nothing to score. Run an agent "
            f"scan for this project; readiness is measured from the repository, not from its "
            f"stored settings."
        )

    return ReadinessReportResponse(
        project_id=project_id,
        score=result.overall_score,
        level=result.level,
        summary_report=summary,
        recommendations=result.recommendations,
        categories=result.breakdown.model_dump(),
        indexed=result.indexed,
        evaluated_paths=result.evaluated_paths,
        checks=[
            ReadinessCheckResponse(
                id=check.id,
                category=check.category,
                passed=check.passed,
                points=check.points,
                max_points=check.max_points,
                evidence=check.evidence,
                why_it_matters=check.why_it_matters,
            )
            for check in result.checks
        ],
    )


# ─── PRD FR-05: archive and delete ───────────────────────────────────────────────────────────────
#
# Both were P0 with no route on either side. They are two operations rather than one with a flag,
# because they answer different questions: archive is "I have stopped working on this", delete is
# "this should not exist". Conflating them is how an archive ends up destroying an index.


class ArchiveRequest(BaseModel):
    """Why the project is being archived. Required, for NFR-14's "why"."""

    model_config = {"extra": "forbid"}

    reason: str = Field(min_length=1, max_length=500)


class DeleteRequest(BaseModel):
    """Why the project is being deleted, and an explicit acknowledgement of what goes with it.

    `confirm_name` must equal the project's own name. Not a checkbox: a checkbox is clicked by
    reflex, and typing the name is the smallest gesture that proves the operator knows WHICH project
    they are deleting. The same reason `gh repo delete` asks.
    """

    model_config = {"extra": "forbid"}

    reason: str = Field(min_length=1, max_length=500)
    confirm_name: str = Field(min_length=1, max_length=200)


class DeletionReport(BaseModel):
    """What the delete actually removed, counted before the statement ran.

    Returned rather than a bare 204 because the cascade is wide and an operator is entitled to know
    what it took. A 204 would make "deleted an empty project" and "deleted 977 chunks and 14 change
    sets" the same response.
    """

    project_id: uuid.UUID
    #: Rows removed per table, by the database's own cascade.
    cascaded: dict[str, int]
    #: Audit rows that mention this project and SURVIVE. See the route's docstring.
    audit_events_retained: int


def _writer(request: Request) -> Any:
    """The composed audit writer, or a loud failure.

    A `RuntimeError` rather than a degraded write, following `audit/routes.py::_writer`: archiving or
    deleting a project without a record would be exactly the unlogged action §1.9 exists to forbid,
    and a missing writer is a composition error rather than a fact about the caller.
    """
    writer = getattr(request.app.state, "audit_writer", None)
    if writer is None:
        raise RuntimeError(
            "app.state.audit_writer is not composed; project archive and delete depend on it "
            "(design §1.9, §11.9). create_app() must build it in the lifespan."
        )
    return writer


@router.post("/{project_id}/archive", response_model=ProjectResponse, summary="Archive a project (soft)")
async def archive_project(
    project_id: uuid.UUID,
    body: ArchiveRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectResponse:
    """Set `archived_at`, leaving every row that references the project untouched.

    Reversible by `POST /unarchive`, and idempotent: archiving an already-archived project keeps the
    FIRST timestamp rather than moving it, because the answer to "when did this stop being worked on"
    is the first time, not the most recent click.

    Nothing is deleted, nothing is unindexed, no device is unpaired. That is the whole distinction
    from delete, and it is why archive is not implemented as "delete but keep the row".
    """
    before = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)

    await session.execute(
        text("UPDATE projects SET archived_at = now() WHERE id = :id AND archived_at IS NULL"),
        {"id": project_id},
    )
    await _writer(request).append(
        session,
        AuditDraft(
            action="project_archived",
            resource_kind="project",
            resource_id=str(project_id),
            reason=body.reason,
            outcome="allowed",
            actor_kind="user",
            actor_user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            before_state={"archived_at": None if before["archived_at"] is None else before["archived_at"].isoformat()},
            after_state={"archived": True},
        ),
    )
    await session.commit()
    row = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    return (await hydrate_projects(session, [row], user_id=principal.user_id))[0]


@router.post("/{project_id}/unarchive", response_model=ProjectResponse, summary="Restore an archived project")
async def unarchive_project(
    project_id: uuid.UUID,
    body: ArchiveRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectResponse:
    """Clear `archived_at`. The other half of "soft, reversible" actually being reversible."""
    before = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)

    await session.execute(text("UPDATE projects SET archived_at = NULL WHERE id = :id"), {"id": project_id})
    await _writer(request).append(
        session,
        AuditDraft(
            action="project_unarchived",
            resource_kind="project",
            resource_id=str(project_id),
            reason=body.reason,
            outcome="allowed",
            actor_kind="user",
            actor_user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            before_state={"archived_at": None if before["archived_at"] is None else before["archived_at"].isoformat()},
            after_state={"archived": False},
        ),
    )
    await session.commit()
    row = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    return (await hydrate_projects(session, [row], user_id=principal.user_id))[0]


#: Every table whose rows the database removes when a project row is deleted, each named with the
#: constraint that does it. Enumerated as DATA so the count in `DeletionReport` cannot drift from the
#: cascade: a table added with an `ON DELETE CASCADE` reference to `projects.id` and not added here
#: would be silently deleted and silently unreported, which is the shape of surprise this route
#: exists to prevent.
#:
#: `file_contents` is reached transitively through `file_tree`, so it is counted through the join
#: rather than by `project_id`, which it does not have.
CASCADING_TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("file_tree", "SELECT count(*) FROM file_tree WHERE project_id = :id"),
    (
        "file_contents",
        "SELECT count(*) FROM file_contents c JOIN file_tree f ON f.id = c.file_id WHERE f.project_id = :id",
    ),
    ("file_dependencies", "SELECT count(*) FROM file_dependencies WHERE project_id = :id"),
    (
        "embeddings",
        "SELECT count(*) FROM embeddings e JOIN file_tree f ON f.id = e.file_id WHERE f.project_id = :id",
    ),
    (
        "embeddings_local",
        "SELECT count(*) FROM embeddings_local l JOIN file_tree f ON f.id = l.file_id WHERE f.project_id = :id",
    ),
    ("change_sets", "SELECT count(*) FROM change_sets WHERE project_id = :id"),
    ("generation_runs", "SELECT count(*) FROM generation_runs WHERE project_id = :id"),
    ("agent_devices", "SELECT count(*) FROM agent_devices WHERE project_id = :id"),
    ("secrets", "SELECT count(*) FROM secrets WHERE project_id = :id"),
    ("policies", "SELECT count(*) FROM policies WHERE project_id = :id"),
    ("policy_bundles", "SELECT count(*) FROM policy_bundles WHERE project_id = :id"),
    ("project_tags", "SELECT count(*) FROM project_tags WHERE project_id = :id"),
    ("project_favourites", "SELECT count(*) FROM project_favourites WHERE project_id = :id"),
)


@router.delete("/{project_id}", response_model=DeletionReport, summary="Delete a project and its dependent rows")
async def delete_project(
    project_id: uuid.UUID,
    body: DeleteRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeletionReport:
    """Delete the project row and let the database's declared cascade take its dependents.

    WHAT GOES, AND WHAT STAYS. Thirteen tables carry `ON DELETE CASCADE` references into
    `projects.id` — the file tree, its contents, the dependency graph, both vector tables, change
    sets, generation runs, agent devices, secrets, policies, policy bundles, tags and favourites — and
    all of them go. They are counted BEFORE the delete and reported, because "deleted" with no
    statement of scope is not informed consent.

    **The audit trail survives, and it survives by design rather than by exemption.** Revision `0007`
    gave `audit_events.project_id` no foreign key, with the reason written into the migration: "an
    immutable log that cascades away when a project is deleted is not an immutable log." The table
    additionally REVOKEs `UPDATE` and `DELETE` from the application role and carries a trigger that
    raises on either, so there is no statement this route could issue that would remove those rows,
    and none it could issue to blank the column either. The rows keep the id of a project that no
    longer exists, which is the honest record: the events happened, and the thing they happened to is
    gone. `audit_events_retained` says how many, so the operator learns the history outlives the
    project instead of discovering it later.

    A DELETE rather than an archive-with-a-longer-name: archive already exists above and is the
    reversible option. This one is not reversible, which is why it asks for the name.
    """
    project = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)

    if body.confirm_name != project["name"]:
        # Reported through the framework's validation path so it renders as the registered
        # `validation-failed` 422 rather than needing a new problem type; Appendix C.1 is closed.
        # The message does not echo the real name — the caller can read it from `GET /projects/{id}`,
        # and repeating it here would turn a confirmation into a fill-in-the-blank.
        raise RequestValidationError(
            [
                {
                    "loc": ("body", "confirm_name"),
                    "msg": "confirm_name must exactly match the project's name",
                    "type": "value_error",
                }
            ]
        )

    cascaded: dict[str, int] = {}
    for table, query in CASCADING_TABLES:
        count = await session.execute(text(query), {"id": project_id})
        cascaded[table] = int(count.scalar() or 0)

    retained = await session.execute(
        text("SELECT count(*) FROM audit_events WHERE project_id = :id"), {"id": project_id}
    )
    audit_events_retained = int(retained.scalar() or 0)

    # The audit row is written BEFORE the delete, deliberately. Writing it afterwards would leave a
    # window in which the project is gone and no record says who removed it, and the hash chain is
    # appended to inside this transaction — so if the delete fails, the record rolls back with it.
    await _writer(request).append(
        session,
        AuditDraft(
            action="project_deleted",
            resource_kind="project",
            resource_id=str(project_id),
            reason=body.reason,
            outcome="allowed",
            actor_kind="user",
            actor_user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            before_state={"name": project["name"], "path": project["path"], "cascaded": cascaded},
            after_state=None,
        ),
    )
    await session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})
    await session.commit()

    return DeletionReport(
        project_id=project_id,
        cascaded=cascaded,
        audit_events_retained=audit_events_retained,
    )


# ─── PRD FR-02 tags, FR-03 favourites ────────────────────────────────────────────────────────────


class TagRequest(BaseModel):
    model_config = {"extra": "forbid"}

    #: Lower-cased and trimmed by the handler. A tag is an identifier for filtering, and `Prod` and
    #: `prod` being two tags would split the filter without anyone intending it.
    tag: str = Field(min_length=1, max_length=64)


@router.put("/{project_id}/tags", response_model=ProjectResponse, summary="Add a tag to a project")
async def add_project_tag(
    project_id: uuid.UUID,
    body: TagRequest,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectResponse:
    """Add one tag. Idempotent, and bounded.

    PUT rather than POST because adding a tag that is already there is not a second tag —
    `ON CONFLICT DO NOTHING` against `uq_project_tags_project_id_tag` makes the operation
    idempotent, which is what PUT promises and POST does not.
    """
    await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    tag = body.tag.strip().lower()
    if not tag:
        raise RequestValidationError(
            [{"loc": ("body", "tag"), "msg": "a tag cannot be only whitespace", "type": "value_error"}]
        )

    existing = await session.execute(
        text("SELECT count(*) FROM project_tags WHERE project_id = :id"), {"id": project_id}
    )
    if int(existing.scalar() or 0) >= MAX_TAGS_PER_PROJECT:
        raise RequestValidationError(
            [
                {
                    "loc": ("body", "tag"),
                    "msg": f"a project may carry at most {MAX_TAGS_PER_PROJECT} tags",
                    "type": "value_error",
                }
            ]
        )

    await session.execute(
        text(
            "INSERT INTO project_tags (id, project_id, tag) VALUES (:id, :project_id, :tag) "
            "ON CONFLICT (project_id, tag) DO NOTHING"
        ),
        {"id": uuid.uuid4(), "project_id": project_id, "tag": tag},
    )
    await session.commit()
    row = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    return (await hydrate_projects(session, [row], user_id=principal.user_id))[0]


@router.delete("/{project_id}/tags/{tag}", response_model=ProjectResponse, summary="Remove a tag")
async def remove_project_tag(
    project_id: uuid.UUID,
    tag: str,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectResponse:
    """Remove one tag. Idempotent: removing a tag that is not there succeeds."""
    await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    await session.execute(
        text("DELETE FROM project_tags WHERE project_id = :project_id AND tag = :tag"),
        {"project_id": project_id, "tag": tag.strip().lower()},
    )
    await session.commit()
    row = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    return (await hydrate_projects(session, [row], user_id=principal.user_id))[0]


@router.put("/{project_id}/favourite", response_model=ProjectResponse, summary="Mark as this caller's favourite")
async def add_favourite(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectResponse:
    """Star the project for the CALLER, not for the project.

    `user_id` comes from the verified principal and there is no parameter for it, so one person
    cannot star a project on another's behalf. That is the whole reason this is a table rather than
    the `projects.settings.favourite` flag that has existed since revision `0009`.
    """
    await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    await session.execute(
        text(
            "INSERT INTO project_favourites (user_id, project_id) VALUES (:user_id, :project_id) "
            "ON CONFLICT (user_id, project_id) DO NOTHING"
        ),
        {"user_id": principal.user_id, "project_id": project_id},
    )
    await session.commit()
    row = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    return (await hydrate_projects(session, [row], user_id=principal.user_id))[0]


@router.delete("/{project_id}/favourite", response_model=ProjectResponse, summary="Unstar")
async def remove_favourite(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProjectResponse:
    """Unstar for the caller. Idempotent."""
    await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    await session.execute(
        text("DELETE FROM project_favourites WHERE user_id = :user_id AND project_id = :project_id"),
        {"user_id": principal.user_id, "project_id": project_id},
    )
    await session.commit()
    row = await load_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)
    return (await hydrate_projects(session, [row], user_id=principal.user_id))[0]
