# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-17 — pairing-code safety (design §11.2, §14.6, Appendix A.1, Appendix B Q-17; leaf 8.11).

Property, universally quantified over concurrent exchange attempts on one pairing code:

    at most one succeeds; an expired, burned or unknown code is indistinguishable in the response;
    attempts beyond the cap always fail; the code value appears in no log, audit row or column.

**What this adds over leaf 8.1's integration cases.** `tests/integration/test_agent_pairing.py`
drives each shape once — six concurrent exchanges, an expired code, a burned code, an unknown code,
the per-IP and global caps. Those are examples at one concurrency level and one refusal order. What
Appendix B quantifies over is the **number** of concurrent attempts and the **order** in which the
four refusal reasons are asked about, because a read-then-delete consume script is atomic *enough*
at two attempts and visibly wrong at eight, and an indistinguishability claim that only holds for
the order the author happened to write is not indistinguishability.

The helpers are imported from that file rather than re-declared: two definitions of "the production
`DeviceService`, sized so a cap can be observed" is how a property comes to quantify over a shape
the integration tests never exercise.

**Negative control** (`mutations.toml` Q-17): the consume script becomes non-atomic — a read
followed by a delete instead of one `EVAL`. The at-most-one clause then fails as soon as two
attempts interleave between the read and the delete, which generated concurrency reaches and a
fixed pair may not.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.auth.devices import (
    PAIRING_KEY_PREFIX,
    AgentMeta,
    PairingCodeInvalidError,
    PairingRateLimitedError,
)

from ..integration.test_agent_pairing import build_csr, make_project, make_service

pytestmark = pytest.mark.mandatory

#: Every example runs real transactions against real Postgres and Redis, so the budget buys breadth
#: of CONCURRENCY and of refusal ORDER rather than raw count.
_SETTINGS = settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

#: The four ways a code can be unusable. §14.6 requires the response to be identical for all of
#: them, so an attacker cannot learn whether a code ever existed.
_REASONS = ("unknown", "expired", "burned", "not-pairable")


class TestAtMostOneConcurrentExchangeSucceeds:
    @given(attempts=st.integers(min_value=2, max_value=8))
    @_SETTINGS
    @pytest.mark.asyncio
    async def test_over_generated_concurrency(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: Any,
        attempts: int,
    ) -> None:
        """Generated over the NUMBER of concurrent attempts, which is the axis that matters.

        A read-then-delete consume is atomic enough at two attempts on a fast machine and visibly
        wrong at eight. Asserting exactly one success at a fixed concurrency is a coin toss against
        that mutation; quantifying over the count is not.

        Each attempt runs on its own session so the database work really is concurrent, and the
        successes are counted rather than the failures, so "everything failed" cannot pass.
        """
        service = make_service(redis_client, per_ip_capacity=100, global_capacity=500)
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)

        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()

        async def attempt() -> str:
            async with sessions() as own:
                try:
                    await service.exchange(own, code=issued.code, csr_pem=csr, meta=meta, client_ip="203.0.113.17")
                    await own.commit()
                    return "ok"
                except (PairingCodeInvalidError, PairingRateLimitedError):
                    # COMMIT on refusal, not rollback, and the difference is load-bearing: the
                    # refusal path writes an audit row naming which branch refused, and the clause
                    # below reads it. A rollback here would discard exactly the evidence that tells
                    # an atomic consume from a race the database happened to catch.
                    await own.commit()
                    return "refused"

        outcomes = await asyncio.gather(*(attempt() for _ in range(attempts)))
        assert outcomes.count("ok") == 1, (
            f"{outcomes.count('ok')} of {attempts} concurrent exchanges succeeded; the consume must "
            "be one atomic EVAL, or two callers can both read before either deletes"
        )

        # And the database agrees: exactly one device row left the pending state, and its
        # `pairing_token_hmac` is gone — Appendix A.1's "the code cannot be reused, even in the DB".
        async with sessions() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT status, pairing_token_hmac FROM agent_devices "
                            "WHERE project_id = :project ORDER BY created_at"
                        ),
                        {"project": project_id},
                    )
                )
                .mappings()
                .all()
            )
            failures = (
                (
                    await session.execute(
                        text(
                            "SELECT reason FROM audit_events WHERE project_id = :project AND action = 'pairing_failed'"
                        ),
                        {"project": project_id},
                    )
                )
                .mappings()
                .all()
            )
        active = [row for row in rows if row["status"] == "active"]
        assert len(active) == 1, f"{len(active)} active device row(s) for one code"
        assert active[0]["pairing_token_hmac"] is None, "the consumed code is still in the row"

        # WHICH refusal the losers got, and this is the clause that pins the atomic consume
        # specifically. `exchange` has a SECOND serialisation point — the `UPDATE … WHERE status =
        # 'pending' AND pairing_token_hmac = :digest` — so at-most-one survives a non-atomic consume
        # on its own, which is defence in depth and also means the count alone cannot fail under
        # Appendix B's control (D-88). What changes is the REASON: with one atomic `EVAL` the losers
        # never see the key at all and are refused as `missing`, writing no `device-not-pairable`
        # record; with a read-then-delete pair they all pass the script and pile up on the row.
        not_pairable = [row for row in failures if "device-not-pairable" in (row["reason"] or "")]
        assert not not_pairable, (
            f"{len(not_pairable)} concurrent attempt(s) were refused by the DATABASE row rather than "
            "by the code; the consume is not the serialisation point"
        )


