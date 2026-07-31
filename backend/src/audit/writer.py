# SPDX-License-Identifier: FSL-1.1-ALv2
"""The append-only, hash-chained audit writer (design.md §6.3, §6.4, §11.9, Appendix A.8).

What this module guarantees, and by which mechanism
---------------------------------------------------
* **One record per governance transit, committed with the transit.** `append` joins the
  **caller's** transaction and never commits. The change-set state transition and its audit
  record therefore commit or roll back together, which is what makes Q-04's "exactly one record
  per transit" provable rather than probable: there is no window in which one exists without the
  other, and a failed audit write aborts the mutation because the exception propagates into the
  caller's transaction.
* **Tamper evidence without a second copy.** `hash = sha256(JCS(semantic fields) || prev_hash)`
  per tenant. Editing any row invalidates its own hash and every later one; deleting a row
  leaves a `seq` gap *and* breaks the chain. `verify_chain` recomputes from any start point and
  reports the first divergence.
* **A well-defined chain.** Appends serialise on a transaction-scoped advisory lock keyed by
  tenant, because "the previous row" is only meaningful under serial append. Two concurrent
  appenders without the lock would both read the same `prev_hash` and produce a fork.

Why the lock is per tenant and transaction-scoped
-------------------------------------------------
Per tenant, because the chain is per tenant: serialising every tenant behind one lock would make
one noisy tenant everybody's problem. Transaction-scoped (`pg_advisory_xact_lock`), because a
session-scoped lock outlives a rollback and a pooled connection hands that session to the next
request — the classic advisory-lock leak. §11.9 records the cost up front rather than having it
discovered under load: audit writes for one tenant are serial, and that is acceptable because
every write is a governance transit and those are human-paced.

What is deliberately not here
-----------------------------
No delete path, no TTL, no archival. §11.9: `audit_events` is the one place in the schema where
unbounded growth is correct; partitioning and export are Phase 2 (OQ-30). UPDATE and DELETE are
additionally impossible for the application role — revoked *and* trigger-guarded by migration
`0007` — so this module could not rewrite history even if it tried to.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.canonical import CanonicalisationError, canonical_bytes
from ..governance.primitives import mutation_primitive
from .models import AuditEvent

__all__ = [
    "ACTOR_KINDS",
    "GENESIS_PREV_HASH",
    "OUTCOMES",
    "SEMANTIC_FIELDS",
    "AuditDraft",
    "AuditWriter",
    "ChainVerification",
    "Divergence",
    "InvalidAuditDraftError",
]

#: The genesis `prev_hash`: 32 zero bytes (Appendix A.8's `ZERO32`).
#:
#: A sentinel value rather than NULL, so the column is `NOT NULL` and every row — including the
#: first — is hashed by exactly the same expression. A nullable `prev_hash` would mean the
#: verifier carried a branch for row one, and a branch that runs once is a branch nobody tests.
GENESIS_PREV_HASH: Final[bytes] = bytes(32)

#: Who acted. Closed, because an open vocabulary makes the log unfilterable — "show me
#: everything a device did" stops working the moment one writer spells it `device`.
ACTOR_KINDS: Final[tuple[str, ...]] = ("user", "agent", "system")

#: What happened. Closed for the same reason, and extending it is a deliberate one-line edit
#: here rather than a new string at a call site. Appendix A.3's transits map onto these:
#: policy deny and blast-radius block are `denied` and `blocked`, an approval gate is `pending`,
#: an auto-approval or a successful apply is `allowed`, and an agent-reported failure is `failed`.
OUTCOMES: Final[tuple[str, ...]] = ("allowed", "denied", "blocked", "pending", "failed")

#: The fields the chain hashes, in one list, held as DATA.
#:
#: Everything the row carries EXCEPT `seq` and `hash`, which Appendix A.8 excludes — `seq` is
#: assigned by the database after the hash is computed, and a value cannot cover itself.
#: `prev_hash` is excluded too, and that exclusion is the subtle one: it enters the digest
#: through the `|| prev_hash` concatenation instead, so including it here would hash it twice
#: and make the chain's structure depend on an accident. Q-05's negative control drops the
#: concatenated term, and `verify_chain` additionally asserts each row's stored `prev_hash`
#: equals its predecessor's `hash`, so tampering with that column alone is also caught.
SEMANTIC_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "tenant_id",
    "project_id",
    "actor_user_id",
    "actor_device_id",
    "actor_kind",
    "action",
    "resource_kind",
    "resource_id",
    "reason",
    "before_state",
    "after_state",
    "outcome",
    "trace_id",
    "created_at",
)


class InvalidAuditDraftError(ValueError):
    """A draft cannot be written as an audit record.

    Raised **before** the insert, so a malformed draft aborts the caller's transaction rather
    than writing a record that says nothing. §11.9: "A required `reason` is what stops the log
    from becoming a list of verbs."
    """


@dataclass(frozen=True, slots=True)
class AuditDraft:
    """The six NFR-14 fields, plus the scoping columns §6.3 defines.

    NFR-14's six: **who** (`actor_kind` with `actor_user_id` / `actor_device_id`), **what**
    (`action` + `resource_kind` + `resource_id`), **when** (`created_at`, supplied by the
    database's clock in `append`, never by a caller), **why** (`reason`, required non-empty),
    and the **before/after** pair.

    Frozen. A draft a later stage could edit after validation would be a draft validated in one
    shape and written in another.
    """

    action: str
    resource_kind: str
    reason: str
    outcome: str
    actor_kind: str = "system"
    actor_user_id: uuid.UUID | None = None
    actor_device_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    resource_id: str | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    trace_id: str | None = None
    #: Not part of the draft's identity; assigned once per record so a caller cannot make two
    #: rows share an id by reusing a draft.
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def validate(self) -> None:
        """Refuse anything that would produce a record nobody can act on."""
        if not self.reason.strip():
            raise InvalidAuditDraftError(
                "reason is required and must be non-empty (NFR-14, §11.9): a governance transit "
                "with no stated reason is exactly the record that is useless six months later"
            )
        if not self.action.strip():
            raise InvalidAuditDraftError("action is required")
        if not self.resource_kind.strip():
            raise InvalidAuditDraftError("resource_kind is required")
        if self.actor_kind not in ACTOR_KINDS:
            raise InvalidAuditDraftError(f"actor_kind must be one of {ACTOR_KINDS}, got {self.actor_kind!r}")
        if self.outcome not in OUTCOMES:
            raise InvalidAuditDraftError(f"outcome must be one of {OUTCOMES}, got {self.outcome!r}")
        if self.actor_kind == "user" and self.actor_user_id is None:
            raise InvalidAuditDraftError("actor_kind='user' requires actor_user_id: 'who' is not optional")
        if self.actor_kind == "agent" and self.actor_device_id is None:
            raise InvalidAuditDraftError("actor_kind='agent' requires actor_device_id (§11.9)")
        if self.actor_kind == "system" and (self.actor_user_id or self.actor_device_id):
            raise InvalidAuditDraftError(
                "actor_kind='system' must carry neither actor id; a system action attributed to a "
                "person is a record that blames the wrong actor"
            )
        # The before/after pair reaches the hash, so it has to be canonicalisable. Checked here
        # rather than at hash time so the error names the draft rather than the digest.
        for name, payload in (("before_state", self.before_state), ("after_state", self.after_state)):
            if payload is None:
                continue
            if not isinstance(payload, dict):
                raise InvalidAuditDraftError(f"{name} must be a JSON object or None, got {type(payload).__name__}")
            try:
                canonical_bytes(payload)
            except CanonicalisationError as exc:
                raise InvalidAuditDraftError(f"{name} is not canonicalisable: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Divergence:
    """The first row whose recomputed hash does not match what is stored."""

    seq: int
    expected_hash: bytes
    stored_hash: bytes
    kind: str  # "hash" | "prev_hash" | "gap"
    detail: str


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The result of recomputing a chain. `ok` is a derived property, never a stored flag."""

    tenant_id: uuid.UUID | None
    from_seq: int
    rows_checked: int
    divergence: Divergence | None = None

    @property
    def ok(self) -> bool:
        return self.divergence is None


def _semantic_payload(row: AuditEvent | AuditDraft, *, created_at: datetime, event_id: uuid.UUID) -> dict[str, Any]:
    """Project a draft or a stored row onto the exact same JSON document.

    One function for both directions on purpose. Two projections — one for writing, one for
    verifying — is how a chain comes to verify against bytes the writer never produced, and the
    failure would look like tampering.

    UUIDs and timestamps become strings, because JSON has neither type and RFC 8785 canonicalises
    what it is given. The timestamp is rendered in UTC with microsecond precision, which is
    Postgres `timestamptz`'s resolution, so a value that round-trips through the database
    reproduces byte-identically.
    """
    values: dict[str, Any] = {}
    for name in SEMANTIC_FIELDS:
        if name == "created_at":
            values[name] = _render_timestamp(created_at)
            continue
        if name == "id":
            values[name] = str(event_id)
            continue
        raw = getattr(row, name)
        if isinstance(raw, uuid.UUID):
            values[name] = str(raw)
        elif isinstance(raw, datetime):
            values[name] = _render_timestamp(raw)
        else:
            values[name] = raw
    return values


def _render_timestamp(value: datetime) -> str:
    """RFC 3339 in UTC, microsecond precision, always with the `+00:00` offset.

    Explicit rather than `isoformat()` alone: `isoformat()` omits the microseconds when they are
    zero, so one row in a million would hash differently from its own re-reading.
    """
    from datetime import UTC

    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _lock_key(value: str) -> int:
    """A stable, signed 32-bit key for `pg_advisory_xact_lock(int4, int4)`.

    Derived from SHA-256 rather than Python's `hash()`, which is randomised per process by
    PYTHONHASHSEED — two workers would take different locks and the chain would fork under
    exactly the concurrency the lock exists to prevent.
    """
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


class AuditWriter:
    """Appends one immutable record, and recomputes the chain on demand.

    Holds no session and no connection: every method takes the caller's `AsyncSession`. That is
    the whole reason the transactional guarantee works — a writer with its own session could not
    join the caller's transaction, and "the audit row committed but the change set did not" is
    precisely the state Q-04 exists to make impossible.
    """

    def __init__(self, *, advisory_lock_key: str = "forgeops-audit") -> None:
        if not advisory_lock_key:
            raise ValueError("advisory_lock_key must be non-empty; an empty key would collide with every other lock")
        self._lock_namespace = _lock_key(advisory_lock_key)
        self.advisory_lock_key = advisory_lock_key

    @mutation_primitive
    async def append(self, session: AsyncSession, draft: AuditDraft) -> AuditEvent:
        """Append one chained record inside the caller's transaction.

        A `@mutation_primitive` (§2.2.1): it changes state outside this process, so
        `scripts/check-chokepoint.sh` requires every caller to be lexically inside
        `src/governance/` or to hold a `MutationAuthority`. It takes no authority itself, and
        that is not an oversight — `MutationAuthority.audit_seq` is the sequence number of a
        **written** record, so requiring one here would be circular: no authority could exist
        before its audit row, and no audit row could be written without an authority.
        """
        draft.validate()

        # One round-trip for both: the lock and the timestamp. `clock_timestamp()` rather than
        # `now()`, because `now()` is transaction start time and two records in one transit
        # would then share a `created_at` — and the DATABASE's clock rather than the app's, so
        # two API replicas cannot disagree about the order of their own records.
        locked = await session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :tenant_key), clock_timestamp() AS ts"),
            {"ns": self._lock_namespace, "tenant_key": _lock_key(str(draft.tenant_id))},
        )
        created_at: datetime = locked.one().ts

        prev_hash = await self._tip_hash(session, draft.tenant_id)
        payload = _semantic_payload(draft, created_at=created_at, event_id=draft.event_id)
        digest = hashlib.sha256(canonical_bytes(payload) + prev_hash).digest()

        event = AuditEvent(
            id=draft.event_id,
            tenant_id=draft.tenant_id,
            project_id=draft.project_id,
            actor_user_id=draft.actor_user_id,
            actor_device_id=draft.actor_device_id,
            actor_kind=draft.actor_kind,
            action=draft.action,
            resource_kind=draft.resource_kind,
            resource_id=draft.resource_id,
            reason=draft.reason,
            before_state=draft.before_state,
            after_state=draft.after_state,
            outcome=draft.outcome,
            trace_id=draft.trace_id,
            prev_hash=prev_hash,
            hash=digest,
            created_at=created_at,
        )
        session.add(event)
        # Flushed, not committed. The flush is what assigns `seq`, which the caller needs in
        # order to mint an authority; the commit stays the caller's, which is what keeps the
        # record and the state transition atomic.
        await session.flush()
        return event

    async def _tip_hash(self, session: AsyncSession, tenant_id: uuid.UUID | None) -> bytes:
        """The current chain tip for this tenant, or the genesis value.

        `IS NOT DISTINCT FROM` rather than `=`, because `tenant_id` is nullable in Phase 1
        (D-35 defers `NOT NULL` to Phase 2) and `tenant_id = NULL` matches no row — so a plain
        equality would restart the untenanted chain at genesis on every append, and every row
        in it would verify against the wrong predecessor.
        """
        result = await session.execute(
            text(
                "SELECT hash FROM audit_events WHERE tenant_id IS NOT DISTINCT FROM :tenant ORDER BY seq DESC LIMIT 1"
            ),
            {"tenant": tenant_id},
        )
        row = result.first()
        return GENESIS_PREV_HASH if row is None else bytes(row[0])

    async def verify_chain(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID | None,
        since_seq: int = 0,
    ) -> ChainVerification:
        """Recompute every hash from `since_seq` and report the FIRST divergence.

        The first, not all of them: after one row diverges every later hash is wrong by
        construction, so a list would be one real finding followed by noise. The reported `seq`
        is the row to look at.

        Exposed through `GET /api/v1/audit/verify` (§11.9), so tamper evidence is a product
        feature rather than an internal helper. A verifier only an engineer can run is a verifier
        nobody runs.
        """
        result = await session.execute(
            text(
                "SELECT seq, id, tenant_id, project_id, actor_user_id, actor_device_id, actor_kind, "
                "action, resource_kind, resource_id, reason, before_state, after_state, outcome, "
                "trace_id, prev_hash, hash, created_at "
                "FROM audit_events WHERE tenant_id IS NOT DISTINCT FROM :tenant AND seq >= :since "
                "ORDER BY seq ASC"
            ),
            {"tenant": tenant_id, "since": since_seq},
        )
        rows = result.mappings().all()

        prev = await self._hash_before(session, tenant_id, since_seq)
        checked = 0
        for row in rows:
            payload = _semantic_payload(
                _RowView(row),
                created_at=row["created_at"],
                event_id=row["id"],
            )
            expected = hashlib.sha256(canonical_bytes(payload) + prev).digest()
            stored_prev = bytes(row["prev_hash"])
            stored = bytes(row["hash"])

            # Checked before the hash, because it localises the tamper. A rewritten `prev_hash`
            # with a recomputed `hash` would otherwise verify: the chain would be internally
            # consistent and no longer describe the history it came from.
            if stored_prev != prev:
                return ChainVerification(
                    tenant_id=tenant_id,
                    from_seq=since_seq,
                    rows_checked=checked,
                    divergence=Divergence(
                        seq=int(row["seq"]),
                        expected_hash=prev,
                        stored_hash=stored_prev,
                        kind="prev_hash",
                        detail="the row's prev_hash does not equal its predecessor's hash",
                    ),
                )
            if expected != stored:
                return ChainVerification(
                    tenant_id=tenant_id,
                    from_seq=since_seq,
                    rows_checked=checked,
                    divergence=Divergence(
                        seq=int(row["seq"]),
                        expected_hash=expected,
                        stored_hash=stored,
                        kind="hash",
                        detail="the row's semantic fields do not reproduce its stored hash",
                    ),
                )
            prev = stored
            checked += 1

        return ChainVerification(tenant_id=tenant_id, from_seq=since_seq, rows_checked=checked)

    async def _hash_before(self, session: AsyncSession, tenant_id: uuid.UUID | None, since_seq: int) -> bytes:
        """The hash the row at `since_seq` must chain from.

        Appendix A.8: `prev ← (from_seq = 0) ? ZERO32 : HashAt(from_seq − 1)`. Verifying from an
        arbitrary point is what makes the chain checkable on a large table without reading all of
        it, and it is only sound because the predecessor's hash is read from the database rather
        than assumed.
        """
        if since_seq <= 0:
            return GENESIS_PREV_HASH
        result = await session.execute(
            text(
                "SELECT hash FROM audit_events WHERE tenant_id IS NOT DISTINCT FROM :tenant "
                "AND seq < :since ORDER BY seq DESC LIMIT 1"
            ),
            {"tenant": tenant_id, "since": since_seq},
        )
        row = result.first()
        return GENESIS_PREV_HASH if row is None else bytes(row[0])


class _RowView:
    """Attribute access over a mapping, so `_semantic_payload` serves rows and drafts alike."""

    __slots__ = ("_row",)

    def __init__(self, row: Any) -> None:
        self._row = row

    def __getattr__(self, name: str) -> Any:
        try:
            return self._row[name]
        except KeyError as exc:  # pragma: no cover - a programming error, not a data case
            raise AttributeError(name) from exc
