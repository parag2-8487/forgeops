# SPDX-License-Identifier: FSL-1.1-ALv2
"""The upserted user's id must equal the id the principal carries (design.md §3.5, §6.2).

WHY THIS FILE EXISTS
`AppTokenVerifier` derives `Principal.user_id` from the `forgeops_user_id` claim or, failing that,
from the subject -- its docstring says "Authentik's `sub` is a UUID, so the common case needs no extra
claim". `SessionService.upsert_user` inserted `uuid.uuid4()` instead, so the local row's id and the
principal's `user_id` were two unrelated values.

THE CONSEQUENCE WAS NOT COSMETIC. Every foreign key to `users.id` written on a request path failed
against a real identity provider. `generation_runs.requested_by` raised `ForeignKeyViolationError` on
the very first insert, so the generation endpoint answered `200 text/event-stream` and then emitted
**zero events** -- the exception was raised inside an async generator after the response had already
begun, so it could not become a Problem response. The UI reported "the stream ended without a terminal
event", which is exactly what it saw and says nothing about the cause.
`approvals.approver_id` would have failed identically on the first decision.

So the property under test is the EQUALITY, asserted directly, rather than either half in isolation.
Nothing else would have caught it: the verifier's tests were right about the principal, the session
tests were right about the row, and no test compared the two.
"""

from __future__ import annotations

import uuid

import pytest
from src.auth.models import UserRole
from src.auth.sessions import SessionService
from src.auth.verifier import AppTokenVerifier

pytestmark = [pytest.mark.mandatory]

#: A UUID subject, which is what Authentik issues when `sub_mode` is `user_uuid`.
UUID_SUBJECT = "2fd643e7-2eb4-4463-8ab2-c2500b385a48"


class _CapturingSession:
    """Records the parameters the upsert binds, and returns them as the inserted row."""

    def __init__(self) -> None:
        self.params: dict[str, object] = {}

    async def execute(self, _statement: object, params: dict[str, object]) -> object:
        self.params = params

        class _Result:
            def one(self_inner) -> tuple[object, ...]:  # noqa: N805
                return (params["id"], params["email"], params["name"], params["role"], None)

        return _Result()


class TestTheLocalIdIsTheSubject:
    async def test_a_uuid_subject_becomes_the_row_id(self) -> None:
        service = SessionService(pepper="test-only-not-a-real-value", refresh_ttl_seconds=3600)
        session = _CapturingSession()

        resolved = await service.upsert_user(
            session,  # type: ignore[arg-type]
            idp_subject=UUID_SUBJECT,
            email="someone@example.invalid",
            name="Someone",
            role=UserRole.ADMIN,
        )

        assert resolved.id == uuid.UUID(UUID_SUBJECT)
        assert session.params["id"] == uuid.UUID(UUID_SUBJECT)

    async def test_it_equals_what_the_verifier_derives_from_the_same_subject(self) -> None:
        """The two halves compared against each other, which is what nothing did before.

        A test that only checked the row, or only checked the principal, passes while the pair is
        inconsistent -- and the inconsistency is the defect.
        """
        service = SessionService(pepper="test-only-not-a-real-value", refresh_ttl_seconds=3600)
        session = _CapturingSession()
        resolved = await service.upsert_user(
            session,  # type: ignore[arg-type]
            idp_subject=UUID_SUBJECT,
            email="someone@example.invalid",
            name="Someone",
            role=UserRole.DEVELOPER,
        )

        from_token = AppTokenVerifier._uuid_from_subject(UUID_SUBJECT)
        assert from_token is not None
        assert resolved.id == from_token, (
            "the row id and the principal's user_id must be the same value, or every foreign key to "
            "users.id fails on a request path"
        )

    async def test_a_non_uuid_subject_still_inserts_with_a_generated_id(self) -> None:
        """Kept insertable, because such a subject has no usable user id anyway.

        The verifier rejects a non-UUID subject that carries no `forgeops_user_id` claim, so it never
        reaches a foreign key — but refusing to store the row would turn a rejected login into a
        500 during the upsert rather than a clean refusal afterwards.
        """
        service = SessionService(pepper="test-only-not-a-real-value", refresh_ttl_seconds=3600)
        session = _CapturingSession()

        resolved = await service.upsert_user(
            session,  # type: ignore[arg-type]
            idp_subject="not-a-uuid|12345",
            email="someone@example.invalid",
            name="Someone",
            role=UserRole.VIEWER,
        )

        assert isinstance(resolved.id, uuid.UUID)
        assert AppTokenVerifier._uuid_from_subject("not-a-uuid|12345") is None
