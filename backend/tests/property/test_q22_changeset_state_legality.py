# SPDX-License-Identifier: Apache-2.0
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from src.approvals.schemas import ApprovalStatus
from src.approvals.service import ApprovalService


@settings(max_examples=100)
@given(actions=st.lists(st.sampled_from(["approve", "reject", "rollback"]), min_size=1, max_size=10))
def test_q22_changeset_state_legality(actions: list[str]):
    """
    Property Q-22: ChangeSet state machine legality.
    Invalid transitions must raise ValueError and maintain legal state machine bounds.
    """
    svc = ApprovalService()
    cs = svc.create_changeset("p1", "Test summary", "--- diff\n+++ diff")
    assert cs.status == ApprovalStatus.PENDING

    for action in actions:
        if cs.status == ApprovalStatus.PENDING:
            if action == "approve":
                cs = svc.approve_changeset(cs.id, approver="alice")
                assert cs.status == ApprovalStatus.APPROVED
            elif action == "reject":
                cs = svc.reject_changeset(cs.id, rejector="bob")
                assert cs.status == ApprovalStatus.REJECTED
            elif action == "rollback":
                with pytest.raises(ValueError):
                    svc.rollback_changeset(cs.id)
        elif cs.status == ApprovalStatus.APPROVED:
            if action == "approve":
                with pytest.raises(ValueError):
                    svc.approve_changeset(cs.id, approver="alice")
            elif action == "reject":
                with pytest.raises(ValueError):
                    svc.reject_changeset(cs.id, rejector="bob")
            elif action == "rollback":
                cs = svc.rollback_changeset(cs.id)
                assert cs.status == ApprovalStatus.ROLLED_BACK
        elif cs.status in (ApprovalStatus.REJECTED, ApprovalStatus.ROLLED_BACK):
            if action in ("approve", "reject", "rollback"):
                with pytest.raises(ValueError):
                    if action == "approve":
                        svc.approve_changeset(cs.id, approver="alice")
                    elif action == "reject":
                        svc.reject_changeset(cs.id, rejector="bob")
                    elif action == "rollback":
                        svc.rollback_changeset(cs.id)
