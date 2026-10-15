# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for service principal audit classification in GovernanceChokepoint."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock
import pytest
from src.audit.models import AuditEvent
from src.audit.writer import AuditDraft
from src.auth.models import UserRole
from src.auth.principal import Principal
from src.governance.chokepoint import GovernanceAction, GovernanceChokepoint


@pytest.mark.asyncio
async def test_service_principal_audit_classification():
    service_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    principal = Principal.for_service(
        service_id=service_id,
        subject=f"service:autonomous-deploy:{tenant_id}",
        email=f"service.autonomous-deploy+{tenant_id}@forgeops.internal",
        tenant_id=tenant_id,
        role=UserRole.DEVELOPER,
    )

    # Construct GovernanceChokepoint with mock audit writer
    chokepoint = object.__new__(GovernanceChokepoint)
    mock_audit = MagicMock()

    async def mock_append(session, draft: AuditDraft):
        draft.validate()
        return AuditEvent(
            id=draft.event_id,
            tenant_id=draft.tenant_id,
            project_id=draft.project_id,
            actor_user_id=draft.actor_user_id,
            actor_device_id=draft.actor_device_id,
            actor_kind=draft.actor_kind,
            action=draft.action,
            resource_kind=draft.resource_kind,
            resource_id=draft.resource_id,
            reason=draft.reason,
            outcome=draft.outcome,
            seq=1,
        )

    mock_audit.append = AsyncMock(side_effect=mock_append)
    chokepoint._audit = mock_audit

    mock_session = AsyncMock()

    event = await chokepoint._append_audit(
        mock_session,
        principal=principal,
        admitted=None,
        action=GovernanceAction.CHANGE_SET_APPROVED,
        outcome="allowed",
        resource_kind="change_set",
        resource_id=str(uuid.uuid4()),
        reason="test service approval",
        tenant_id=tenant_id,
    )
    assert event.actor_kind == "system"
    assert event.actor_user_id is None
