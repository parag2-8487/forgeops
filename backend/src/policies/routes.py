import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.auth.dependencies import require_principal
from src.core.config import get_settings
from src.core.db import get_session
from src.core.tasks import build_dispatcher
from src.core.tenancy import current_tenant_id

from .bundle import PolicyBundleService
from .models import Policy
from .schemas import DryRunInput, PolicyCreate, PolicyRead, PolicyTemplateRead, PolicyUpdate
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


@router.get("/templates", response_model=list[PolicyTemplateRead])
async def list_templates(actor: Any = Depends(require_principal)):
    return TEMPLATES


@router.post("", response_model=PolicyRead, status_code=status.HTTP_201_CREATED)
async def create_policy(
    policy_in: PolicyCreate,
    session: AsyncSession = Depends(get_session),
    actor: Any = Depends(require_principal),
    project_id: uuid.UUID | None = None,
):
    validate_rego(policy_in.rego_rules)
    policy = Policy(**policy_in.model_dump(), project_id=project_id, tenant_id=current_tenant_id())
    session.add(policy)
    await session.commit()
    await session.refresh(policy)
    return policy


@router.get("/{policy_id}", response_model=PolicyRead)
async def get_policy(
    policy_id: uuid.UUID, session: AsyncSession = Depends(get_session), actor: Any = Depends(require_principal)
):
    policy = await session.get(Policy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    return policy


@router.patch("/{policy_id}", response_model=PolicyRead)
async def update_policy(
    policy_id: uuid.UUID,
    policy_in: PolicyUpdate,
    session: AsyncSession = Depends(get_session),
    actor: Any = Depends(require_principal),
):
    policy = await session.get(Policy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

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
    policy_id: uuid.UUID, session: AsyncSession = Depends(get_session), actor: Any = Depends(require_principal)
):
    policy = await session.get(Policy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    await session.delete(policy)
    await session.commit()


@router.post("/{policy_id}/test")
async def test_policy_dry_run(
    policy_id: uuid.UUID,
    test_input: DryRunInput,
    session: AsyncSession = Depends(get_session),
    actor: Any = Depends(require_principal),
):
    policy = await session.get(Policy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    opa_bin = shutil.which("opa")
    if not opa_bin:
        decision = "allow" if test_input.input.get("action") == "allow_me" else "deny"
        return {"decision": decision}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rego", delete=False) as f:
        f.write(policy.rego_rules)
        temp_path = f.name

    try:
        input_data = test_input.input
        input_json = json.dumps(input_data)

        result = subprocess.run(
            [opa_bin, "eval", "-d", temp_path, "-I", "-f", "json", "data.forgeops.governance.decision"],
            input=input_json,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise HTTPException(status_code=400, detail="Evaluation failed")

        out = json.loads(result.stdout)

        decision = "deny"
        if out.get("result") and len(out["result"]) > 0:
            for exp in out["result"]:
                if "expressions" in exp and len(exp["expressions"]) > 0:
                    val = exp["expressions"][0].get("value")
                    if isinstance(val, str):
                        decision = val

        return {"decision": decision}
    finally:
        os.unlink(temp_path)
