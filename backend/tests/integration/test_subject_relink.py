# SPDX-License-Identifier: FSL-1.1-ALv2
"""A returning operator whose IdP subject changed must be able to sign in (design.md 3.5, 6.2, 1.11).

WHAT THIS REPRODUCES

Signing in produced, in a browser, mid-login:

    GET /api/v1/auth/callback?code=...&state=...
    500 {"type":"https://errors.forgeops.dev/internal","title":"Internal Server Error",
         "detail":"An unexpected error occurred. Quote the trace_id when reporting this.",
         "instance":"/api/v1/auth/callback"}

and in the backend log the actual cause:

    duplicate key value violates unique constraint "uq_users_email"

`users` has TWO unique constraints -- `uq_users_idp_subject` and `uq_users_email` -- and
`upsert_user`'s `ON CONFLICT (idp_subject)` can name only one. So the ordinary subject collision was
handled and an email collision under a NEW subject was not.

That is not an exotic state. The same email arrives under a new `sub` whenever the identity
provider's own database is rebuilt, whenever a user is deleted and recreated there, and whenever the
issuer is replaced -- which design.md 1.11 explicitly contemplates by naming both Authentik and
Keycloak. In this case an Authentik rebuild reissued every account's UUID while the local `users`
rows still held the old ones. Every affected operator was locked out by a 500 on the one route that
cannot be worked around.

WHY THESE TESTS USE A REAL DATABASE

The bug is the interaction of two unique constraints with one `ON CONFLICT` clause. A double that
returns rows cannot have it: the constraint has to exist and PostgreSQL has to enforce it. The unit
test for this function asserts the id-equality property against a recorder and would pass either way.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

# Imported for the mapper side effect: `users` is created by the migrations these fixtures run, and
# the models must be registered for the metadata the fixtures build against to include them.
from src.auth import models as _auth_models  # noqa: F401
from src.auth.models import UserRole
from src.auth.sessions import SessionService

pytestmark = [pytest.mark.mandatory]

EMAIL = "returning-operator@forgeops.invalid"


def _service() -> SessionService:
    return SessionService(pepper="test-only-not-a-real-value", refresh_ttl_seconds=3600)


class TestASubjectChangeDoesNotLockTheOperatorOut:
    @pytest.mark.asyncio
    async def test_the_same_email_under_a_new_subject_reuses_the_row(self, sessions) -> None:
        """The reported failure, asserted end to end through the real constraint.

        The id is asserted to be UNCHANGED, which is the point of re-linking rather than inserting:
        `audit_events.actor_user_id`, `change_sets`, `approvals.approver_id` and
        `generation_runs.requested_by` all reference it. A new row would leave every earlier action
        attributed to an account the operator can no longer sign in as.
        """
        first_subject = str(uuid.uuid4())
        second_subject = str(uuid.uuid4())
        service = _service()

        async with sessions() as session:
            before = await service.upsert_user(
                session,
                idp_subject=first_subject,
                email=EMAIL,
                name="Returning Operator",
                role=UserRole.ADMIN,
            )
            await session.commit()

        async with sessions() as session:
            # The IdP reissued this person's subject. Before the fix this raised
            # UniqueViolationError on uq_users_email and the callback answered 500.
            after = await service.upsert_user(
                session,
                idp_subject=second_subject,
                email=EMAIL,
                name="Returning Operator",
                role=UserRole.DEVELOPER,
            )
            await session.commit()

        assert after.id == before.id, "the row must be re-linked, not duplicated: its id is referenced"
        assert after.email == EMAIL

        async with sessions() as session:
            rows = (
                await session.execute(
                    text("SELECT idp_subject, role FROM users WHERE email = :email"),
                    {"email": EMAIL},
                )
            ).all()

        assert len(rows) == 1, f"exactly one row for one human, found {len(rows)}"
        assert str(rows[0][0]) == second_subject, "the row now carries the subject the IdP issued"
        # Role is rewritten on every login, because the IdP is authoritative for group membership.
        assert str(rows[0][1]) == UserRole.DEVELOPER.value

    @pytest.mark.asyncio
    async def test_an_email_change_still_follows_the_subject(self, sessions) -> None:
        """The case the original `ON CONFLICT (idp_subject)` was chosen for, still working.

        Email is mutable at the IdP and `sub` is not, so a changed email must update the existing row
        rather than create a second account. The re-link must not have inverted this.
        """
        subject = str(uuid.uuid4())
        old_email = f"before-{uuid.uuid4().hex[:8]}@forgeops.invalid"
        new_email = f"after-{uuid.uuid4().hex[:8]}@forgeops.invalid"
        service = _service()

        async with sessions() as session:
            before = await service.upsert_user(
                session, idp_subject=subject, email=old_email, name="Renamed", role=UserRole.VIEWER
            )
            await session.commit()

        async with sessions() as session:
            after = await service.upsert_user(
                session, idp_subject=subject, email=new_email, name="Renamed", role=UserRole.VIEWER
            )
            await session.commit()

        assert after.id == before.id
        assert after.email == new_email

        async with sessions() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM users WHERE idp_subject = :s"), {"s": subject}
                )
            ).scalar()
        assert count == 1, "one subject must never own two rows"

    @pytest.mark.asyncio
    async def test_two_accounts_sharing_an_email_are_not_silently_merged(self, sessions) -> None:
        """The case deliberately left unresolved, pinned so it cannot become a silent merge.

        If a row already holds the incoming subject, re-linking the email row would give one subject
        two rows and violate the other constraint. That is a genuine conflict between two identities
        and no rule here can settle it, so the `NOT EXISTS` guard declines to act: the account keyed
        by subject is updated and the other row is left alone.
        """
        shared = f"shared-{uuid.uuid4().hex[:8]}@forgeops.invalid"
        other = f"other-{uuid.uuid4().hex[:8]}@forgeops.invalid"
        subject_a = str(uuid.uuid4())
        subject_b = str(uuid.uuid4())
        service = _service()

        async with sessions() as session:
            await service.upsert_user(
                session, idp_subject=subject_a, email=shared, name="A", role=UserRole.VIEWER
            )
            await service.upsert_user(
                session, idp_subject=subject_b, email=other, name="B", role=UserRole.VIEWER
            )
            await session.commit()

        # B now presents A's email. There is nothing safe to do, so the constraint must speak.
        with pytest.raises(Exception, match="uq_users_email"):
            async with sessions() as session:
                await service.upsert_user(
                    session, idp_subject=subject_b, email=shared, name="B", role=UserRole.VIEWER
                )
                await session.commit()
