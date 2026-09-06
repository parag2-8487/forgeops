# SPDX-License-Identifier: FSL-1.1-ALv2
"""The approval audit reason must not read like a defect report.

The record said

    137  change_set_approved  user  change_set/2d256d94-…  allowed
         approved by parag@forgeops.invalid: environment is absent

which is a successful, deliberate approval described in the words of the policy rule that required it.
`decision.reason` says why approval was NEEDED; after "approved by X:" it reads as X's justification.
Nothing was wrong with the change set — a generated one legitimately carries no environment, and
`approval.rego` answers `require_approval` exactly so a human looks at it.
"""

from __future__ import annotations

from src.governance.chokepoint import (
    _APPROVAL_COMMENT_BUDGET,
    _APPROVAL_REASON_LIMIT,
    _approval_audit_reason,
)


def test_the_policy_requirement_is_not_presented_as_the_approvers_reason() -> None:
    """The exact string from the user's audit trail must no longer be producible."""
    reason = _approval_audit_reason(
        actor="parag@forgeops.invalid",
        policy_reason="environment is absent",
        comment=None,
    )
    assert reason != "approved by parag@forgeops.invalid: environment is absent"
    # The causal direction is stated rather than implied by a colon.
    assert reason == ("approved by parag@forgeops.invalid; approval was required because environment is absent")


def test_both_facts_survive() -> None:
    """Neither fact is dropped to fix the sentence.

    Losing the policy reason would make the record less useful than the broken one — a reader must
    still be able to see why this change set needed a human at all.
    """
    reason = _approval_audit_reason(
        actor="ops@forgeops.invalid",
        policy_reason='environment is "prod"',
        comment=None,
    )
    assert "ops@forgeops.invalid" in reason
    assert 'environment is "prod"' in reason


def test_the_approvers_own_comment_is_recorded() -> None:
    """It was written to `approvals.comment` and omitted from the line a reader reads."""
    reason = _approval_audit_reason(
        actor="ops@forgeops.invalid",
        policy_reason="environment is absent",
        comment="checked the manifest against the staging cluster",
    )
    assert 'approver comment: "checked the manifest against the staging cluster"' in reason


def test_an_allow_with_no_stated_reason_produces_a_clean_sentence() -> None:
    """No trailing punctuation and no empty clause when the policy had nothing to add."""
    reason = _approval_audit_reason(actor="ops@forgeops.invalid", policy_reason="", comment=None)
    assert reason == "approved by ops@forgeops.invalid"


def test_a_long_comment_cannot_make_the_record_unwritable() -> None:
    """The column accepts 1024 characters; the comment is the part that yields, and says so.

    Trimming silently would make a short comment and a cut one indistinguishable, so the marker
    matters as much as the bound.
    """
    reason = _approval_audit_reason(
        actor="ops@forgeops.invalid",
        policy_reason="environment is absent",
        comment="x" * 5000,
    )
    assert len(reason) <= _APPROVAL_REASON_LIMIT
    assert "\u2026" in reason, "a truncated comment must be marked as truncated"
    # The two load-bearing facts are never the part that gets cut.
    assert "approved by ops@forgeops.invalid" in reason
    assert "approval was required because environment is absent" in reason


def test_the_comment_budget_leaves_room_for_the_facts() -> None:
    """Guards the constants against each other rather than trusting them separately."""
    assert _APPROVAL_COMMENT_BUDGET < _APPROVAL_REASON_LIMIT
