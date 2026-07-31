# SPDX-License-Identifier: FSL-1.1-ALv2
"""Shared plumbing for driving chokepoint transits against real services (§2.2, §11.6).

Two suites need the same fixtures: `test_governance_chokepoint.py` asserts the seven transits
behave, and `tests/property/test_q04_audit_completeness.py` quantifies "exactly one audit row per
transit" over generated transit sequences. One copy, here, because two copies of a fixture is how
the two suites come to disagree about what a transit *is* — and the property would then be
quantifying over a shape the integration tests never exercise.

What is substituted, and why §0.4.1 permits it
----------------------------------------------
The real `SemanticPlanAnalyzer`, `ThresholdApprovalGate`, `AuditWriter`, `RedisEnvelopeSequencer`,
`DeviceService` custody path and envelope signing all run. Two collaborators are substituted, and
in both cases the production article **does not exist yet**: `GovernancePolicySource` arrives with
leaf 9.2 (it cannot precede leaf 9.1's bundle) and `CommandSink` with leaf 8.4's hub. Both doubles
below are ordinary classes implementing the real Protocol — §0.4.3's "signature-enforcing double"
— and never a `Mock`, which `FO-TD004` forbids under `tests/integration/**`.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.analysis.plan_analyzer.approval import ThresholdApprovalGate
from src.analysis.plan_analyzer.semantic import SemanticPlanAnalyzer
from src.audit.writer import AuditWriter
from src.auth.devices import DeviceService
from src.auth.models import UserRole
from src.auth.principal import Principal
from src.governance.chokepoint import ChangeItemRequest, GovernanceChokepoint
from src.governance.policy import GovernanceDecision
from src.governance.sequencing import RedisEnvelopeSequencer

from .capability import require_capability
from .migration_support import head_engine, schema_at_head  # noqa: F401 - re-exported fixtures

__all__ = [
    "PEPPER",
    "SAME_AS_BUNDLE",
    "STALE_DIGEST",
    "Fixture",
    "RecordingSink",
    "ScriptedPolicy",
    "allow",
    "audit_rows",
    "build_chokepoint",
    "change_sets",
    "deny",
    "fresh_digest",
    "handles",
    "head_engine",
    "make_fixture",
    "many_deletes",
    "one_create",
    "one_delete",
    "one_update",
    "redis_client",
    "redis_url",
    "require_approval",
    "schema_at_head",
    "sessions",
    "sha256_text",
    "sink",
]

#: Obviously synthetic and self-labelling, per `.kiro/steering/secret-safety.md`.
PEPPER = "test-only-not-a-real-secret-envelope-pepper"

#: A well-formed digest that is deliberately NOT any fixture's active bundle, for the stale-pin
#: refusal. Distinct from `fresh_digest()` so the mismatch is the test's own construction.
STALE_DIGEST = "sha256:" + "cd" * 32


class ScriptedPolicy:
    """A `GovernancePolicySource` whose answer each test states.

    A hand-written class, not a `Mock`: `FO-TD004` forbids any `Mock` under
    `tests/integration/**`, and the reason it does is D-23 — a reassigned `spec=`'d child
    implements the caller's shape rather than the callee's. This implements the Protocol's real
    signature, so a change to `evaluate`'s shape breaks it here rather than in production.
    """

    def __init__(self, *, decision: GovernanceDecision | None = None, raises: Exception | None = None) -> None:
        self._decision = decision
        self._raises = raises
        self.calls: list[Mapping[str, Any]] = []

    async def evaluate(self, *, payload: Mapping[str, Any]) -> GovernanceDecision:
        self.calls.append(payload)
        if self._raises is not None:
            raise self._raises
        assert self._decision is not None
        return self._decision


class RecordingSink:
    """A `CommandSink` that keeps what it was handed."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.sent: list[tuple[uuid.UUID, Any]] = []
        self._raises = raises

    async def send_command(self, *, device_id: uuid.UUID, command: Any) -> None:
        self.sent.append((device_id, command))
        if self._raises is not None:
            raise self._raises


