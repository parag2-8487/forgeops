import json
import os
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
    pool = getattr(request.app.state, "arq_pool", None)
    dispatcher = build_dispatcher(settings, pool=pool) if settings is not None else None
    agent_policies_dir = (
        Path(settings.agent_policies_dir)
        if settings and hasattr(settings, "agent_policies_dir")
        else Path("policies/agent")
    )
    return PolicyBundleService(session, agent_policies_dir, tasks=dispatcher)


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

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rego", delete=False) as f:
        f.write(policy.rego_rules)
        temp_path = f.name

    try:
        input_data = test_input.input
        input_json = json.dumps(input_data)

        result = subprocess.run(
            ["opa", "eval", "-d", temp_path, "-I", "-f", "json", "data.forgeops.governance.decision"],
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
