# SPDX-License-Identifier: FSL-1.1-ALv2
"""The audit writer's pure surface (design.md §6.3, §11.9, Appendix A.8; tasks.md leaf 7.6).

Everything here runs without a database, because everything here is a decision the writer makes
before it touches one: which fields are hashed, how a timestamp is rendered, what a draft must
carry, and how the advisory-lock key is derived. The chain itself is asserted against a real
PostgreSQL in `tests/integration/test_audit_writer.py` — a hash chain verified only in memory
would prove the arithmetic and nothing about the append.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from src.audit.models import AuditEvent
from src.audit.writer import (
    ACTOR_KINDS,
    GENESIS_PREV_HASH,
    OUTCOMES,
    SEMANTIC_FIELDS,
    AuditDraft,
    AuditWriter,
    ChainVerification,
    Divergence,
    InvalidAuditDraftError,
    _lock_key,
    _render_timestamp,
    _semantic_payload,
)
from src.core.canonical import canonical_bytes
from src.governance.primitives import is_mutation_primitive

pytestmark = pytest.mark.mandatory


def _draft(**overrides: object) -> AuditDraft:
    values: dict[str, object] = {
        "action": "change_set_auto_approved",
        "resource_kind": "change_set",
        "reason": "blast radius below the threshold",
        "outcome": "allowed",
    }
    values.update(overrides)
    return AuditDraft(**values)  # type: ignore[arg-type]


class TestTheHashedFieldSetIsTheContract:
    def test_seq_and_hash_are_excluded(self) -> None:
        """Appendix A.8 excludes both: `seq` is assigned after the hash is computed, and a value
        cannot cover itself."""
        assert "seq" not in SEMANTIC_FIELDS
        assert "hash" not in SEMANTIC_FIELDS

    def test_prev_hash_is_excluded_because_it_enters_through_the_concatenation(self) -> None:
        """Including it as well would hash it twice, and Q-05's control drops the concatenated
        term — so the two would no longer be the same clause."""
        assert "prev_hash" not in SEMANTIC_FIELDS

    def test_every_other_column_of_the_row_is_hashed(self) -> None:
        """The real guard against a column being added to the table and silently left unhashed.

        Derived from the model rather than hand-listed, so a new column in `0010` fails here
        instead of quietly falling outside the chain.
        """
        columns = {column.name for column in AuditEvent.__table__.columns}
        unhashed = columns - set(SEMANTIC_FIELDS) - {"seq", "hash", "prev_hash"}
        assert not unhashed, (
            f"columns present in audit_events but not covered by the hash chain: {sorted(unhashed)}. "
            f"An unhashed column can be edited without breaking the chain, which is the whole "
            f"property Q-05 asserts."
        )

    def test_the_payload_is_the_same_projection_for_a_draft_and_a_row(self) -> None:
        """One projection, both directions. Two would let a chain verify against bytes the writer
        never produced, and the failure would look exactly like tampering."""
        created = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)
        event_id = uuid.uuid4()
        draft = _draft(project_id=uuid.uuid4(), tenant_id=uuid.uuid4(), event_id=event_id)
        row = AuditEvent(
            id=event_id,
            tenant_id=draft.tenant_id,
            project_id=draft.project_id,
            actor_user_id=None,
            actor_device_id=None,
            actor_kind=draft.actor_kind,
            action=draft.action,
            resource_kind=draft.resource_kind,
            resource_id=None,
            reason=draft.reason,
            before_state=None,
            after_state=None,
            outcome=draft.outcome,
            trace_id=None,
            prev_hash=GENESIS_PREV_HASH,
            hash=b"\x00" * 32,
            created_at=created,
        )
        assert _semantic_payload(draft, created_at=created, event_id=event_id) == _semantic_payload(
            row, created_at=created, event_id=event_id
        )

    def test_the_payload_canonicalises(self) -> None:
        payload = _semantic_payload(_draft(), created_at=datetime.now(UTC), event_id=uuid.uuid4())
        assert canonical_bytes(payload)


class TestTheGenesisValue:
    def test_it_is_thirty_two_zero_bytes(self) -> None:
        assert GENESIS_PREV_HASH == bytes(32)
        assert len(GENESIS_PREV_HASH) == 32

    def test_it_is_a_value_not_a_null(self) -> None:
        """`prev_hash` is NOT NULL in `0007`, so the first row is hashed by the same expression as
        every other row. A nullable column would mean a branch that runs once."""
        column = AuditEvent.__table__.columns["prev_hash"]
        assert column.nullable is False


class TestTheTimestampRendering:
    def test_microseconds_are_always_present(self) -> None:
        """`isoformat()` omits them when they are zero, so one row in a million would hash
        differently from its own re-reading."""
        rendered = _render_timestamp(datetime(2026, 1, 1, 0, 0, 0, 0, tzinfo=UTC))
        assert rendered == "2026-01-01T00:00:00.000000+00:00"

    def test_a_naive_datetime_is_read_as_utc(self) -> None:
        assert _render_timestamp(datetime(2026, 1, 1, 0, 0, 0)) == "2026-01-01T00:00:00.000000+00:00"

    def test_an_offset_datetime_is_normalised_to_utc(self) -> None:
        from datetime import timedelta, timezone

        plus_two = timezone(timedelta(hours=2))
        assert _render_timestamp(datetime(2026, 1, 1, 2, 0, 0, tzinfo=plus_two)) == ("2026-01-01T00:00:00.000000+00:00")


class TestTheAdvisoryLockKey:
    def test_it_is_a_signed_int32(self) -> None:
        for value in ("forgeops-audit", "None", str(uuid.uuid4())):
            key = _lock_key(value)
            assert -(2**31) <= key < 2**31

    def test_it_is_stable_across_processes(self) -> None:
        """Python's `hash()` is randomised by PYTHONHASHSEED, so two workers would take different
        locks and the chain would fork under exactly the concurrency the lock exists to prevent."""
        expected = int.from_bytes(hashlib.sha256(b"forgeops-audit").digest()[:4], "big", signed=True)
        assert _lock_key("forgeops-audit") == expected

    def test_different_tenants_take_different_keys(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        assert _lock_key(str(a)) != _lock_key(str(b))

    def test_an_empty_lock_namespace_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            AuditWriter(advisory_lock_key="")


class TestTheDraftRefusesWhatCannotBeActedOn:
    def test_append_is_a_mutation_primitive(self) -> None:
        """§2.2.1: it changes state outside this process, so `check-chokepoint.sh` must find it."""
        assert is_mutation_primitive(AuditWriter.append)

    @pytest.mark.parametrize("reason", ["", "   ", "\n"])
    def test_an_empty_reason_is_refused(self, reason: str) -> None:
        """NFR-14's "why". §11.9: a required reason is what stops the log becoming a list of
        verbs."""
        with pytest.raises(InvalidAuditDraftError, match="reason is required"):
            _draft(reason=reason).validate()

    @pytest.mark.parametrize("field", ["action", "resource_kind"])
    def test_an_empty_what_is_refused(self, field: str) -> None:
        with pytest.raises(InvalidAuditDraftError, match=f"{field} is required"):
            _draft(**{field: ""}).validate()

    def test_an_unknown_actor_kind_is_refused(self) -> None:
        with pytest.raises(InvalidAuditDraftError, match="actor_kind must be one of"):
            _draft(actor_kind="device").validate()

    def test_an_unknown_outcome_is_refused(self) -> None:
        with pytest.raises(InvalidAuditDraftError, match="outcome must be one of"):
            _draft(outcome="ok").validate()

    def test_a_user_actor_must_name_the_user(self) -> None:
        with pytest.raises(InvalidAuditDraftError, match="requires actor_user_id"):
            _draft(actor_kind="user").validate()

    def test_an_agent_actor_must_name_the_device(self) -> None:
        """§11.9: agent-side operations are covered by records the hub writes with
        `actor_kind='agent'` and `actor_device_id` set."""
        with pytest.raises(InvalidAuditDraftError, match="requires actor_device_id"):
            _draft(actor_kind="agent").validate()

    def test_a_system_actor_must_name_neither(self) -> None:
        with pytest.raises(InvalidAuditDraftError, match="neither actor id"):
            _draft(actor_kind="system", actor_user_id=uuid.uuid4()).validate()

    def test_a_valid_draft_of_each_actor_kind_passes(self) -> None:
        _draft(actor_kind="user", actor_user_id=uuid.uuid4()).validate()
        _draft(actor_kind="agent", actor_device_id=uuid.uuid4()).validate()
        _draft(actor_kind="system").validate()

    @pytest.mark.parametrize("outcome", OUTCOMES)
    def test_every_declared_outcome_is_accepted(self, outcome: str) -> None:
        _draft(outcome=outcome).validate()

    @pytest.mark.parametrize("kind", ACTOR_KINDS)
    def test_every_declared_actor_kind_is_accepted(self, kind: str) -> None:
        ids: dict[str, object] = {}
        if kind == "user":
            ids["actor_user_id"] = uuid.uuid4()
        if kind == "agent":
            ids["actor_device_id"] = uuid.uuid4()
        _draft(actor_kind=kind, **ids).validate()

    def test_a_float_in_before_state_is_refused_naming_the_field(self) -> None:
        """The pair reaches the hash, so it has to be canonicalisable — and the error should name
        the draft, not the digest."""
        with pytest.raises(InvalidAuditDraftError, match="before_state is not canonicalisable"):
            _draft(before_state={"score": 0.5}).validate()

    def test_a_non_object_state_is_refused(self) -> None:
        with pytest.raises(InvalidAuditDraftError, match="must be a JSON object"):
            _draft(after_state=[1, 2, 3]).validate()

    def test_two_records_from_one_draft_cannot_share_an_id(self) -> None:
        """`event_id` defaults per instance, so reusing a draft object is the only way to collide —
        and that is a caller reusing one deliberately, which the unique constraint then refuses."""
        assert _draft().event_id != _draft().event_id


class TestTheVerificationResult:
    def test_ok_is_derived_not_stored(self) -> None:
        assert ChainVerification(tenant_id=None, from_seq=0, rows_checked=3).ok
        divergent = ChainVerification(
            tenant_id=None,
            from_seq=0,
            rows_checked=1,
            divergence=Divergence(seq=2, expected_hash=b"a", stored_hash=b"b", kind="hash", detail="x"),
        )
        assert not divergent.ok
