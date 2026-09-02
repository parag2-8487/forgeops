import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from src.auth.dependencies import require_principal
from src.auth.principal import Principal
from src.core.config import get_settings
from src.core.db import get_session
from src.core.errors import forbidden_problem, problem
from src.core.tasks import build_dispatcher

from .bundle import PolicyBundleService
from .models import Policy
from .schemas import (
    DryRunInput,
    DryRunResult,
    PolicyCreate,
    PolicyPage,
    PolicyRead,
    PolicyTemplateRead,
    PolicyUpdate,
)
from .templates import TEMPLATES
from .validation import validate_rego

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/policies",
    tags=["policies"],
    dependencies=[Depends(require_principal)],
)


async def get_bundle_service(request: Request, session: AsyncSession = Depends(get_session)) -> PolicyBundleService:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        try:
            settings = get_settings()
        except Exception:
            settings = None
    pool = await _arq_pool(request, settings)
    dispatcher = build_dispatcher(settings, pool=pool) if settings is not None else None
    agent_policies_dir = (
        Path(settings.agent_policies_dir)
        if settings and hasattr(settings, "agent_policies_dir")
        else Path("policies/agent")
    )
    return PolicyBundleService(session, agent_policies_dir, tasks=dispatcher)


async def _arq_pool(request: Request, settings: Any) -> Any | None:
    """The ARQ pool, created once on first use and cached on `app.state`.

    Nothing used to create it. `.env.example` ships `TASK_DISPATCHER=arq`, and `build_dispatcher`
    raises "TASK_DISPATCHER=arq requires an ARQ pool; call create_arq_pool first" without one, so
    `POST /policies/publish` answered 500 on the committed default configuration -- and therefore no
    bundle could be published, no device pinned to one, and the governance chokepoint refused every
    generation submission as "policy bundle stale".

    HERE rather than in the lifespan, deliberately. `arq.create_pool` connects and retries, so doing
    it at startup made an unreachable Redis a slow start instead of a readiness failure: `ci / secrets`
    timed out with no test having failed. Bounding it still charged every one of the suite's app
    constructions for a connection attempt. Creating it on the first request that needs it charges
    only that request.

    A failure returns None, so `build_dispatcher` raises a message naming the cause rather than this
    dependency inventing a different one.
    """
    if settings is None or str(getattr(settings, "task_dispatcher", "inline")) != "arq":
        return None
    existing = getattr(request.app.state, "arq_pool", None)
    if existing is not None:
        return existing

    # Imported here so `arq` is only required when it is the configured dispatcher; `inline` is a
    # fully supported mode rather than a dev fallback.
    from src.core.tasks import create_arq_pool  # noqa: PLC0415

    lock = getattr(request.app.state, "arq_pool_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.arq_pool_lock = lock
    async with lock:
        # Re-read under the lock: two concurrent first requests must not build two pools.
        existing = getattr(request.app.state, "arq_pool", None)
        if existing is not None:
            return existing
        try:
            pool = await create_arq_pool(settings)
        except Exception:
            logger.warning("the ARQ pool could not be created; work cannot be enqueued", exc_info=True)
            return None
        request.app.state.arq_pool = pool
        return pool


@router.post("/publish", status_code=status.HTTP_202_ACCEPTED)
async def publish_bundle(
    project_id: uuid.UUID | None = None,
    service: PolicyBundleService = Depends(get_bundle_service),
    actor: Any = Depends(require_principal),
) -> dict[str, Any]:
    bundle = await service.build(project_id=project_id)
    await service.publish(bundle, actor=actor)
    return {"digest": bundle.digest, "status": "publishing"}


@router.get("/active-bundle", summary="Which policy bundle is active for a project")
async def get_active_bundle(
    project_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    actor: Any = Depends(require_principal),
) -> dict[str, Any]:
    """Report the active bundle's digest, or `null` when nothing is published.

    WHY THIS EXISTS. The Onboarding screen showed "Not checked" against *publish the policy bundle*
    because no read route could report it — and that reads as "not working" rather than "nobody
    looked". It is a one-row query the chokepoint was already making on every submission
    (`_active_bundle_digest`), so the step was permanently unverifiable for want of an endpoint that
    already had its SQL written.

    IT MATTERS MORE THAN A TICK. Pairing pins a device to the project's active digest, and the
    chokepoint refuses every submission whose device pin differs from it. So "nothing is published"
    is the cause of a `policy-bundle-stale` refusal an operator would otherwise have to infer from a
    rejected change set.

    THE SAME PREDICATE THE CHOKEPOINT USES, including `project_id IS NULL`: an installation-wide
    bundle is active for a project that has none of its own, and answering `null` here while the
    chokepoint saw one would make this endpoint a second, disagreeing opinion.
    """
    row = (
        (
            await session.execute(
                text(
                    "SELECT digest, created_at FROM policy_bundles "
                    "WHERE active AND (project_id = :project OR project_id IS NULL) "
                    "ORDER BY project_id NULLS LAST, created_at DESC LIMIT 1"
                ),
                {"project": project_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return {"digest": None, "published_at": None}
    return {
        "digest": row["digest"],
        "published_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.get("/templates", response_model=list[PolicyTemplateRead])
async def list_templates(actor: Any = Depends(require_principal)):
    return TEMPLATES


#: Page size ceiling, mirroring `projects.MAX_PAGE_SIZE` so one concept has one bound.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


def _tenant_clause(tenant_id: uuid.UUID | None) -> Any:
    """Row visibility for policies.

    `IS NULL` is matched explicitly rather than skipped, for the reason `projects/routes.py` states:
    a principal with no tenant must see only rows with no tenant, and omitting the predicate in that
    case would show it every policy in the installation.
    """
    return Policy.tenant_id.is_(None) if tenant_id is None else Policy.tenant_id == tenant_id


async def load_visible_policy(session: AsyncSession, *, policy_id: uuid.UUID, tenant_id: uuid.UUID | None) -> Policy:
    """One policy the caller's tenant may see, or the non-disclosing 403.

    THE READ PATH WAS NOT TENANT-SCOPED. `get_policy`, `update_policy`, `delete_policy` and the
    dry-run all did `session.get(Policy, policy_id)` and answered `404 Policy not found` when the
    row was absent — so any authenticated caller could read, rewrite or delete another tenant's
    governance rules by id, and the 404/200 split was an enumeration oracle for policy ids on top of
    that. Fixed here rather than in four places, and answered with `forbidden_problem()` so the body
    is byte-identical whether the policy does not exist or belongs to someone else (§4.2, Q-20) —
    the same line `load_visible_project` and `GovernanceChokepoint._admit` take.
    """
    result = await session.execute(select(Policy).where(Policy.id == policy_id, _tenant_clause(tenant_id)))
    policy = result.scalar_one_or_none()
    if policy is None:
        raise forbidden_problem()
    return policy


def encode_cursor(created_at: datetime, policy_id: uuid.UUID) -> str:
    """Base64url of `"<created_at>|<id>"`, for the reason `projects.encode_cursor` gives.

    A raw cursor contains `+00:00`, and `+` in a query string decodes to a space, so the round trip
    breaks. Encoding removes the class of problem rather than escaping one character.
    """
    raw = f"{created_at.isoformat()}|{policy_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Split a cursor, raising `ValueError` on anything malformed."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - any decode failure is one malformed-cursor answer
        raise ValueError("a cursor must be base64url of '<created_at>|<id>'") from exc
    timestamp, _, raw_id = raw.partition("|")
    if not timestamp or not raw_id:
        raise ValueError("a cursor must be base64url of '<created_at>|<id>'")
    return datetime.fromisoformat(timestamp), uuid.UUID(raw_id)


@router.get("", response_model=PolicyPage, summary="List the caller's stored policies")
async def list_policies(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
    project_id: uuid.UUID | None = None,
    enabled: bool | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
) -> PolicyPage:
    """Enumerate stored policies for the caller's tenant, newest first.

    THE ROUTE THAT DID NOT EXIST. This module published publish, templates, create, read, update,
    delete and test — every operation on a policy you already know the id of, and no way to learn an
    id. So a policy management screen was unbuildable: the only enumerable thing was the immutable
    template list, which is why `/policies` was a read-only wall of templates.

    `project_id` filters to one project's policies; passing it with no value is not the same as
    omitting it, so a global policy (`project_id IS NULL`) is reachable only by omitting the filter.
    That asymmetry is deliberate — a "global" filter would be a second meaning for an absent
    parameter.
    """
    clauses = [_tenant_clause(principal.tenant_id)]
    if project_id is not None:
        clauses.append(Policy.project_id == project_id)
    if enabled is not None:
        clauses.append(Policy.enabled == enabled)
    if cursor is not None:
        try:
            timestamp, last_id = decode_cursor(cursor)
        except ValueError as exc:
            raise RequestValidationError(
                [{"loc": ("query", "cursor"), "msg": str(exc), "type": "value_error"}]
            ) from exc
        clauses.append(tuple_(Policy.created_at, Policy.id) < (timestamp, last_id))

    # One row more than asked for, so "is there a next page" is answered by the data rather than by
    # a second COUNT that could disagree with it.
    result = await session.execute(
        select(Policy).where(*clauses).order_by(Policy.created_at.desc(), Policy.id.desc()).limit(limit + 1)
    )
    rows = list(result.scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return PolicyPage(
        policies=[PolicyRead.model_validate(row, from_attributes=True) for row in rows],
        next_cursor=encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None,
    )


@router.post("", response_model=PolicyRead, status_code=status.HTTP_201_CREATED)
async def create_policy(
    policy_in: PolicyCreate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
    project_id: uuid.UUID | None = None,
):
    """Store a policy, owned by the caller's tenant.

    THE TENANT COMES FROM THE PRINCIPAL, not from `current_tenant_id()`, and that is a fix rather
    than a preference. `TenantContextMiddleware` populates the context variable from the principal it
    finds on `request.state`, so the two agree on a normal request — but they diverge wherever the
    middleware has not run for this request, and the divergence is silent: the row is written with
    `tenant_id = NULL` while the reader scopes on the principal's real tenant, so a create returns
    201 and the immediately following read returns 403 for the row it just made. `create_project`
    already reads `principal.tenant_id` for the same reason; this now matches it, and the
    create-then-read case above is what holds it there.
    """
    validate_rego(policy_in.rego_rules)
    policy = Policy(**policy_in.model_dump(), project_id=project_id, tenant_id=principal.tenant_id)
    session.add(policy)
    await session.commit()
    await session.refresh(policy)
    return policy


@router.get("/{policy_id}", response_model=PolicyRead)
async def get_policy(
    policy_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    return await load_visible_policy(session, policy_id=policy_id, tenant_id=principal.tenant_id)


@router.patch("/{policy_id}", response_model=PolicyRead)
async def update_policy(
    policy_id: uuid.UUID,
    policy_in: PolicyUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    policy = await load_visible_policy(session, policy_id=policy_id, tenant_id=principal.tenant_id)

    update_data = policy_in.model_dump(exclude_unset=True)
    if "rego_rules" in update_data and update_data["rego_rules"] is not None:
        validate_rego(update_data["rego_rules"])

    for key, value in update_data.items():
        setattr(policy, key, value)

    session.add(policy)
    await session.commit()
    await session.refresh(policy)
    return policy


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    policy = await load_visible_policy(session, policy_id=policy_id, tenant_id=principal.tenant_id)
    await session.delete(policy)
    await session.commit()


#: The Rego query the dry-run evaluates. Named as a constant because it is returned to the caller in
#: `DryRunResult.rule`: a decision the caller cannot attribute to a query is not an auditable answer.
DECISION_QUERY = "data.forgeops.governance.decision"


def _opa_version(opa_bin: str) -> str:
    """The evaluator's own version string, read from the binary.

    Read rather than assumed. `evaluated_with` exists so a stored dry-run result can be attributed to
    a specific evaluator, and a hardcoded version would make that attribution a claim rather than an
    observation the moment the image was rebuilt.
    """
    try:
        completed = subprocess.run([opa_bin, "version"], capture_output=True, text=True, timeout=10)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return "opa (version unavailable)"
    for line in completed.stdout.splitlines():
        if line.lower().startswith("version:"):
            return f"opa {line.split(':', 1)[1].strip()}"
    return "opa (version unavailable)"


@router.post("/{policy_id}/test", response_model=DryRunResult)
async def test_policy_dry_run(
    policy_id: uuid.UUID,
    test_input: DryRunInput,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> DryRunResult:
    """Evaluate this policy's Rego against a caller-supplied input document, through OPA.

    THIS ROUTE USED TO FABRICATE ITS ANSWER. When `shutil.which("opa")` found nothing it returned

        decision = "allow" if test_input.input.get("action") == "allow_me" else "deny"

    which is a verdict on a security surface that no policy engine computed, indistinguishable on
    the wire from a real one. Worse than useless: an operator testing a `deny` rule would see `deny`
    and conclude the rule worked, when the rule had not been read. It now raises the registered
    `dryrun-unavailable` (503) naming the missing binary, so the failure is legible and retryable
    rather than silent and wrong. `opa` is installed in the backend image (see `Dockerfile`), so the
    503 means a broken deployment rather than an ordinary state.

    503 rather than 500, and `dryrun-unavailable` rather than a new type: the operation cannot be
    evaluated right now and may succeed later, which is exactly what that registered type means
    (§4.2, Appendix C.1's registry is closed).
    """
    policy = await load_visible_policy(session, policy_id=policy_id, tenant_id=principal.tenant_id)

    opa_bin = shutil.which("opa")
    if not opa_bin:
        raise problem(
            "dryrun-unavailable",
            detail=(
                "The 'opa' binary is not on PATH, so this policy cannot be evaluated. A dry-run "
                "result is only meaningful if OPA produced it; no decision is returned rather than "
                "a synthesised one."
            ),
        )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rego", delete=False) as f:
        f.write(policy.rego_rules)
        temp_path = f.name

    try:
        result = subprocess.run(  # noqa: S603
            [opa_bin, "eval", "-d", temp_path, "-I", "-f", "json", DECISION_QUERY],
            input=json.dumps(test_input.input),
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            # OPA itself refused the evaluation — a rule that does not compile, or an input it
            # cannot bind. Reported as unavailable rather than as a decision, for the same reason
            # the missing binary is: there is no verdict to report.
            raise problem(
                "dryrun-unavailable",
                detail="OPA could not evaluate this policy. Check the Rego compiles and the input document is valid.",
            )

        out = json.loads(result.stdout)
        expressions = [
            expression
            for entry in (out.get("result") or [])
            for expression in (entry.get("expressions") or [])
            if "value" in expression
        ]
        if not expressions:
            # `opa eval` returns NO result set when the query is undefined for this input. That is
            # not a deny: a deny is a rule that fired, and undefined is a rule that did not. The old
            # code defaulted to "deny" here, which hid a policy that never matched behind a verdict
            # that looks like enforcement.
            return DryRunResult(
                decision="undefined",
                rule=DECISION_QUERY,
                evaluated_with=_opa_version(opa_bin),
                undefined=True,
            )

        value = expressions[0]["value"]
        return DryRunResult(
            decision=value if isinstance(value, str) else json.dumps(value),
            rule=DECISION_QUERY,
            evaluated_with=_opa_version(opa_bin),
        )
    except subprocess.TimeoutExpired as exc:
        raise problem(
            "dryrun-unavailable",
            detail="OPA did not finish evaluating this policy within 30 seconds.",
        ) from exc
    finally:
        os.unlink(temp_path)