class TestEveryRefusalIsIndistinguishable:
    @given(order=st.permutations(_REASONS))
    @_SETTINGS
    @pytest.mark.asyncio
    async def test_over_every_order_of_the_four_reasons(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: Any,
        order: tuple[str, ...],
    ) -> None:
        """Generated over the ORDER the four reasons are asked about.

        Indistinguishability that only holds for the order the author wrote is not
        indistinguishability: a service that memoised the first refusal, or whose limiter state
        leaked across reasons, would pass one fixed sequence and fail another. So the permutation is
        drawn and every refusal in it must carry the same exception type and the same message.
        """
        service = make_service(redis_client, per_ip_capacity=100, global_capacity=500, max_attempts=2)
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)

        async with sessions() as session:
            project_id, actor = await make_project(session)
            await session.commit()

        seen: list[tuple[str, str]] = []
        for reason in order:
            code = await _unusable_code(service, sessions, project_id, actor, reason, csr, meta)
            async with sessions() as own:
                with pytest.raises(PairingCodeInvalidError) as caught:
                    await service.exchange(own, code=code, csr_pem=csr, meta=meta, client_ip="203.0.113.18")
                await own.rollback()
            seen.append((type(caught.value).__name__, str(caught.value)))

        assert len({message for _kind, message in seen}) == 1, (
            f"the four refusals differ: {seen}. §14.6 requires unknown, expired, burned and "
            "not-pairable to be one outcome, or an attacker learns whether a code ever existed"
        )

    @pytest.mark.asyncio
    async def test_the_control_shows_a_usable_code_succeeds(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: Any,
    ) -> None:
        """Without this, the clause above would pass for a service that refuses everything."""
        service = make_service(redis_client, per_ip_capacity=100, global_capacity=500)
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
        async with sessions() as own:
            result = await service.exchange(own, code=issued.code, csr_pem=csr, meta=meta, client_ip="203.0.113.19")
            await own.commit()
        assert result.device_id is not None


class TestAttemptsBeyondTheCapAlwaysFail:
    @given(extra=st.integers(min_value=1, max_value=4))
    @_SETTINGS
    @pytest.mark.asyncio
    async def test_the_code_is_burned_and_stays_burned(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: Any,
        extra: int,
    ) -> None:
        """§14.6's attempt cap, quantified over how far past it the caller goes.

        The attempt counter lives on the code's own Redis hash, so a wrong code cannot burn a right
        one — it has a different digest and therefore a different key and counter. The cap is
        therefore driven the way leaf 8.1 drives it: the hash's `attempts` field is set to the cap,
        so the next presentation crosses it.

        The clause that matters is not the first refusal after the cap but that the code stays dead.
        A counter that reset, or a cap checked before it was incremented, would let a later attempt
        through — so `extra` further attempts are made with the CORRECT code, which is the case a
        wrong-code-only test never reaches.
        """
        service = make_service(redis_client, per_ip_capacity=100, global_capacity=500, max_attempts=2)
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)

        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()

        await _drive_attempts_to_the_cap(service, redis_client, issued.code, cap=2)

        for _ in range(1 + extra):
            async with sessions() as own:
                with pytest.raises(PairingCodeInvalidError):
                    await service.exchange(own, code=issued.code, csr_pem=csr, meta=meta, client_ip="203.0.113.20")
                await own.rollback()


