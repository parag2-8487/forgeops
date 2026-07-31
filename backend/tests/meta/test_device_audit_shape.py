# SPDX-License-Identifier: FSL-1.1-ALv2
"""D-70 — a device-lifecycle record cannot be shaped like a chokepoint transit.

Why this is a meta test rather than a unit test
----------------------------------------------
Every assertion here is about the *relationship between two vocabularies*, not about behaviour.
D-70's whole claim is that `DeviceAuditEvent` and `AuditDraft` cannot produce the same row: the
device type's actions are disjoint from `GovernanceAction`, its `resource_kind` is a constant, and
its evidence field is a closed key set. If any of those three stops being true, Q-04 —
"exactly one `audit_events` row per chokepoint transit" — quietly stops being able to fail,
because a second writer could then emit transit-shaped rows Q-04 never looks at.

That is chapter 5's defect, and the reason it belongs here is that a **structural** property is
best asserted against the structures rather than through an exercise of them. There is no
generated input that makes "these two tuples overlap" more true.
"""

from __future__ import annotations

import dataclasses
import inspect
import uuid

import pytest
from src.audit.device_log import (
    DEVICE_AUDIT_ACTIONS,
    DEVICE_AUDIT_DETAIL_KEYS,
    DEVICE_AUDIT_OUTCOMES,
    DEVICE_RESOURCE_KIND,
    DeviceAuditEvent,
    DeviceAuditRecorder,
    InvalidDeviceAuditEventError,
)
from src.audit.writer import OUTCOMES, AuditDraft
from src.governance.chokepoint import GovernanceAction
from src.governance.device_audit import GovernanceDeviceAuditRecorder

pytestmark = pytest.mark.mandatory


class TestTheTwoVocabulariesAreDisjoint:
    def test_no_device_action_is_a_governance_action(self) -> None:
        """The clause D-70 rests on. An overlap here is a device record that reads as a transit."""
        governance = {action.value for action in GovernanceAction}
        overlap = sorted(set(DEVICE_AUDIT_ACTIONS) & governance)
        assert not overlap, (
            f"{overlap} appears in both vocabularies. A device-lifecycle record carrying a "
            "transit's action is indistinguishable from a transit's row, and Q-04 counts rows "
            "by action (D-70)"
        )

    def test_both_vocabularies_are_non_empty(self) -> None:
        """Disjointness is trivially true of an empty set, which is how this clause would rot."""
        assert DEVICE_AUDIT_ACTIONS
        assert {action.value for action in GovernanceAction}

    def test_the_device_resource_kind_is_not_the_transit_resource_kind(self) -> None:
        assert DEVICE_RESOURCE_KIND == "agent_device"
        assert DEVICE_RESOURCE_KIND != "change_set"

    def test_the_device_outcomes_are_a_subset_of_the_writers(self) -> None:
        """A device outcome the writer would refuse would be a row that never gets written."""
        assert set(DEVICE_AUDIT_OUTCOMES) <= set(OUTCOMES)

    def test_the_chokepoint_only_verdicts_are_absent_from_the_device_set(self) -> None:
        """`blocked` and `pending` are blast-radius and approval verdicts; a pairing has neither."""
        assert "blocked" not in DEVICE_AUDIT_OUTCOMES
        assert "pending" not in DEVICE_AUDIT_OUTCOMES


class TestTheTransitShapedFieldsAreUnreachable:
    def test_the_device_event_has_no_resource_kind_field(self) -> None:
        """A field would let a caller write `resource_kind="change_set"`; a constant cannot."""
        fields = {field.name for field in dataclasses.fields(DeviceAuditEvent)}
        assert "resource_kind" not in fields

    def test_the_device_event_has_neither_before_state_nor_after_state(self) -> None:
        """The transit's evidence pair. `details` replaces it, with a closed key set."""
        fields = {field.name for field in dataclasses.fields(DeviceAuditEvent)}
        assert "before_state" not in fields
        assert "after_state" not in fields
        # And the draft it is projected onto DOES have them, so the absence above is a real
        # narrowing rather than a coincidence of two types that both lack the field.
        draft_fields = {field.name for field in dataclasses.fields(AuditDraft)}
        assert {"before_state", "after_state", "resource_kind"} <= draft_fields

    @pytest.mark.parametrize("action", sorted({action.value for action in GovernanceAction}))
    def test_no_governance_action_can_be_constructed_as_a_device_event(self, action: str) -> None:
        """Parametrised over every transit action, so a new one is covered the day it is added."""
        with pytest.raises(InvalidDeviceAuditEventError, match="action must be one of"):
            DeviceAuditEvent(action=action, reason="attempted", outcome="allowed")

    @pytest.mark.parametrize("key", ["code", "pairing_code", "token", "secret", "before_state"])
    def test_details_refuses_an_unregistered_key(self, key: str) -> None:
        """Q-17's "the code appears in no audit row", made structural.

        `code` and `pairing_code` are the two names a well-meaning author would reach for, and
        neither exists. There is no catch-all either, so there is no key to smuggle it under.
        """
        with pytest.raises(InvalidDeviceAuditEventError, match="unregistered key"):
            DeviceAuditEvent(
                action="pairing_failed",
                reason="refused",
                outcome="denied",
                details={key: "value"},
            )

    def test_no_permitted_detail_key_names_a_credential(self) -> None:
        """The key set is reviewed here rather than at each call site."""
        forbidden = ("code", "token", "secret", "password", "key", "pepper")
        for key in sorted(DEVICE_AUDIT_DETAIL_KEYS):
            lowered = key.lower()
            assert not any(word in lowered.split("_") for word in forbidden), key

    def test_a_registered_key_is_accepted(self) -> None:
        """Without this, the clause above passes for a key set that rejects everything."""
        event = DeviceAuditEvent(
            action="device_paired",
            reason="pairing code exchanged",
            outcome="allowed",
            details={"device_id": "d", "csr_spki_sha256": "f"},
        )
        assert event.details == {"device_id": "d", "csr_spki_sha256": "f"}


class TestTheRecorderContract:
    def test_the_governance_recorder_satisfies_the_protocol(self) -> None:
        """§0.4.3: the implementation is bound against the declared shape, not assumed to match."""
        from src.audit.writer import AuditWriter

        recorder: DeviceAuditRecorder = GovernanceDeviceAuditRecorder(writer=AuditWriter())
        assert isinstance(recorder, DeviceAuditRecorder)
        assert inspect.signature(GovernanceDeviceAuditRecorder.record) == inspect.signature(DeviceAuditRecorder.record)

    def test_the_recorder_lives_in_governance(self) -> None:
        """The positional half of D-70. If this module moved, the write would stop being authorised.

        Asserted rather than left to `check-chokepoint.sh`: the checker would catch the move, but
        this states *why* the module is where it is, in the place a reader looks first.
        """
        assert GovernanceDeviceAuditRecorder.__module__ == "src.governance.device_audit"

    def test_the_actor_kind_is_derived_and_consistent(self) -> None:
        """`AuditDraft.validate` refuses both inconsistent combinations; deriving avoids them."""
        assert DeviceAuditEvent(action="pairing_failed", reason="r", outcome="denied").actor_kind == "system"
        with_user = DeviceAuditEvent(action="device_paired", reason="r", outcome="allowed", actor_user_id=uuid.uuid4())
        assert with_user.actor_kind == "user"

    def test_an_empty_reason_is_refused(self) -> None:
        with pytest.raises(InvalidDeviceAuditEventError, match="reason is required"):
            DeviceAuditEvent(action="device_revoked", reason="   ", outcome="allowed")
