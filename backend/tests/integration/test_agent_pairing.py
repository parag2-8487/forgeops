# SPDX-License-Identifier: FSL-1.1-ALv2
"""Pairing-code issue and single-use exchange, against real Postgres and Redis (§3.1, §14.6, A.1).

Why every one of these needs real services
------------------------------------------
Each clause below is a claim about a *serialisation point* or a *transaction*, and neither exists
in a fake. "At most one of N concurrent attempts succeeds" is a statement about Redis executing
one `EVAL` to completion; "the code is unusable even from the database" is a statement about a
committed `UPDATE`. A double would let both pass while the real thing failed.

What is substituted, and why §0.4.1 permits it
----------------------------------------------
Nothing that exists. The real `DeviceService`, the real `GovernanceDeviceAuditRecorder`, the real
`AuditWriter` with its hash chain, the real `RedisTokenBucketLimiter` and the real Lua scripts all
run. The only test-owned objects are the two limiter *sizings* — a test that had to spend 600
requests to observe the global cap would be a test nobody runs — and a `DeviceService` configured
with a short TTL for the expiry clause. Both are configuration of the production article, not a
replacement for it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509 import CertificateSigningRequestBuilder, Name, NameAttribute
from cryptography.x509.oid import NameOID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.ai.rate_limit.redis_bucket import RedisTokenBucketLimiter
from src.audit.writer import AuditWriter
from src.auth.ca import (
    CertificateAuthorityUnavailableError,
    CertificateRejectedError,
    InternalCertificateAuthority,
    UnavailableCertificateAuthority,
    generate_development_ca,
)
from src.auth.devices import (
    PAIRING_KEY_PREFIX,
    AgentMeta,
    CertificateRotationRefusedError,
    CsrRejectedError,
    DeviceNotFoundError,
    DeviceService,
    PairingCodeInvalidError,
    PairingRateLimitedError,
    csr_spki_fingerprint,
)
from src.auth.models import UserRole
from src.auth.pairing_limits import PairingUnavailableError, TokenBucketPairingLimiter
from src.auth.principal import Principal
from src.governance.device_audit import GovernanceDeviceAuditRecorder

from .chokepoint_support import PEPPER

# `sessions`, `redis_client` and `redis_url` are deliberately NOT imported: `conftest.py`
# re-exports them, so pytest discovers them by name. Importing them here would shadow the
# `sessions` parameter of almost every method below — which is the same reason
# `chokepoint_support.py`'s own docstring gives for routing them through conftest.

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


# ─── helpers ──────────────────────────────────────────────────────────────────────────────


def build_csr(*, key: ec.EllipticCurvePrivateKey | None = None) -> tuple[bytes, str]:
    """A real P-256 CSR and its SubjectPublicKeyInfo SHA-256, as the agent will send them."""
    private = key or ec.generate_private_key(ec.SECP256R1())
    csr = (
        CertificateSigningRequestBuilder()
        .subject_name(Name([NameAttribute(NameOID.COMMON_NAME, "forgeops-agent")]))
        .sign(private, hashes.SHA256())
    )
    pem = csr.public_bytes(serialization.Encoding.PEM)
    return pem, csr_spki_fingerprint(pem)


def build_rsa_csr() -> bytes:
    """A well-formed CSR with the wrong key type. §3.1 fixes the curve at P-256."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        CertificateSigningRequestBuilder()
        .subject_name(Name([NameAttribute(NameOID.COMMON_NAME, "forgeops-agent")]))
        .sign(private, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM)


#: One development CA for the whole module. Generated once because key generation is the slowest
#: thing in this file by an order of magnitude, and every test wants the *same* issuer so a
#: certificate from one test is verifiable in another.
_CA_CERT_PEM, _CA_KEY_PEM = generate_development_ca()


def build_ca(*, ttl_hours: int = 24, renew_before_hours: int = 6, clock: Any = None) -> InternalCertificateAuthority:
    """The production CA over the module's development key material."""
    return InternalCertificateAuthority(
        cert_pem=_CA_CERT_PEM,
        key_pem=_CA_KEY_PEM,
        ttl_hours=ttl_hours,
        renew_before_hours=renew_before_hours,
        clock=clock,
    )