class TestTheCodeAppearsNowhere:
    @given(reason=st.sampled_from(_REASONS + ("success",)))
    @_SETTINGS
    @pytest.mark.asyncio
    async def test_no_column_and_no_audit_row_carries_the_code(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: Any,
        reason: str,
    ) -> None:
        """Quantified over the OUTCOME, because a leak is most likely on the path nobody re-reads.

        Every column of every device row and every field of every audit row is searched for the code
        — not just the column it is expected to be absent from. A `details` mapping or a `reason`
        string is exactly where a code ends up when someone adds a log line to debug a refusal.
        """
        service = make_service(redis_client, per_ip_capacity=100, global_capacity=500, max_attempts=2)
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)

        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()

        if reason == "success":
            async with sessions() as own:
                await service.exchange(own, code=issued.code, csr_pem=csr, meta=meta, client_ip="203.0.113.21")
                await own.commit()
        else:
            code = await _unusable_code(service, sessions, project_id, actor, reason, csr, meta)
            async with sessions() as own:
                with pytest.raises(PairingCodeInvalidError):
                    await service.exchange(own, code=code, csr_pem=csr, meta=meta, client_ip="203.0.113.21")
                await own.rollback()

        async with sessions() as session:
            devices = (
                (
                    await session.execute(
                        text("SELECT * FROM agent_devices WHERE project_id = :project"), {"project": project_id}
                    )
                )
                .mappings()
                .all()
            )
            audits = (
                (
                    await session.execute(
                        text("SELECT * FROM audit_events WHERE project_id = :project"), {"project": project_id}
                    )
                )
                .mappings()
                .all()
            )

        needle = issued.code.upper()
        for row in list(devices) + list(audits):
            for column, value in dict(row).items():
                if value is None:
                    continue
                assert needle not in str(value).upper(), (
                    f"the pairing code appears in {column}; §14.6 stores only its HMAC"
                )


async def _unusable_code(
    service: Any,
    sessions: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
    actor: Any,
    reason: str,
    csr: bytes,
    meta: AgentMeta,
) -> str:
    """Produce a code that is unusable for the named reason.

    Each reason is reached through the mechanism production uses rather than by editing the code's
    semantics, so the refusal being compared is the one production would raise: expiry deletes the
    Redis key, which is exactly what its TTL elapsing does, and the burn drives the hash's own
    attempt counter to the cap, which is what a caller under attack does.
    """
    if reason == "unknown":
        return _neighbouring_code("ABCDEF")

    async with sessions() as session:
        issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
        await session.commit()

    if reason == "expired":
        await _delete_code_key(service, issued.code)
    elif reason == "burned":
        await _drive_attempts_to_the_cap(service, service._redis, issued.code, cap=2)  # noqa: SLF001
        async with sessions() as own:
            with pytest.raises(PairingCodeInvalidError):
                await service.exchange(own, code=issued.code, csr_pem=csr, meta=meta, client_ip="203.0.113.22")
            await own.rollback()
    elif reason == "not-pairable":
        async with sessions() as own:
            await own.execute(
                text("UPDATE agent_devices SET status = 'revoked' WHERE project_id = :project"),
                {"project": project_id},
            )
            await own.commit()
    return issued.code


def _code_key(service: Any, code: str) -> str:
    """The Redis key behind a code: §14.6's prefix over the HMAC of the code, never the code."""
    digest = service._pairing_digest(code)  # noqa: SLF001 - the key layout is §14.6's own
    return PAIRING_KEY_PREFIX + digest.hex()


async def _delete_code_key(service: Any, code: str) -> None:
    """Expire a code by deleting its key, which is what the TTL elapsing does."""
    await service._redis.delete(_code_key(service, code))  # noqa: SLF001


async def _drive_attempts_to_the_cap(service: Any, redis: Any, code: str, *, cap: int) -> None:
    """Set the code's own attempt counter to the cap, so the next presentation crosses it.

    The counter is per DIGEST, so presenting a different code cannot burn this one — it has its own
    key and its own counter. Leaf 8.1 drives the cap the same way for the same reason.
    """
    await redis.hset(_code_key(service, code), "attempts", cap)


def _neighbouring_code(code: str) -> str:
    """A code of the same shape that is not this one.

    Same alphabet and same length, one step along Crockford base32 per character, so it exercises
    the "unknown code" path rather than the "malformed input" path — a refusal for the wrong reason
    would make the burn clause pass without any attempt ever being counted.
    """
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "".join(alphabet[(alphabet.index(char) + 1) % len(alphabet)] if char in alphabet else char for char in code)