def allow(reason: str = "no rule objected") -> GovernanceDecision:
    return GovernanceDecision(result="allow", reason=reason, rule_id="governance/allow")


def require_approval(reason: str = "prod requires approval") -> GovernanceDecision:
    return GovernanceDecision(result="require_approval", reason=reason, rule_id="governance/approval")


def deny(reason: str = "friday deploy window is closed") -> GovernanceDecision:
    return GovernanceDecision(result="deny", reason=reason, rule_id="governance/schedule")


# ─── fixtures, re-exported into each suite that needs them ────────────────────────────────
#
# Declared here and imported by name in each test module, which is the pattern
# `tests/integration/conftest.py` already uses for `production_app`: pytest discovers a fixture
# by module-level name, so an import is enough and the definition stays in one place.


@pytest.fixture(scope="session")
def redis_url() -> str:
    url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
    if not url:
        require_capability(
            "redis",
            "FORGEOPS_TEST_REDIS_URL is not set; §7.6 makes the envelope `seq` allocator "
            "Redis-authoritative, so these tests need a real Redis",
        )
    return url


@pytest_asyncio.fixture()
async def redis_client(redis_url: str) -> AsyncIterator[Any]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture()
async def sessions(head_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:  # noqa: F811
    return async_sessionmaker(head_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture()
async def sink() -> RecordingSink:
    return RecordingSink()


def build_chokepoint(
    *, policy: Any, sink: Any, redis_client: Any, analyzer: SemanticPlanAnalyzer | None = None
) -> GovernanceChokepoint:
    """A chokepoint over the real collaborators, with a per-instance Redis key prefix.

    The prefix is fresh per chokepoint so two tests cannot share a `seq` counter — a shared
    counter would make `seq` depend on test ordering, and an assertion about `seq` would then be
    a statement about the suite rather than about the allocator.
    """
    return GovernanceChokepoint(
        policy=policy,
        approval_gate=ThresholdApprovalGate(),
        analyzer=analyzer or SemanticPlanAnalyzer(),
        audit_writer=AuditWriter(),
        sequencer=RedisEnvelopeSequencer(redis_client, key_prefix=f"forgeops-test-{uuid.uuid4().hex[:8]}"),
        sink=sink,
        envelope_pepper=PEPPER,
        envelope_max_age_seconds=300,
    )


class Fixture:
    """One project, one user, one active device with a sealed key, one active bundle."""

    def __init__(
        self, project_id: uuid.UUID, user_id: uuid.UUID, device_id: uuid.UUID, key: bytes, digest: str
    ) -> None:
        self.project_id = project_id
        self.user_id = user_id
        self.device_id = device_id
        self.key = key
        self.digest = digest

    @property
    def principal(self) -> Principal:
        return Principal.for_user(
            user_id=self.user_id,
            subject=f"sub-{self.user_id.hex}",
            email=f"dev-{self.user_id.hex[:8]}@example.invalid",
            role=UserRole.DEVELOPER,
        )


def fresh_digest() -> str:
    """A distinct, well-formed bundle digest per fixture.

    `policy_bundles.digest` is globally unique (`uq_policy_bundles_digest`, §11.7), so a shared
    constant would make the second fixture in a session collide. Derived from a UUID rather than
    a counter so tests stay order-independent.
    """
    return "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex


#: Sentinel meaning "use this fixture's own fresh digest". `None` means "no digest at all", and
#: the two have to be distinguishable: a device pinned to nothing and a device pinned to the
#: wrong thing are different admission refusals.
SAME_AS_BUNDLE = object()


async def make_fixture(
    session: AsyncSession,
    *,
    device_status: str = "active",
    device_digest: Any = SAME_AS_BUNDLE,
    active_digest: Any = SAME_AS_BUNDLE,
    seal_key: bool = True,
) -> Fixture:
    """Insert the rows a transit needs, committed so a later rollback cannot remove them."""
    digest = fresh_digest()
    resolved_device = digest if device_digest is SAME_AS_BUNDLE else device_digest
    resolved_active = digest if active_digest is SAME_AS_BUNDLE else active_digest
    project_id, user_id, device_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, :path)"),
        {"id": project_id, "name": f"chokepoint-{project_id.hex[:8]}", "path": "/tmp/chokepoint"},
    )
    await session.execute(
        text(
            "INSERT INTO users (id, email, name, role, idp_subject, is_active) "
            "VALUES (:id, :email, 'Proof User', 'developer', :sub, true)"
        ),
        {"id": user_id, "email": f"dev-{user_id.hex[:8]}@example.invalid", "sub": f"sub-{user_id.hex}"},
    )
    await session.execute(
        text(
            "INSERT INTO agent_devices (id, project_id, status, agent_version, platform, "
            "policy_bundle_digest, last_seq, last_seen) "
            "VALUES (:id, :project, :status, '0.0.1', 'linux', :digest, 0, now())"
        ),
        {"id": device_id, "project": project_id, "status": device_status, "digest": resolved_device},
    )
    if resolved_active is not None:
        await session.execute(
            text(
                "INSERT INTO policy_bundles (id, digest, bundle, project_id, active) "
                "VALUES (:id, :digest, :bundle, :project, true)"
            ),
            {"id": uuid.uuid4(), "digest": resolved_active, "bundle": b"bundle", "project": project_id},
        )
    key = b""
    if seal_key:
        sealed = await DeviceService(pepper=PEPPER).provision_envelope_key(session, device_id)
        key = sealed.key.get_secret_value()
    await session.commit()
    return Fixture(project_id, user_id, device_id, key, str(resolved_device or ""))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def one_create(path: str = "docker-compose.yml") -> tuple[ChangeItemRequest, ...]:
    return (ChangeItemRequest(file_path=path, action="create", new_content="services: {}\n"),)