def make_service(
    redis: Any,
    *,
    ttl_seconds: int = 300,
    max_attempts: int = 5,
    per_ip_capacity: int = 10,
    global_capacity: int = 600,
    refill_rate: float = 0.0001,
    ca: Any = None,
) -> DeviceService:
    """The production `DeviceService`, sized so a cap can be observed inside one test.

    `refill_rate` is deliberately near zero rather than §14.6's real rate: the bucket is
    Redis-clock-driven, so a test that wanted to see a refill would have to sleep for real
    seconds. The clause under test is the *cap*, not the refill, and the cap is the same object
    either way.
    """
    return DeviceService(
        pepper=PEPPER,
        recorder=GovernanceDeviceAuditRecorder(writer=AuditWriter()),
        redis=redis,
        limiter=TokenBucketPairingLimiter(
            per_ip=RedisTokenBucketLimiter(
                redis=redis,
                capacity=per_ip_capacity,
                refill_rate=refill_rate,
                key_prefix=f"forgeops-test:pair:ip:{uuid.uuid4().hex[:8]}:",
            ),
            global_bucket=RedisTokenBucketLimiter(
                redis=redis,
                capacity=global_capacity,
                refill_rate=refill_rate,
                key_prefix=f"forgeops-test:pair:global:{uuid.uuid4().hex[:8]}:",
            ),
        ),
        ca=ca if ca is not None else build_ca(),
        code_ttl_seconds=ttl_seconds,
        max_attempts=max_attempts,
    )


async def make_project(session: AsyncSession) -> tuple[uuid.UUID, Principal]:
    """One project and one admin principal, committed."""
    project_id, user_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, '/tmp/pairing')"),
        {"id": project_id, "name": f"pairing-{project_id.hex[:8]}"},
    )
    await session.execute(
        text(
            "INSERT INTO users (id, email, name, role, idp_subject, is_active) "
            "VALUES (:id, :email, 'Pairing Operator', 'admin', :sub, true)"
        ),
        {"id": user_id, "email": f"ops-{user_id.hex[:8]}@example.invalid", "sub": f"sub-{user_id.hex}"},
    )
    await session.commit()
    principal = Principal.for_user(
        user_id=user_id,
        subject=f"sub-{user_id.hex}",
        email=f"ops-{user_id.hex[:8]}@example.invalid",
        role=UserRole.ADMIN,
    )
    return project_id, principal


async def device_rows(session: AsyncSession, project_id: uuid.UUID) -> list[Mapping[str, Any]]:
    result = await session.execute(
        text(
            "SELECT id, status, pairing_token_hmac, device_token_hmac, envelope_key_enc, "
            "agent_version, platform, pairing_expires_at, revoked_at, cert_serial, "
            "cert_fingerprint, cert_not_after "
            "FROM agent_devices WHERE project_id = :project ORDER BY created_at ASC"
        ),
        {"project": project_id},
    )
    return list(result.mappings().all())


async def audit_for(session: AsyncSession, project_id: uuid.UUID) -> list[Mapping[str, Any]]:
    result = await session.execute(
        text(
            "SELECT seq, action, outcome, reason, resource_kind, resource_id, actor_kind, "
            "actor_user_id, after_state FROM audit_events WHERE project_id = :project "
            "ORDER BY seq ASC"
        ),
        {"project": project_id},
    )
    return list(result.mappings().all())


@pytest_asyncio.fixture()
async def service(redis_client: Any) -> DeviceService:
    return make_service(redis_client)


# ─── issue ────────────────────────────────────────────────────────────────────────────────


