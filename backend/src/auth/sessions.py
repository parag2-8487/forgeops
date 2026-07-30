# SPDX-License-Identifier: FSL-1.1-ALv2
"""User upsert and the session lifecycle (design.md §3.5, §6.2, §11.2).

Where the refresh token lives, and why
--------------------------------------
`sessions.refresh_token_hmac` is an HMAC — never the token, never a reversible
encryption of it (§6.2). That single column settles a question §3.5 leaves open, and
the answer is worth stating because it is not the only conceivable one.

The refresh token is the **IdP's**: `/refresh` calls Authentik's token endpoint with
`grant_type=refresh_token`, because access tokens are Authentik-signed and this backend
cannot mint one. So the plaintext has to exist somewhere at refresh time. The schema
forbids the server holding it — there is no column for a sealed copy, and adding one
would make the database a second store of live credentials, which is exactly what the
HMAC column exists to prevent. Therefore the **browser** holds it, in an httpOnly
`SameSite=Lax` cookie, and the server holds only `HMAC(pepper, token)` — enough to find
the session, prove the presented token is the one issued, and revoke it, and useless to
anyone who steals the database.

The consequences are deliberate and bounded: a stolen database yields no usable
credential; a stolen cookie is usable, which is why it is httpOnly (no script access),
`SameSite=Lax` (no cross-site submission), `Secure` outside local development, and
rotated on every refresh so a captured token has one use before it stops working.

Rotation is a revoke-then-insert, not an update
-----------------------------------------------
`rotate` marks the old row `revoked_at` and inserts a new row. An in-place update would
erase the evidence that a rotation happened, and the audit question that matters after
a suspected theft — "how many times was this session rotated, and from where" — is
answerable only if the old rows survive. It also makes replay detectable: a second
presentation of an already-rotated token finds a row whose `revoked_at` is set, which is
distinguishable from a token that was never issued.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UserRole

#: HMAC-SHA256 produces 32 bytes, which is exactly `LargeBinary(32)` in §6.2. Stated as
#: a constant so a future change of digest cannot silently truncate.
HMAC_DIGEST_SIZE = 32


def refresh_token_hmac(pepper: str, token: str) -> bytes:
    """`HMAC-SHA256(pepper, token)` — the only form of a refresh token we store.

    A bare SHA-256 was rejected: refresh tokens are high-entropy, so a plain hash is not
    guessable, but a keyed MAC also means a database dump alone cannot be used to
    *recognise* a token an attacker already holds from another source. The pepper is
    `ENVELOPE_PEPPER`, which §13.1 defines as the "HMAC pepper for code/token storage" —
    one pepper for one purpose, not a second knob to configure and forget.
    """
    if not pepper:
        # An empty pepper would silently degrade the MAC to an unkeyed hash. Loud is
        # correct: this is a misconfiguration, and it is one that leaves no trace at
        # runtime if it is tolerated.
        raise ValueError(
            "ENVELOPE_PEPPER is empty; refresh-token HMACs would be unkeyed. Set it "
            "in the environment (design §13.1)."
        )
    return hmac.new(pepper.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).digest()


@dataclass(frozen=True, slots=True)
class ResolvedUser:
    """The user row the callback upserted, as the caller needs to see it."""

    id: uuid.UUID
    email: str
    name: str
    role: UserRole
    tenant_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ActiveSession:
    """A live session row located by its refresh-token HMAC."""

    id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime
    idp_session_id: str | None


class SessionService:
    """User upsert plus session create / locate / rotate / revoke.

    Takes an `AsyncSession` per call rather than holding one. A service that held a
    session would decide the transaction boundary, and the boundary belongs to the
    request (`core.db.get_session` commits on success and rolls back on any exception).
    """

    def __init__(self, *, pepper: str, refresh_ttl_seconds: int) -> None:
        self._pepper = pepper
        self._refresh_ttl = refresh_ttl_seconds

    async def upsert_user(
        self,
        session: AsyncSession,
        *,
        idp_subject: str,
        email: str,
        name: str,
        role: UserRole,
    ) -> ResolvedUser:
        """Insert or update the user identified by the IdP subject.

        Conflict target is `idp_subject`, not `email`. Email is mutable at the IdP and
        `sub` is not (§6.2), so keying on email would create a second account the first
        time someone changes theirs — and the audit trail would then split across two
        user ids for one human.

        `role` is written on every login. The IdP is authoritative for group
        membership, so a group removed there must take effect at the next login rather
        than persisting until someone notices.
        """
        result = await session.execute(
            text(
                """
                INSERT INTO users (id, email, name, role, idp_subject, is_active)
                VALUES (:id, :email, :name, :role, :idp_subject, true)
                ON CONFLICT (idp_subject) DO UPDATE
                    SET email = EXCLUDED.email,
                        name = EXCLUDED.name,
                        role = EXCLUDED.role,
                        is_active = true,
                        updated_at = now()
                RETURNING id, email, name, role, tenant_id
                """
            ),
            {
                "id": uuid.uuid4(),
                "email": email,
                "name": name,
                "role": role.value,
                "idp_subject": idp_subject,
            },
        )
        row = result.one()
        return ResolvedUser(
            id=row[0],
            email=str(row[1]),
            name=str(row[2]),
            role=UserRole(str(row[3])),
            tenant_id=row[4],
        )

    async def create_session(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        refresh_token: str,
        idp_session_id: str | None,
    ) -> ActiveSession:
        """Persist a new session holding only the HMAC of the refresh token."""
        session_id = uuid.uuid4()
        expires_at = datetime.now(UTC) + timedelta(seconds=self._refresh_ttl)
        await session.execute(
            text(
                """
                INSERT INTO sessions (id, user_id, refresh_token_hmac, idp_session_id, expires_at)
                VALUES (:id, :user_id, :hmac, :idp_session_id, :expires_at)
                """
            ),
            {
                "id": session_id,
                "user_id": user_id,
                "hmac": refresh_token_hmac(self._pepper, refresh_token),
                "idp_session_id": idp_session_id,
                "expires_at": expires_at,
            },
        )
        return ActiveSession(
            id=session_id,
            user_id=user_id,
            expires_at=expires_at,
            idp_session_id=idp_session_id,
        )

    async def find_active(self, session: AsyncSession, *, refresh_token: str) -> ActiveSession | None:
        """Locate a live session by the presented token, or None.

        "Live" means not revoked and not expired, both evaluated in the database with
        `now()`. Comparing timestamps in Python would compare the application's clock
        with a value the database wrote, and the two are not the same clock.
        """
        result = await session.execute(
            text(
                """
                SELECT id, user_id, expires_at, idp_session_id
                FROM sessions
                WHERE refresh_token_hmac = :hmac
                  AND revoked_at IS NULL
                  AND expires_at > now()
                """
            ),
            {"hmac": refresh_token_hmac(self._pepper, refresh_token)},
        )
        row = result.first()
        if row is None:
            return None
        return ActiveSession(id=row[0], user_id=row[1], expires_at=row[2], idp_session_id=row[3])

    async def rotate(
        self,
        session: AsyncSession,
        *,
        current: ActiveSession,
        new_refresh_token: str,
    ) -> ActiveSession:
        """Revoke the presented session and issue a successor.

        When the IdP returns the *same* refresh token (some do not rotate), the caller
        passes it through and this still produces a new row: the old row is revoked and
        a new one carries the same HMAC. That keeps one live row per token and keeps the
        rotation count honest, which an update-in-place would not.
        """
        await self.revoke(session, session_id=current.id)
        return await self.create_session(
            session,
            user_id=current.user_id,
            refresh_token=new_refresh_token,
            idp_session_id=current.idp_session_id,
        )

    async def revoke(self, session: AsyncSession, *, session_id: uuid.UUID) -> None:
        """Mark a session revoked. Idempotent: `revoked_at` is set once and kept.

        `revoked_at IS NULL` in the predicate is what makes it idempotent *and*
        preserves the first revocation's timestamp, which is the one that matters when
        reconstructing what happened.
        """
        await session.execute(
            text("UPDATE sessions SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"),
            {"id": session_id},
        )

    async def revoke_by_token(self, session: AsyncSession, *, refresh_token: str) -> bool:
        """Revoke whatever live session the presented token identifies.

        Returns whether a session was found. Logout uses this and ignores the result:
        §4.4 requires logout to succeed even when the credential has already expired, so
        "no live session" is a successful logout, not an error.
        """
        found = await self.find_active(session, refresh_token=refresh_token)
        if found is None:
            return False
        await self.revoke(session, session_id=found.id)
        return True
