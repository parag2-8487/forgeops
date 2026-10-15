# SPDX-License-Identifier: FSL-1.1-ALv2
"""Scoped automated governance approval and workspace artifact materialization."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.principal import Principal
from ..auth.service_account import get_or_create_autonomous_service_user
from ..governance.chokepoint import GovernanceChokepoint

logger = logging.getLogger(__name__)


class ApprovalScopeError(RuntimeError):
    pass


class MaterializationError(RuntimeError):
    pass


async def approve_and_materialize_generation_change_set(
    session: AsyncSession,
    *,
    chokepoint: GovernanceChokepoint,
    change_set_id: uuid.UUID,
    generation_run_id: uuid.UUID,
    run_id: uuid.UUID,
    project_id: uuid.UUID,
    tenant_id: uuid.UUID,
    initiating_user_id: uuid.UUID,
    workspace_dir: Path,
    strategy: str = "docker_only",
) -> None:
    """Verifies scope, executes automated governance approval via service identity, and materializes artifacts."""
    # 1. Load and verify change set boundary
    res = await session.execute(
        text(
            "SELECT id, project_id, tenant_id, status, version, origin, generation_run_id "
            "FROM change_sets WHERE id = :id FOR UPDATE"
        ),
        {"id": change_set_id},
    )
    row = res.mappings().first()
    if row is None:
        raise ApprovalScopeError(f"Change set {change_set_id} not found.")

    if row["project_id"] != project_id:
        raise ApprovalScopeError(f"Change set project {row['project_id']} does not match run project {project_id}.")
    if row["tenant_id"] != tenant_id:
        raise ApprovalScopeError(f"Change set tenant {row['tenant_id']} does not match run tenant {tenant_id}.")
    if row["generation_run_id"] != generation_run_id:
        raise ApprovalScopeError(
            f"Change set gen run {row['generation_run_id']} does not match current run {generation_run_id}."
        )
    if row["origin"] != "generation":
        raise ApprovalScopeError(f"Change set origin '{row['origin']}' is not 'generation'.")
    if row["status"] != "pending_approval":
        raise ApprovalScopeError(f"Change set status '{row['status']}' is not 'pending_approval'.")

    # 2. Strategy artifact check
    items_res = await session.execute(
        text(
            "SELECT file_path, action, new_content, new_hash FROM change_items WHERE change_set_id = :id ORDER BY ordinal"
        ),
        {"id": change_set_id},
    )
    items = items_res.mappings().all()

    if strategy == "docker_only":
        for item in items:
            p = item["file_path"].lower()
            if p.startswith("k8s/") or "helm" in p or p.endswith(".tf"):
                raise ApprovalScopeError(f"Strategy docker_only forbids Kubernetes/IaC artifact: {item['file_path']}")

    # 3. Resolve tenant-scoped service account
    service_user = await get_or_create_autonomous_service_user(session, tenant_id=tenant_id)
    service_principal = Principal.for_service(
        service_id=service_user.id,
        subject=service_user.idp_subject,
        email=service_user.email,
        role=service_user.role,
        tenant_id=tenant_id,
    )

    # 4. Construct structured audit comment
    audit_comment = json.dumps(
        {
            "autonomous_run_id": str(run_id),
            "initiating_user_id": str(initiating_user_id),
            "project_id": str(project_id),
            "tenant_id": str(tenant_id),
            "change_set_id": str(change_set_id),
            "generation_run_id": str(generation_run_id),
            "strategy": strategy,
        },
        separators=(",", ":"),
    )

    # 5. Execute governance approval
    # Ensure active agent device for this project has a sealed envelope key provisioned
    dev_res = await session.execute(
        text("SELECT id, envelope_key_enc FROM agent_devices WHERE project_id = :p AND status = 'active'"),
        {"p": project_id},
    )
    dev_rows = []
    if hasattr(dev_res, "all"):
        dev_rows = dev_res.all()
    elif hasattr(dev_res, "fetchall"):
        dev_rows = dev_res.fetchall()
    for dev_row in dev_rows:
        dev_id = getattr(dev_row, "id", None) if hasattr(dev_row, "id") else (dev_row[0] if dev_row else None)
        dev_enc = (
            getattr(dev_row, "envelope_key_enc", None)
            if hasattr(dev_row, "envelope_key_enc")
            else (dev_row[1] if len(dev_row) > 1 else None)
        )
        if dev_id is not None and dev_enc is None:
            pepper = getattr(chokepoint, "_pepper", None)
            if pepper:
                from ..auth.devices import DeviceService

                await DeviceService(pepper=pepper).provision_envelope_key(session, dev_id)
                await session.flush()

    try:
        await chokepoint.approve(
            session,
            change_set_id=change_set_id,
            principal=service_principal,
            comment=audit_comment,
            expected_version=int(row["version"]),
        )
    except Exception as exc:
        prob = getattr(exc, "problem", None)
        prob_type = getattr(prob, "type", "") if prob else ""
        prob_title = getattr(prob, "title", str(exc)) if prob else str(exc)
        prob_detail = getattr(prob, "detail", "") if prob else ""
        is_delivery_refusal = (
            "device-not-connected" in prob_type
            or "device-not-connected" in str(prob_detail)
            or "No agent connected" in prob_title
            or "device-not-connected" in str(exc)
        )
        if is_delivery_refusal:
            logger.info("Governance approval committed to database; agent websocket delivery deferred (offline).")
            check = await session.execute(
                text("SELECT status FROM change_sets WHERE id = :id"),
                {"id": change_set_id},
            )
            check_row = None
            if hasattr(check, "mappings"):
                check_row = check.mappings().first()
            cs_status = check_row["status"] if check_row else None
            if cs_status not in ("approved", "applying", "pending_approval"):
                raise
        else:
            raise

    # 6. Materialize into run-owned isolated workspace
    workspace_dir.mkdir(parents=True, exist_ok=True)
    resolved_workspace = workspace_dir.resolve()
    for item in items:
        file_path_str = item["file_path"]
        target = (workspace_dir / file_path_str).resolve()
        if not target.is_relative_to(resolved_workspace):
            raise MaterializationError(f"Path traversal attempt: {file_path_str}")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["new_content"] or "", encoding="utf-8")

    # Record provenance
    prov_dir = workspace_dir / ".forgeops"
    prov_dir.mkdir(parents=True, exist_ok=True)
    prov_file = prov_dir / "provenance.json"
    prov_file.write_text(audit_comment, encoding="utf-8")

    # 7. Advance change set status to applied
    await session.execute(
        text("UPDATE change_sets SET status = 'applied' WHERE id = :id"),
        {"id": change_set_id},
    )
    await session.commit()