def one_update(path: str = "Dockerfile") -> tuple[ChangeItemRequest, ...]:
    return (ChangeItemRequest(file_path=path, action="update", old_content="FROM a\n", new_content="FROM b\n"),)


def one_delete(path: str = "docker-compose.yml") -> tuple[ChangeItemRequest, ...]:
    return (ChangeItemRequest(file_path=path, action="delete", old_content="services: {}\n"),)


def many_deletes(count: int) -> tuple[ChangeItemRequest, ...]:
    return tuple(
        ChangeItemRequest(file_path=f"deleted-{index}.yml", action="delete", old_content="x\n")
        for index in range(count)
    )


async def audit_rows(session: AsyncSession, project_id: uuid.UUID) -> list[Mapping[str, Any]]:
    result = await session.execute(
        text(
            "SELECT seq, action, outcome, reason, resource_kind, resource_id, actor_kind, actor_user_id "
            "FROM audit_events WHERE project_id = :project ORDER BY seq ASC"
        ),
        {"project": project_id},
    )
    return list(result.mappings().all())


async def change_sets(session: AsyncSession, project_id: uuid.UUID) -> list[Mapping[str, Any]]:
    result = await session.execute(
        text(
            "SELECT id, status, version, blast_radius_score, blast_radius_verdict FROM change_sets "
            "WHERE project_id = :project ORDER BY created_at ASC"
        ),
        {"project": project_id},
    )
    return list(result.mappings().all())


async def handles(session: AsyncSession, change_set_id: uuid.UUID) -> list[Mapping[str, Any]]:
    result = await session.execute(
        text("SELECT id, consumed, backup_manifest, agent_device_id FROM rollback_handles WHERE change_set_id = :cs"),
        {"cs": change_set_id},
    )
    return list(result.mappings().all())