class TestIssuePairingCode:
    async def test_it_returns_a_code_and_stores_only_its_hmac(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService, redis_client: Any
    ) -> None:
        """The single most important clause: the code exists in the return value and nowhere else.

        Checked against the whole `agent_devices` row rather than against the column it is
        expected to be in, because "stored only as an HMAC" is a claim about every column.
        """
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            rows = await device_rows(session, project_id)

        assert len(issued.code) == 6
        assert set(issued.code) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "pending"
        assert row["pairing_token_hmac"] is not None
        assert len(bytes(row["pairing_token_hmac"])) == 32
        for column, value in row.items():
            rendered = value.hex() if isinstance(value, bytes | memoryview) else str(value)
            assert issued.code not in rendered, f"the code leaked into column {column}"

        stored = await redis_client.hgetall(PAIRING_KEY_PREFIX + bytes(row["pairing_token_hmac"]).hex())
        assert stored, "the Redis payload was not written"
        for field, value in stored.items():
            assert issued.code not in str(value), f"the code leaked into the Redis field {field}"
        assert str(issued.code) not in str(stored)

    async def test_the_redis_key_carries_the_ttl(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """A code with no expiry is the one failure §14.6's per-window arithmetic cannot survive."""
        service = make_service(redis_client, ttl_seconds=120)
        async with sessions() as session:
            project_id, actor = await make_project(session)
            await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            rows = await device_rows(session, project_id)
        ttl = await redis_client.ttl(PAIRING_KEY_PREFIX + bytes(rows[0]["pairing_token_hmac"]).hex())
        assert 0 < ttl <= 120

    async def test_a_second_issue_abandons_the_first(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService, redis_client: Any
    ) -> None:
        """A.1's `RevokeLiveCodesFor`. "One live code per project" is what §14.6 counts on."""
        async with sessions() as session:
            project_id, actor = await make_project(session)
            first = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            first_rows = await device_rows(session, project_id)
            first_digest = bytes(first_rows[0]["pairing_token_hmac"]).hex()

            second = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            rows = await device_rows(session, project_id)

        statuses = sorted(row["status"] for row in rows)
        assert statuses == ["abandoned", "pending"]
        abandoned = next(row for row in rows if row["status"] == "abandoned")
        assert abandoned["pairing_token_hmac"] is None, "an abandoned code must not remain in the DB"
        assert await redis_client.exists(PAIRING_KEY_PREFIX + first_digest) == 0
        assert first.code != second.code

    async def test_the_first_code_stops_working_once_replaced(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """The behaviour the clause above is for. Abandoning a row nobody re-checks proves nothing."""
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            first = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            with pytest.raises(PairingCodeInvalidError):
                await service.exchange(
                    session,
                    code=first.code,
                    csr_pem=csr,
                    meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                    client_ip="203.0.113.9",
                )

    async def test_it_writes_one_audit_row_naming_the_operator(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """Appendix A.1's `Audit(actor, "pairing_code_issued", …)`, and D-70's shape."""
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            rows = await audit_for(session, project_id)

        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "pairing_code_issued"
        assert row["resource_kind"] == "agent_device"
        assert row["outcome"] == "allowed"
        assert row["actor_kind"] == "user"
        assert row["actor_user_id"] == actor.user_id
        assert row["resource_id"] == str(issued.device_id)
        assert issued.code not in str(dict(row)), "the code reached the audit row"


# ─── exchange ─────────────────────────────────────────────────────────────────────────────


class TestExchangeSucceedsOnce:
    async def test_a_valid_code_yields_a_token_and_an_envelope_key(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            credentials = await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux/amd64", fingerprint=fingerprint),
                client_ip="203.0.113.7",
            )
            await session.commit()
            rows = await device_rows(session, project_id)
            audit = await audit_for(session, project_id)

        assert credentials.device_id == issued.device_id
        assert credentials.project_id == project_id
        assert len(credentials.device_token.get_secret_value()) == 32
        assert len(credentials.envelope_key.get_secret_value()) == 32
        assert credentials.csr_spki_sha256 == fingerprint

        row = next(row for row in rows if row["id"] == issued.device_id)
        assert row["status"] == "active"
        # A.1: `pairing_token_hmac ← NULL` — "the code cannot be reused, even in the DB".
        assert row["pairing_token_hmac"] is None
        assert row["pairing_expires_at"] is None
        assert row["device_token_hmac"] is not None
        assert bytes(row["device_token_hmac"]) != credentials.device_token.get_secret_value()
        assert row["envelope_key_enc"] is not None
        assert row["agent_version"] == "0.1.0"
        assert row["platform"] == "linux/amd64"

        assert [entry["action"] for entry in audit] == ["pairing_code_issued", "device_paired"]
        paired = audit[-1]
        assert paired["actor_user_id"] == actor.user_id, "A.1 attributes the pairing to the issuer"
        assert paired["after_state"]["csr_spki_sha256"] == fingerprint
        assert issued.code not in str(dict(paired))

    async def test_the_envelope_key_round_trips_through_the_sealed_column(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """The exchange must use `provision_envelope_key`, so D-62's AAD binding is not bypassed.

        Proved by unsealing: a key sealed without the device id as additional authenticated data
        would not open under it.
        """
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            credentials = await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.7",
            )
            await session.commit()
            recovered = await service.envelope_key(session, issued.device_id)
        assert recovered.get_secret_value() == credentials.envelope_key.get_secret_value()

    async def test_a_second_exchange_of_the_same_code_fails(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """Single use, sequentially. The concurrent form is Q-17's (leaf 8.11)."""
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            await service.exchange(session, code=issued.code, csr_pem=csr, meta=meta, client_ip="203.0.113.7")
            await session.commit()
            with pytest.raises(PairingCodeInvalidError):
                await service.exchange(session, code=issued.code, csr_pem=csr, meta=meta, client_ip="203.0.113.7")

    async def test_concurrent_attempts_yield_at_most_one_success(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """Atomicity is what makes single-use TRUE, and this is the clause that shows it.

        Six concurrent exchanges on one code, each on its own session so the database work really
        is concurrent. Exactly one must succeed: the `EVAL` is the serialisation point, and a
        read-then-delete pair in the application would let two callers both read before either
        deleted.
        """
        service = make_service(redis_client, per_ip_capacity=50, global_capacity=50)
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()

        async def attempt() -> str:
            async with sessions() as own:
                try:
                    await service.exchange(own, code=issued.code, csr_pem=csr, meta=meta, client_ip="203.0.113.8")
                    await own.commit()
                    return "ok"
                except PairingCodeInvalidError:
                    await own.commit()
                    return "invalid"

        outcomes = await asyncio.gather(*(attempt() for _ in range(6)))
        assert outcomes.count("ok") == 1, outcomes


class TestEveryRefusalLooksTheSame:
    async def test_an_unknown_code_is_refused(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, _ = await make_project(session)
            with pytest.raises(PairingCodeInvalidError):
                await service.exchange(
                    session,
                    code="ZZZZZZ",
                    csr_pem=csr,
                    meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                    client_ip="203.0.113.10",
                )
            await session.commit()
            rows = await audit_for(session, project_id)
        # The failure row is written with no project (the code named none), so the project-scoped
        # query is empty — which is itself the assertion: a refusal cannot attribute itself to a
        # project it never identified.
        assert rows == []

    async def test_an_expired_code_is_refused_and_indistinguishable(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """Expiry is the Redis TTL, so an expired code is byte-identically `missing` to the caller.

        Driven by deleting the key rather than by sleeping: the TTL is the mechanism and
        `redis.expire(key, 0)` reaches the same state the clock would, in a test that does not
        take five minutes.
        """
        service = make_service(redis_client, ttl_seconds=60)
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            rows = await device_rows(session, project_id)
            key = PAIRING_KEY_PREFIX + bytes(rows[0]["pairing_token_hmac"]).hex()
            await redis_client.expire(key, 0)
            assert await redis_client.exists(key) == 0

            with pytest.raises(PairingCodeInvalidError):
                await service.exchange(
                    session,
                    code=issued.code,
                    csr_pem=csr,
                    meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                    client_ip="203.0.113.11",
                )
            await session.commit()
            audit = await audit_for(session, project_id)
        assert [entry["action"] for entry in audit] == ["pairing_code_issued"]

    async def test_the_burn_branch_deletes_the_code(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """§3.7's `issued --> burned : 5 failed attempts`, driven to the state that triggers it.

        `attempts` is set to the cap directly rather than by presenting the code five times. The
        state is reachable by any five presentations inside the window; constructing it is how the
        *branch* gets exercised without the test depending on five round trips. The clause being
        proved is that at the cap the code is DELETED, so a code under attack stops working even
        for its owner.
        """
        service = make_service(redis_client, max_attempts=3)
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            rows = await device_rows(session, project_id)
            key = PAIRING_KEY_PREFIX + bytes(rows[0]["pairing_token_hmac"]).hex()
            await redis_client.hset(key, "attempts", "3")

            with pytest.raises(PairingCodeInvalidError):
                await service.exchange(
                    session,
                    code=issued.code,
                    csr_pem=csr,
                    meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                    client_ip="203.0.113.12",
                )
            await session.commit()
            assert await redis_client.exists(key) == 0, "the burn branch must delete the code"
            audit = await audit_for(session, project_id)
            still_pending = await device_rows(session, project_id)

        assert [entry["action"] for entry in audit] == ["pairing_code_issued"]
        assert still_pending[0]["status"] == "pending", "a burn issues nothing"

    async def test_the_control_shows_the_same_code_succeeds_below_the_cap(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """Without this, the burn clause passes for a service that refuses everything."""
        service = make_service(redis_client, max_attempts=3)
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            rows = await device_rows(session, project_id)
            key = PAIRING_KEY_PREFIX + bytes(rows[0]["pairing_token_hmac"]).hex()
            await redis_client.hset(key, "attempts", "2")
            credentials = await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.13",
            )
            await session.commit()
        assert credentials.device_id == issued.device_id

    async def test_a_revoked_device_row_refuses_its_own_code(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """The `device-not-pairable` branch: the code was consumable, the row was not.

        Indistinguishable in the response from an unknown code, and the internal branch reaches
        the audit row's `failure_kind` and stops there.
        """
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            await service.revoke(session, device_id=issued.device_id, actor=actor, reason="revoked before exchange")
            await session.commit()
            with pytest.raises(PairingCodeInvalidError):
                await service.exchange(
                    session,
                    code=issued.code,
                    csr_pem=csr,
                    meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                    client_ip="203.0.113.14",
                )
            await session.commit()
            audit = await audit_for(session, project_id)
        actions = [entry["action"] for entry in audit]
        assert actions == ["pairing_code_issued", "device_revoked", "pairing_failed"]
        failure = audit[-1]
        assert failure["outcome"] == "denied"
        assert failure["actor_kind"] == "system"
        assert failure["after_state"] == {"failure_kind": "device-not-pairable"}
        assert issued.code not in str(dict(failure))


class TestTheCsrIsCheckedBeforeTheCodeIsSpent:
    async def test_a_malformed_csr_is_rejected_and_the_code_survives(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """The one deliberate reordering of A.1, and the reason for it.

        A.1 signs the CSR after the consume. Validating first means a broken agent cannot spend a
        valid code's single use — proved here by exchanging successfully afterwards.
        """
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            with pytest.raises(CsrRejectedError, match="readable PEM CSR"):
                await service.exchange(
                    session,
                    code=issued.code,
                    # Deliberately not PEM-shaped. `load_pem_x509_csr` refuses these bytes for
                    # exactly the reason under test, and a literal carrying a PEM begin-armour
                    # line would match the mandatory pre-push shape grep in
                    # `.kiro/steering/secret-safety.md` on every future run — a permanent false
                    # positive in a gate whose value is that a match means stop.
                    csr_pem=b"this is not a certificate request",
                    meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                    client_ip="203.0.113.15",
                )
            credentials = await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.15",
            )
            await session.commit()
        assert credentials.device_id == issued.device_id

    async def test_an_rsa_csr_is_rejected(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            with pytest.raises(CsrRejectedError, match="P-256"):
                await service.exchange(
                    session,
                    code=issued.code,
                    csr_pem=build_rsa_csr(),
                    meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint="0" * 64),
                    client_ip="203.0.113.16",
                )
        assert project_id  # the row exists; the refusal is about the key type, not the project

    async def test_a_fingerprint_that_does_not_match_the_csr_is_rejected(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """The field §3.1 lists and does not define. Defining it means checking it."""
        csr, _ = build_csr()
        _, other = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            with pytest.raises(CsrRejectedError, match="declared fingerprint"):
                await service.exchange(
                    session,
                    code=issued.code,
                    csr_pem=csr,
                    meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=other),
                    client_ip="203.0.113.17",
                )
        assert issued.device_id


class TestTheRateLimitCaps:
    async def test_the_per_ip_bucket_refuses_beyond_its_capacity(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """§14.6's per-IP cap, and it must bite BEFORE the consume script runs."""
        service = make_service(redis_client, per_ip_capacity=3, global_capacity=1000)
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)
        async with sessions() as session:
            await make_project(session)
            for _ in range(3):
                with pytest.raises(PairingCodeInvalidError):
                    await service.exchange(session, code="ZZZZZZ", csr_pem=csr, meta=meta, client_ip="198.51.100.1")
            with pytest.raises(PairingRateLimitedError) as raised:
                await service.exchange(session, code="ZZZZZZ", csr_pem=csr, meta=meta, client_ip="198.51.100.1")
            await session.commit()
        assert raised.value.retry_after_seconds >= 1

    async def test_a_different_ip_is_unaffected(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """Without this, the clause above passes for a limiter that refuses every caller."""
        service = make_service(redis_client, per_ip_capacity=2, global_capacity=1000)
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)
        async with sessions() as session:
            await make_project(session)
            for _ in range(2):
                with pytest.raises(PairingCodeInvalidError):
                    await service.exchange(session, code="ZZZZZZ", csr_pem=csr, meta=meta, client_ip="198.51.100.2")
            with pytest.raises(PairingCodeInvalidError):
                await service.exchange(session, code="ZZZZZZ", csr_pem=csr, meta=meta, client_ip="198.51.100.3")
            await session.commit()

    async def test_the_global_bucket_binds_a_distributed_caller(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """The bound the per-IP cap cannot express: one attempt each from many addresses."""
        service = make_service(redis_client, per_ip_capacity=100, global_capacity=3)
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)
        async with sessions() as session:
            await make_project(session)
            for index in range(3):
                with pytest.raises(PairingCodeInvalidError):
                    await service.exchange(
                        session, code="ZZZZZZ", csr_pem=csr, meta=meta, client_ip=f"198.51.100.{20 + index}"
                    )
            with pytest.raises(PairingRateLimitedError):
                await service.exchange(session, code="ZZZZZZ", csr_pem=csr, meta=meta, client_ip="198.51.100.99")
            await session.commit()

    async def test_an_unreachable_limiter_refuses_rather_than_allows(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """Fail closed. A limiter that cannot be evaluated must not admit an unbounded caller.

        The failure is injected in the one place that produces a real outage without a container
        restart: a Redis client whose `eval` raises. It is a signature-enforcing double of the
        `TokenBucket` Protocol, not a `Mock` (FO-TD004).
        """

        class UnreachableBucket:
            async def check(self, bucket_id: str, *, tokens: int = 1) -> Any:
                raise ConnectionError("redis is unreachable")

        service = DeviceService(
            pepper=PEPPER,
            recorder=GovernanceDeviceAuditRecorder(writer=AuditWriter()),
            redis=redis_client,
            limiter=TokenBucketPairingLimiter(per_ip=UnreachableBucket(), global_bucket=UnreachableBucket()),
            ca=build_ca(),
        )
        csr, fingerprint = build_csr()
        async with sessions() as session:
            await make_project(session)
            with pytest.raises(PairingUnavailableError):
                await service.exchange(
                    session,
                    code="ZZZZZZ",
                    csr_pem=csr,
                    meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                    client_ip="198.51.100.7",
                )


class TestRevoke:
    async def test_it_marks_the_row_and_records_once(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """Idempotent by predicate: a second call writes no second row."""
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.20",
            )
            await session.commit()
            await service.revoke(session, device_id=issued.device_id, actor=actor, reason="laptop lost")
            await service.revoke(session, device_id=issued.device_id, actor=actor, reason="laptop lost again")
            await session.commit()
            rows = await device_rows(session, project_id)
            audit = await audit_for(session, project_id)

        row = next(row for row in rows if row["id"] == issued.device_id)
        assert row["status"] == "revoked"
        assert row["revoked_at"] is not None
        assert [entry["action"] for entry in audit] == [
            "pairing_code_issued",
            "device_paired",
            "device_revoked",
        ]
        assert audit[-1]["reason"] == "laptop lost"

    async def test_an_unknown_device_raises(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        async with sessions() as session:
            _, actor = await make_project(session)
            with pytest.raises(DeviceNotFoundError):
                await service.revoke(session, device_id=uuid.uuid4(), actor=actor, reason="does not exist")

    async def test_a_revoked_device_cannot_produce_its_envelope_key(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """Leaf 7.5's custody refusal, now reachable through the real revocation path."""
        from src.auth.devices import EnvelopeKeyUnavailableError

        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.21",
            )
            await session.commit()
            assert await service.envelope_key(session, issued.device_id)
            await service.revoke(session, device_id=issued.device_id, actor=actor, reason="revoked")
            await session.commit()
            with pytest.raises(EnvelopeKeyUnavailableError):
                await service.envelope_key(session, issued.device_id)


class TestTheHalfWiredServiceIsRefused:
    async def test_a_partial_collaborator_set_raises_at_construction(self, redis_client: Any) -> None:
        """A service with Redis but no recorder would consume a code and record nothing."""
        from src.auth.devices import DeviceKeyError

        with pytest.raises(DeviceKeyError, match="needs all four"):
            DeviceService(pepper=PEPPER, redis=redis_client)

    async def test_the_custody_only_form_still_works(self, redis_client: Any) -> None:
        """Leaf 7.5's form must keep constructing; the chokepoint builds exactly this one."""
        service = DeviceService(pepper=PEPPER)
        with pytest.raises(Exception, match="custody only"):
            await service.exchange(
                None,  # type: ignore[arg-type]
                code="ZZZZZZ",
                csr_pem=b"",
                meta=AgentMeta(agent_version="0", platform="linux", fingerprint="0" * 64),
                client_ip="",
            )


# ─── leaf 8.2: the internal CA and short-lived device certificates ────────────────────────


class TestTheExchangeIssuesACertificate:
    async def test_the_certificate_chains_to_the_ca_and_names_the_device(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """§3.1's `client_cert` and `ca_bundle`, verified rather than merely returned.

        Three things are asserted about the certificate rather than one: it verifies under the CA,
        its subject CN is the **device id** (not the CSR's subject — the CA rewrites it), and its
        public key is the one the agent proved possession of. The third is what makes the
        certificate useful: a certificate over somebody else's key would chain fine and be
        unusable.
        """
        private = ec.generate_private_key(ec.SECP256R1())
        csr, fingerprint = build_csr(key=private)
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            credentials = await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.30",
            )
            await session.commit()
            rows = await device_rows(session, project_id)

        ca = build_ca()
        certificate = ca.verify_chain(credentials.client_cert_pem)
        assert certificate.subject.rfc4514_string() == f"CN={issued.device_id}"
        assert certificate.public_key().public_numbers() == private.public_key().public_numbers()
        assert credentials.ca_bundle_pem == ca.ca_bundle

        row = next(row for row in rows if row["id"] == issued.device_id)
        assert row["cert_serial"] == credentials.cert_serial
        assert row["cert_fingerprint"] == credentials.cert_fingerprint
        assert row["cert_not_after"] is not None
        # The row's fingerprint must be the fingerprint OF the issued certificate, computed
        # independently here — if the service stored a digest of something else, the handshake
        # check in §3.1 would reject every genuine device.
        expected = ":".join(f"{byte:02X}" for byte in certificate.fingerprint(hashes.SHA256()))
        assert row["cert_fingerprint"] == expected

    async def test_the_ttl_and_the_renew_window_come_from_configuration(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """`notAfter = now + DEVICE_CERT_TTL_HOURS`, and `renew_after` is that minus the margin."""
        service = make_service(redis_client, ca=build_ca(ttl_hours=3, renew_before_hours=1))
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            credentials = await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.31",
            )
            await session.commit()
        window = credentials.cert_not_after - credentials.renew_after
        assert window == timedelta(hours=1)
        assert timedelta(hours=2, minutes=58) < credentials.cert_not_after - datetime.now(UTC) <= timedelta(hours=3)

    async def test_no_ca_refuses_without_spending_the_code(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: Any
    ) -> None:
        """Availability is checked BEFORE the consume, so a misconfigured deployment burns nothing.

        The proof is the second half: after the 503, the same code still works against a service
        that does have a CA. Without the pre-check the code would have been deleted by the consume
        script and the operator would have to reissue it on every attempt.
        """
        broken = make_service(redis_client, ca=UnavailableCertificateAuthority())
        csr, fingerprint = build_csr()
        meta = AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint)
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await broken.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            with pytest.raises(CertificateAuthorityUnavailableError, match="make init-ca"):
                await broken.exchange(session, code=issued.code, csr_pem=csr, meta=meta, client_ip="203.0.113.32")
            working = make_service(redis_client)
            credentials = await working.exchange(
                session, code=issued.code, csr_pem=csr, meta=meta, client_ip="203.0.113.32"
            )
            await session.commit()
        assert credentials.device_id == issued.device_id


class TestRotation:
    async def test_it_replaces_the_serial_and_fingerprint_without_reconnection(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """§3.1's `cert_renewing -> active`: a new certificate over the live session.

        "Without reconnection" is asserted as the thing it means here — the device row stays
        `active` throughout and no pairing code is involved — since there is no socket to hold open
        until leaf 8.4.
        """
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            first = await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.33",
            )
            await session.commit()

            rotated_csr, _ = build_csr()
            bundle = await service.rotate_certificate(session, device_id=issued.device_id, csr_pem=rotated_csr)
            await session.commit()
            rows = await device_rows(session, project_id)

        assert bundle.serial != first.cert_serial
        assert bundle.fingerprint != first.cert_fingerprint
        row = next(row for row in rows if row["id"] == issued.device_id)
        assert row["status"] == "active"
        assert row["cert_serial"] == bundle.serial
        assert row["cert_fingerprint"] == bundle.fingerprint
        build_ca().verify_chain(bundle.certificate_pem)

    async def test_the_new_key_is_the_rotated_key_not_the_original(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """Rotation exists to change the KEY, not only the dates. This is that clause."""
        original = ec.generate_private_key(ec.SECP256R1())
        replacement = ec.generate_private_key(ec.SECP256R1())
        csr, fingerprint = build_csr(key=original)
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.34",
            )
            await session.commit()
            rotated_csr, _ = build_csr(key=replacement)
            bundle = await service.rotate_certificate(session, device_id=issued.device_id, csr_pem=rotated_csr)
            await session.commit()
        certificate = build_ca().verify_chain(bundle.certificate_pem)
        assert certificate.public_key().public_numbers() == replacement.public_key().public_numbers()
        assert certificate.public_key().public_numbers() != original.public_key().public_numbers()

    async def test_a_revoked_device_is_refused(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """An attacker holding an expiring certificate must not be able to renew it."""
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.35",
            )
            await session.commit()
            await service.revoke(session, device_id=issued.device_id, actor=actor, reason="stolen")
            await session.commit()
            rotated_csr, _ = build_csr()
            with pytest.raises(CertificateRotationRefusedError, match="not active"):
                await service.rotate_certificate(session, device_id=issued.device_id, csr_pem=rotated_csr)
        assert project_id

    async def test_a_pending_device_is_refused(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """A device that never exchanged its code has no certificate to rotate."""
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            rotated_csr, _ = build_csr()
            with pytest.raises(CertificateRotationRefusedError, match="pending"):
                await service.rotate_certificate(session, device_id=issued.device_id, csr_pem=rotated_csr)
        assert project_id

    async def test_an_unknown_device_raises_not_found(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        async with sessions() as session:
            await make_project(session)
            rotated_csr, _ = build_csr()
            with pytest.raises(DeviceNotFoundError):
                await service.rotate_certificate(session, device_id=uuid.uuid4(), csr_pem=rotated_csr)

    async def test_a_rejected_csr_leaves_the_previous_certificate_in_place(
        self, sessions: async_sessionmaker[AsyncSession], service: DeviceService
    ) -> None:
        """A failed rotation must not leave the device with no usable certificate."""
        csr, fingerprint = build_csr()
        async with sessions() as session:
            project_id, actor = await make_project(session)
            issued = await service.issue_pairing_code(session, project_id=project_id, actor=actor)
            await session.commit()
            first = await service.exchange(
                session,
                code=issued.code,
                csr_pem=csr,
                meta=AgentMeta(agent_version="0.1.0", platform="linux", fingerprint=fingerprint),
                client_ip="203.0.113.36",
            )
            await session.commit()
            with pytest.raises(CertificateRejectedError):
                await service.rotate_certificate(session, device_id=issued.device_id, csr_pem=build_rsa_csr())
            await session.commit()
            rows = await device_rows(session, project_id)
        row = next(row for row in rows if row["id"] == issued.device_id)
        assert row["cert_serial"] == first.cert_serial
        assert row["cert_fingerprint"] == first.cert_fingerprint
