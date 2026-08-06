# SPDX-License-Identifier: Apache-2.0
import pytest
from src.approvals.service import get_approval_service
from src.approvals.schemas import ApprovalStatus


def test_approval_state_machine_flow():
    svc = get_approval_service()
    cs = svc.create_changeset("proj-1", "Deploy new API", "--- main.go\n+++ main.go")

    assert cs.status == ApprovalStatus.PENDING

    approved = svc.approve_changeset(cs.id, approver="alice")
    assert approved.status == ApprovalStatus.APPROVED
    assert approved.approved_by == "alice"

    with pytest.raises(ValueError, match="Cannot approve"):
        svc.approve_changeset(cs.id, approver="bob")


def test_approval_reject_flow():
    svc = get_approval_service()
    cs = svc.create_changeset("proj-2", "Dangerous config change", "- auth: true\n+ auth: false")

    rejected = svc.reject_changeset(cs.id, rejector="sec-team")
    assert rejected.status == ApprovalStatus.REJECTED


def test_approval_rollback_flow():
    svc = get_approval_service()
    cs = svc.create_changeset("proj-3", "Deploy feature X", "--- file\n+++ file")
    svc.approve_changeset(cs.id, approver="alice")

    rolled_back = svc.rollback_changeset(cs.id)
    assert rolled_back.status == ApprovalStatus.ROLLED_BACK

