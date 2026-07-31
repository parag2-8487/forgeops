# SPDX-License-Identifier: FSL-1.1-ALv2
"""The governance chokepoint (design.md §2.2, §5.4, §11.6, Appendix A.3; Q-03, Q-04, Q-22).

The single enforced path from an intent to a change on a user's disk. Six ordered stages, then
a mint. Every stage is an existing, tested component where one exists; the value this module
adds is that they cannot be skipped and cannot be reordered — the order is a literal in one
method, not a convention spread across callers.

The order, and why it is not the order the numbers suggest
----------------------------------------------------------
Appendix A.3 numbers the stages 0–6 but executes 3 and 4 **before** 2, and that is deliberate
rather than a slip in the pseudocode: the approval gate's input *is* the blast radius, and the
blast radius is computed from the compiled change set. So the executed order is

    0 admit → 1 policy → [ 3 compile → 4 blast radius → 2 approval gate → 5 audit → 6 handle ]

with the bracketed five inside one transaction. `_transit` below is straight-line for the
reason A.3 gives: "a loop here would be a place to skip a stage."

Exactly one mint, and it is reachable from exactly one function
--------------------------------------------------------------
`mint_authority` is called from `_mint_and_sign` and from nowhere else in the codebase.
`submit`, `approve` and `revert` all reach a mint through that one function, so "what must be
true before an authority exists" is answered by reading one 30-line method rather than by
tracing three.

Why the digest is computed before the mint, inverting A.3's last two lines
-------------------------------------------------------------------------
A.3 writes `authority ← MintAuthority(...)` then `envelope ← SignCommand(authority, ...)`, but
`MutationAuthority.envelope_digest` names the envelope. A frozen authority cannot learn its
digest afterwards, and a digest computed after the mint could disagree with the bytes that were
actually signed. So `_mint_and_sign` composes the envelope, digests it, mints the authority over
that digest, and only then signs. The stage ordering A.3 cares about is untouched: the mint
still happens after all six stages and before any signature exists.

Every early return writes exactly one audit record
--------------------------------------------------
A.3's *pseudocode* calls `AuditDenied` on two of its five early returns. Its **postcondition**
says "every early return writes exactly one audit record", and §11.6 says "a denial is as
auditable as an approval — an audit trail with only successes in it is a marketing artifact".
The postcondition wins: the three paths A.3's body is silent about (unauthenticated, no device,
revoked device) write one record here too. Q-04 quantifies over all six transit kinds.

Because those records are the *only* effect of a refused transit, `_refuse` commits before it
raises. Raising first would roll the record back and leave the denial invisible, which is the
one outcome this whole module exists to prevent.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..analysis.plan_analyzer.approval import ApprovalDecision, ApprovalGate
from ..analysis.plan_analyzer.models import PlanDocument, StageContext
from ..analysis.plan_analyzer.semantic import SemanticPlanAnalyzer
from ..audit.writer import AuditDraft, AuditWriter
from ..auth.devices import envelope_key
from ..auth.principal import Principal
from ..core.errors import ProblemException, forbidden_problem, problem
from .authority import MutationAuthority, mint_authority
from .envelope import (
    CommandEnvelope,
    PolicyContextPayload,
    envelope_digest,
    sign_envelope,
    signing_key_scope,
)
from .models import CHANGE_ITEM_ACTIONS, CHANGE_SET_ORIGINS
from .policy import (
    GovernanceDecision,
    GovernancePolicySource,
    PolicyDocumentUndefinedError,
    PolicySourceUnavailableError,
)
from .sequencing import EnvelopeSequencer, generate_nonce

__all__ = [
    "APPLY_OPERATION",
    "REVERT_OPERATION",
    "ROLLBACK_HANDLE_TTL",
    "ChangeItemRequest",
    "CommandSink",
    "GovernanceAction",
    "GovernanceChokepoint",
    "MutationRequest",
    "SignedCommand",
    "Submission",
    "UnavailableCommandSink",
    "plan_from_change_items",
]

#: §7.7's two mutating operations. Literals here rather than free-form strings at call sites,
#: because the catalogue is closed and an unknown operation must be a `KeyError` in this module
#: rather than an `operation-unknown` the agent discovers after a signature has been minted.
APPLY_OPERATION: Final[str] = "changeset.apply"
REVERT_OPERATION: Final[str] = "changeset.revert"

#: How long a reserved rollback handle stays usable.
#:
#: DATA, not configuration, and the distinction is load-bearing. Criterion 6 requires that an
#: applied change set can be reverted; a deployment that could set this to zero would satisfy
#: every test in the suite while silently shipping a product with no rollback. Thirty days is
#: long enough that a revert is a decision rather than a race, and short enough that
#: `rollback_handles` does not become an unbounded second copy of every file the platform ever
#: touched.
ROLLBACK_HANDLE_TTL: Final[timedelta] = timedelta(days=30)

#: The synthetic resource type `plan_from_change_items` renders each change item as (D-65).
#:
#: `SemanticPlanAnalyzer.classify_resource` does not know it, so every file item classifies as
#: `unknown` and takes that class's multiplier. That is the honest outcome: a file change set has
#: no cloud resources, so its blast radius is a function of how many files change and how
#: destructively, not of a resource class it does not have.
FILE_RESOURCE_TYPE: Final[str] = "forgeops_file"


class GovernanceAction(StrEnum):
    """The closed set of `audit_events.action` values a transit can write.

    Closed for the reason §11.9 closes `ACTOR_KINDS` and `OUTCOMES`: an open vocabulary makes
    the log unfilterable, and "show me every mutation this project refused" stops working the
    moment one call site spells an action differently.
    """

    #: Admission refused the transit: no principal, no device, a revoked device, a stale bundle.
    MUTATION_REFUSED = "mutation_refused"
    #: Stage 1 produced a deny, or the engine was unreachable and the chokepoint failed closed.
    MUTATION_DENIED = "mutation_denied"
    #: Stage 1's document was undefined. Not a decision — a deployment fault (503).
    POLICY_UNDEFINED = "policy_undefined"
    #: Stage 4 blocked.
    CHANGE_SET_BLOCKED = "change_set_blocked"
    #: Stage 2 requires a human.
    APPROVAL_REQUIRED = "approval_required"
    #: All six stages passed with no human needed.
    CHANGE_SET_AUTO_APPROVED = "change_set_auto_approved"
    #: A human approved a pending change set.
    CHANGE_SET_APPROVED = "change_set_approved"
    #: A revert was authorised; the reverse change set is named in `after_state`.
    CHANGE_SET_REVERT_AUTHORISED = "change_set_revert_authorised"


@dataclass(frozen=True, slots=True)
class ChangeItemRequest:
    """One file's intended change, before it is compiled into a `change_items` row.

    `old_content` is the **pre-image the intent was built against**, not a hint. Its SHA-256
    becomes `change_items.old_hash`, and that column is what lets the agent refuse a stale apply
    (§6.3): the agent recomputes the hash of the file it is about to write and aborts the whole
    set if it disagrees. An intent that does not know what it is overwriting cannot be applied
    safely, which is why `update` and `delete` require it below.
    """

    file_path: str
    action: str
    old_content: str | None = None
    new_content: str | None = None

    def __post_init__(self) -> None:
        if not self.file_path.strip():
            raise ValueError("a change item must name a file path")
        if self.action not in CHANGE_ITEM_ACTIONS:
            raise ValueError(f"action must be one of {CHANGE_ITEM_ACTIONS}, got {self.action!r}")
        if self.action in ("create", "update") and self.new_content is None:
            raise ValueError(f"{self.action} of {self.file_path!r} must carry new_content")
        if self.action in ("update", "delete") and self.old_content is None:
            raise ValueError(
                f"{self.action} of {self.file_path!r} must carry old_content: change_items.old_hash "
                "is what lets the agent refuse a stale apply (§6.3)"
            )
        if self.action == "delete" and self.new_content is not None:
            raise ValueError(f"delete of {self.file_path!r} must not carry new_content")


@dataclass(frozen=True, slots=True)
class MutationRequest:
    """The ONLY input type to the chokepoint (§2.2's diagram).

    One type rather than one method per caller, so the generation pipeline, the Approval Center
    and a future Phase 2 deployer cannot each grow their own entry with its own subset of the
    stages. `reason` is required and non-empty because it becomes `audit_events.reason`.
    """

    project_id: uuid.UUID
    items: tuple[ChangeItemRequest, ...]
    reason: str
    origin: str = "manual"
    generation_run_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("a mutation request must carry at least one change item")
        if self.origin not in CHANGE_SET_ORIGINS:
            raise ValueError(f"origin must be one of {CHANGE_SET_ORIGINS}, got {self.origin!r}")
        if not self.reason.strip():
            raise ValueError("a mutation request must state a reason (NFR-14, §11.9)")
        seen = [item.file_path for item in self.items]
        if len(set(seen)) != len(seen):
            raise ValueError("a change set must not name the same file twice; the apply order would decide the result")


@dataclass(frozen=True, slots=True)
class SignedCommand:
    """A minted envelope and its signature. Produced on exactly one path.

    Carries the wire mapping rather than the `CommandEnvelope`, because the signature covers
    canonical bytes of that mapping and a consumer that re-derived the mapping from the dataclass
    could differ from what was signed.
    """

    envelope: Mapping[str, Any]
    signature: str
    digest: str
    device_id: uuid.UUID

    def as_wire(self) -> dict[str, Any]:
        """The envelope with `signature` present — the form the hub sends."""
        return {**dict(self.envelope), "signature": self.signature}


@dataclass(frozen=True, slots=True)
class Submission:
    """What a transit produced.

    `command is None` is the assertion the integration tests make per path: no envelope exists
    for a denied, blocked or pending change set. It is a field rather than a derived property so
    an absent envelope is a value a caller must handle, not an attribute it may forget to read.
    """

    change_set_id: uuid.UUID | None
    status: str
    outcome: str
    audit_seq: int
    approval_id: uuid.UUID | None = None
    blast_radius_score: int = 0
    blast_radius_verdict: str = ""
    command: SignedCommand | None = None
    reverse_change_set_id: uuid.UUID | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class CommandSink(Protocol):
    """Where a minted envelope goes. Implemented by `websocket.hub` in leaf 8.4.

    Named `send_command` to match §2.2.1's banned-api entry for `src.websocket.hub.send_command`
    exactly: the ban and the seam have to agree on the spelling, or the ban names a function
    nobody calls.
    """

    async def send_command(self, *, device_id: uuid.UUID, command: SignedCommand) -> None: ...


class UnavailableCommandSink:
    """The composed default until leaf 8.4 builds the hub: delivery always refuses.

    Raises `device-not-connected` (409) rather than discarding the envelope. A sink that
    silently dropped commands would let every transit report success while nothing ever ran,
    and the change set would sit in `approved` with no explanation on the record.
    """

    async def send_command(self, *, device_id: uuid.UUID, command: SignedCommand) -> None:
        raise problem(
            "device-not-connected",
            detail=(
                "no agent WebSocket hub is composed, so a signed command cannot be delivered "
                "(design §11.10; leaf 8.4). The change set is approved and its rollback handle "
                "is reserved, so the apply can be retried once the hub exists."
            ),
        )


def plan_from_change_items(items: Sequence[ChangeItemRequest]) -> PlanDocument:
    """Render change items as the plan document stage 4's analyser reads (A.3 `PlanFrom`, D-65).

    One synthetic resource type for every item, so the analyser's class multipliers apply
    uniformly and the score is a function of the action mix and the cardinality. Deterministic
    and order-independent in the only sense that matters: `SemanticPlanAnalyzer.analyse` sums
    per-item contributions, so a permutation of `items` yields an identical `BlastRadius`.

    The rejected alternative and its cost are in D-65. In short: mapping file paths onto cloud
    resource classes would invent a class the change set does not have, and a second analyser
    for file change sets is how two blast-radius implementations come to disagree. The cost is
    that a file change set can never reach `stateful_deletions`, so protecting a specific path
    is the policy layer's job (`policies/agent/paths.rego`) rather than the analyser's.
    """
    return PlanDocument(
        raw={},
        resource_changes=[
            {
                "address": f"file.{item.file_path}",
                "type": FILE_RESOURCE_TYPE,
                "change": {"actions": [item.action]},
            }
            for item in items
        ],
    )


def _sha256_text(value: str | None) -> str | None:
    return None if value is None else hashlib.sha256(value.encode("utf-8")).hexdigest()


def _blast_radius_state(report: Any) -> dict[str, Any]:
    """The analyser's verdict as a canonicalisable audit payload.

    Explicit rather than `dataclasses.asdict`: `stateful_deletions` is a tuple, and RFC 8785 has
    no tuple — so the conversion has to happen somewhere, and doing it here means the audit hash
    covers a shape this function fixes rather than one a dataclass field order decides.
    """
    return {
        "score": int(report.score),
        "destructive_count": int(report.destructive_count),
        "affected_resources": int(report.affected_resources),
        "stateful_deletions": list(report.stateful_deletions),
        "verdict": str(report.verdict),
    }


class GovernanceChokepoint:
    """Six ordered stages, then a mint (§11.6).

    Holds no session. Every public method takes the caller's `AsyncSession`, and this class owns
    the transaction boundaries on it — because "exactly one audit record per transit, committed
    with the transit" (Q-04) is a promise about commits, and a caller that forgot to commit
    would discard a denial record without anything noticing.
    """

    def __init__(
        self,
        *,
        policy: GovernancePolicySource,
        approval_gate: ApprovalGate,
        analyzer: SemanticPlanAnalyzer,
        audit_writer: AuditWriter,
        sequencer: EnvelopeSequencer,
        sink: CommandSink,
        envelope_pepper: str,
        envelope_max_age_seconds: int = 300,
    ) -> None:
        if not envelope_pepper:
            raise ValueError(
                "GovernanceChokepoint requires a non-empty ENVELOPE_PEPPER: it is the input "
                "keying material for the envelope key-encryption key (D-62)"
            )
        if envelope_max_age_seconds < 1:
            raise ValueError("envelope_max_age_seconds must be positive; not_after is now + this value")
        self._policy = policy
        self._gate = approval_gate
        self._analyzer = analyzer
        self._audit = audit_writer
        self._sequencer = sequencer
        self._sink = sink
        self._pepper = envelope_pepper
        self._max_age = envelope_max_age_seconds

    # ─── public transits ──────────────────────────────────────────────────────────────────

    async def submit(self, session: AsyncSession, req: MutationRequest, *, principal: Principal) -> Submission:
        """Stages 0–6 in Appendix A.3's order, then the mint.

        Returns a `Submission` for the two non-error outcomes (approval required, applying) and
        raises a registered `ProblemException` for the four refusals, each after its audit
        record has committed.
        """
        admitted = await self._admit(session, project_id=req.project_id, principal=principal)
        decision = await self._evaluate_policy(
            session,
            principal=principal,
            admitted=admitted,
            operation=APPLY_OPERATION,
            items=req.items,
        )

        # ── one transaction: compile, blast radius, gate, audit, handle ───────────────────
        change_set_id = uuid.uuid4()
        report = self._analyzer.analyse(plan_from_change_items(req.items))
        await session.execute(
            text(
                "INSERT INTO change_sets (id, project_id, tenant_id, status, created_by, origin, "
                "generation_run_id, blast_radius_score, blast_radius_verdict, policy_bundle_digest, version) "
                "VALUES (:id, :project, :tenant, 'validating', :created_by, :origin, :run, 0, '', :digest, 1)"
            ),
            {
                "id": change_set_id,
                "project": req.project_id,
                "tenant": admitted.tenant_id,
                "created_by": principal.user_id if principal.kind == "user" else None,
                "origin": req.origin,
                "run": req.generation_run_id,
                "digest": admitted.bundle_digest,
            },
        )
        await self._insert_change_items(session, change_set_id, req.items)
        await self._store_blast_radius(session, change_set_id, report)

        if report.verdict == "block":
            return await self._blocked(
                session,
                principal=principal,
                admitted=admitted,
                change_set_id=change_set_id,
                report=report,
                reason=f"blast radius block: score {report.score}, {report.destructive_count} destructive change(s)",
            )

        gate = await self._gate.submit(report, StageContext())
        if gate == ApprovalDecision.BLOCKED:
            # Reachable even though stage 4 already returned for `verdict == "block"`: the gate
            # is a seam, and a Phase 1 replacement may block what the analyser only warned
            # about. Treating an unexpected BLOCKED as "carry on" would let a gate that refuses
            # be ignored, which is the one failure a gate cannot tolerate.
            return await self._blocked(
                session,
                principal=principal,
                admitted=admitted,
                change_set_id=change_set_id,
                report=report,
                reason="the approval gate blocked this change set",
            )

        if gate == ApprovalDecision.REQUIRES_APPROVAL or decision.result == "require_approval":
            await self._set_status(session, change_set_id, "pending_approval")
            event = await self._append_audit(
                session,
                principal=principal,
                admitted=admitted,
                action=GovernanceAction.APPROVAL_REQUIRED,
                outcome="pending",
                resource_kind="change_set",
                resource_id=str(change_set_id),
                reason=(f"human approval required: gate={gate.value}, policy={decision.result}; {decision.reason}"),
                after_state=_blast_radius_state(report),
            )
            await session.commit()
            return Submission(
                change_set_id=change_set_id,
                status="pending_approval",
                outcome="approval-required",
                audit_seq=int(event.seq),
                blast_radius_score=report.score,
                blast_radius_verdict=report.verdict,
                command=None,
            )

        await self._set_status(session, change_set_id, "approved")
        event = await self._append_audit(
            session,
            principal=principal,
            admitted=admitted,
            action=GovernanceAction.CHANGE_SET_AUTO_APPROVED,
            outcome="allowed",
            resource_kind="change_set",
            resource_id=str(change_set_id),
            reason=f"auto-approved: {decision.reason}",
            after_state=_blast_radius_state(report),
        )
        # Stage 6 — reserved BEFORE any envelope exists, so a crash between mint and apply
        # cannot leave an irreversible change (A.3's postcondition).
        await self._reserve_rollback_handle(session, change_set_id, admitted.device_id)
        await session.commit()

        # D-64: the auto-approved path has no `approvals` row, because nobody approved it. The
        # audit record that recorded the auto-approval is the authorising artifact, so its id is
        # the `approval_id`.
        return await self._deliver(
            session,
            change_set_id=change_set_id,
            admitted=admitted,
            approval_id=event.id,
            audit_seq=int(event.seq),
            decision=decision,
            report=report,
            operation=APPLY_OPERATION,
            args={"change_set_id": str(change_set_id), "version": 1, "item_count": len(req.items)},
            status_after_delivery="applying",
            outcome="applying",
        )

    async def approve(
        self,
        session: AsyncSession,
        *,
        change_set_id: uuid.UUID,
        principal: Principal,
        comment: str | None = None,
        expected_version: int | None = None,
    ) -> Submission:
        """Human approval → authority mint → signed envelope → hub (§11.6).

        Optimistic concurrency on `change_sets.version`: the transition is a single `UPDATE`
        predicated on the version read in this transaction, so two concurrent approvals produce
        exactly one winner and one `409 change-set-conflict` (Q-22). `expected_version` lets an
        API caller supply the version it displayed, which turns a stale browser tab into the same
        409 rather than an approval of a change set that moved on.

        Admission and policy run **again**. A change set approved an hour after its device was
        revoked, or after the bundle moved, must not be applied — and re-evaluating is the only
        way to know. A policy deny here leaves the change set `pending_approval` rather than
        transitioning it: §3.6 has no `pending_approval → rejected_by_policy` edge, and the set
        is genuinely still pending, since a policy change could allow it later.
        """
        row = await self._load_change_set(session, change_set_id)
        if row["status"] != "pending_approval":
            raise problem(
                "change-set-conflict",
                detail=f"change set {change_set_id} is {row['status']}, not pending_approval",
            )
        version = int(row["version"]) if expected_version is None else int(expected_version)

        admitted = await self._admit(session, project_id=row["project_id"], principal=principal)
        decision = await self._evaluate_policy(
            session,
            principal=principal,
            admitted=admitted,
            operation=APPLY_OPERATION,
            items=(),
            change_set_id=change_set_id,
        )

        updated = await session.execute(
            text(
                "UPDATE change_sets SET status = 'approved', version = version + 1 "
                "WHERE id = :id AND version = :version AND status = 'pending_approval'"
            ),
            {"id": change_set_id, "version": version},
        )
        if updated.rowcount != 1:
            # Rolled back rather than committed: nothing happened, and an audit record for a
            # transit that lost a race would make one approval look like two.
            await session.rollback()
            raise problem(
                "change-set-conflict",
                detail=f"change set {change_set_id} was modified concurrently; re-read it and retry",
            )

        approval_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO approvals (id, change_set_id, approver_id, status, comment) "
                "VALUES (:id, :cs, :approver, 'approved', :comment)"
            ),
            {"id": approval_id, "cs": change_set_id, "approver": principal.user_id, "comment": comment},
        )
        event = await self._append_audit(
            session,
            principal=principal,
            admitted=admitted,
            action=GovernanceAction.CHANGE_SET_APPROVED,
            outcome="allowed",
            resource_kind="change_set",
            resource_id=str(change_set_id),
            reason=f"approved by {principal.email or principal.subject}: {decision.reason}",
            before_state={"status": "pending_approval", "version": version},
            after_state={"status": "approved", "version": version + 1, "approval_id": str(approval_id)},
        )
        await self._reserve_rollback_handle(session, change_set_id, admitted.device_id)
        await session.commit()

        return await self._deliver(
            session,
            change_set_id=change_set_id,
            admitted=admitted,
            approval_id=approval_id,
            audit_seq=int(event.seq),
            decision=decision,
            report=None,
            operation=APPLY_OPERATION,
            args={"change_set_id": str(change_set_id), "version": version + 1, "approval_id": str(approval_id)},
            status_after_delivery="applying",
            outcome="applying",
            blast_radius_score=int(row["blast_radius_score"]),
            blast_radius_verdict=str(row["blast_radius_verdict"]),
        )

    async def revert(self, session: AsyncSession, *, change_set_id: uuid.UUID, principal: Principal) -> Submission:
        """A revert is a mutation: the full chokepoint again, and its own authority (§11.6, D-66).

        Stage 3 compiles the **reverse** change set — every item inverted — and stages 1, 2, 4, 5
        and 6 run over it. The original moves `applied → reverted` when the reverse set has been
        applied and the handle consumed, which is exactly §3.6's label for that edge; the result
        handler that writes it arrives with the hub in group 8, so in this wave a reverted
        original stays `applied` after its reverse set is minted. That gap is named in D-66
        rather than papered over by marking a revert that has not happened.

        Reusing the original authority would make rollback a privileged back door, so `_deliver`
        mints a fresh one over the reverse set. The original's rollback handle is consumed here,
        which is what makes a revert single-use (Q-02's clause, enforced backend-side as well as
        in the agent).
        """
        row = await self._load_change_set(session, change_set_id)
        if row["status"] != "applied":
            raise problem(
                "revert-unavailable",
                detail=f"change set {change_set_id} is {row['status']}; only an applied set can be reverted (§3.6)",
            )
        handle = await session.execute(
            text("SELECT id, consumed, expires_at FROM rollback_handles WHERE change_set_id = :cs FOR UPDATE"),
            {"cs": change_set_id},
        )
        handle_row = handle.mappings().first()
        if handle_row is None:
            raise problem("revert-unavailable", detail=f"change set {change_set_id} has no rollback handle")
        if bool(handle_row["consumed"]):
            raise problem("revert-unavailable", detail="the rollback handle for this change set is already consumed")
        if handle_row["expires_at"] <= datetime.now(UTC):
            raise problem("revert-unavailable", detail="the rollback handle for this change set has expired")

        items = await self._reverse_items(session, change_set_id)
        admitted = await self._admit(session, project_id=row["project_id"], principal=principal)
        decision = await self._evaluate_policy(
            session,
            principal=principal,
            admitted=admitted,
            operation=REVERT_OPERATION,
            items=items,
            change_set_id=change_set_id,
        )

        reverse_id = uuid.uuid4()
        report = self._analyzer.analyse(plan_from_change_items(items))
        await session.execute(
            text(
                "INSERT INTO change_sets (id, project_id, tenant_id, status, created_by, origin, "
                "blast_radius_score, blast_radius_verdict, policy_bundle_digest, version) "
                "VALUES (:id, :project, :tenant, 'validating', :created_by, 'manual', 0, '', :digest, 1)"
            ),
            {
                "id": reverse_id,
                "project": row["project_id"],
                "tenant": admitted.tenant_id,
                "created_by": principal.user_id if principal.kind == "user" else None,
                "digest": admitted.bundle_digest,
            },
        )
        await self._insert_change_items(session, reverse_id, items)
        await self._store_blast_radius(session, reverse_id, report)

        if report.verdict == "block":
            return await self._blocked(
                session,
                principal=principal,
                admitted=admitted,
                change_set_id=reverse_id,
                report=report,
                reason=f"blast radius block on the reverse of {change_set_id}: score {report.score}",
            )

        gate = await self._gate.submit(report, StageContext())
        if gate == ApprovalDecision.BLOCKED:
            return await self._blocked(
                session,
                principal=principal,
                admitted=admitted,
                change_set_id=reverse_id,
                report=report,
                reason=f"the approval gate blocked the reverse of {change_set_id}",
            )
        if gate == ApprovalDecision.REQUIRES_APPROVAL or decision.result == "require_approval":
            await self._set_status(session, reverse_id, "pending_approval")
            event = await self._append_audit(
                session,
                principal=principal,
                admitted=admitted,
                action=GovernanceAction.APPROVAL_REQUIRED,
                outcome="pending",
                resource_kind="change_set",
                resource_id=str(reverse_id),
                reason=f"human approval required to revert {change_set_id}: {decision.reason}",
                after_state={**_blast_radius_state(report), "reverts": str(change_set_id)},
            )
            await session.commit()
            return Submission(
                change_set_id=change_set_id,
                reverse_change_set_id=reverse_id,
                status="pending_approval",
                outcome="approval-required",
                audit_seq=int(event.seq),
                blast_radius_score=report.score,
                blast_radius_verdict=report.verdict,
                command=None,
            )

        await self._set_status(session, reverse_id, "approved")
        event = await self._append_audit(
            session,
            principal=principal,
            admitted=admitted,
            action=GovernanceAction.CHANGE_SET_REVERT_AUTHORISED,
            outcome="allowed",
            resource_kind="change_set",
            resource_id=str(change_set_id),
            reason=f"revert authorised: {decision.reason}",
            before_state={"status": "applied"},
            after_state={**_blast_radius_state(report), "reverse_change_set_id": str(reverse_id)},
        )
        await session.execute(
            text("UPDATE rollback_handles SET consumed = true WHERE id = :id AND consumed = false"),
            {"id": handle_row["id"]},
        )
        await self._reserve_rollback_handle(session, reverse_id, admitted.device_id)
        await session.commit()

        submission = await self._deliver(
            session,
            change_set_id=reverse_id,
            admitted=admitted,
            approval_id=event.id,
            audit_seq=int(event.seq),
            decision=decision,
            report=report,
            operation=REVERT_OPERATION,
            args={
                "change_set_id": str(reverse_id),
                "reverts_change_set_id": str(change_set_id),
                "rollback_handle_id": str(handle_row["id"]),
            },
            status_after_delivery="applying",
            outcome="reverting",
        )
        return Submission(
            change_set_id=change_set_id,
            reverse_change_set_id=reverse_id,
            status=submission.status,
            outcome=submission.outcome,
            audit_seq=submission.audit_seq,
            approval_id=submission.approval_id,
            blast_radius_score=submission.blast_radius_score,
            blast_radius_verdict=submission.blast_radius_verdict,
            command=submission.command,
        )

    # ─── stage 0 ──────────────────────────────────────────────────────────────────────────

    async def _admit(self, session: AsyncSession, *, project_id: uuid.UUID, principal: Principal) -> _Admitted:
        """Resolve principal, project, tenant and target device; refuse anything unfit.

        Four refusals, each with its own audit record and each committed before it raises. A
        missing project produces the non-disclosing 403 rather than a 404: §4.2 and Q-20 require
        a forbidden body that is byte-identical whether or not the resource exists, and a 404
        here would be an enumeration oracle for project ids.
        """
        if principal is None:  # type: ignore[unreachable]
            await self._refuse(
                session,
                principal=None,
                tenant_id=None,
                project_id=project_id,
                reason="unauthenticated: the chokepoint has no unauthenticated entry (A.3 PRE)",
                problem_type="unauthenticated",
            )
        result = await session.execute(text("SELECT id, tenant_id FROM projects WHERE id = :id"), {"id": project_id})
        project = result.mappings().first()
        if project is None:
            raise forbidden_problem()
        tenant_id = project["tenant_id"]

        device = await session.execute(
            text(
                "SELECT id, status, policy_bundle_digest FROM agent_devices "
                "WHERE project_id = :project AND status = 'active' "
                "ORDER BY last_seen DESC NULLS LAST, created_at DESC LIMIT 1"
            ),
            {"project": project_id},
        )
        device_row = device.mappings().first()
        if device_row is None:
            revoked = await session.execute(
                text("SELECT count(*) FROM agent_devices WHERE project_id = :project AND status = 'revoked'"),
                {"project": project_id},
            )
            if int(revoked.scalar() or 0) > 0:
                await self._refuse(
                    session,
                    principal=principal,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    reason="every paired device for this project is revoked",
                    problem_type="device-revoked",
                )
            await self._refuse(
                session,
                principal=principal,
                tenant_id=tenant_id,
                project_id=project_id,
                reason="no active agent device is paired to this project",
                problem_type="device-not-connected",
            )

        device_digest = device_row["policy_bundle_digest"]
        active_digest = await self._active_bundle_digest(session, project_id)
        if not device_digest or device_digest != active_digest:
            await self._refuse(
                session,
                principal=principal,
                tenant_id=tenant_id,
                project_id=project_id,
                reason=(
                    f"policy bundle stale: device pinned {device_digest or '<none>'}, "
                    f"project active {active_digest or '<none>'}"
                ),
                problem_type="policy-bundle-stale",
                device_id=device_row["id"],
            )
        return _Admitted(
            project_id=project_id,
            tenant_id=tenant_id,
            device_id=device_row["id"],
            bundle_digest=str(device_digest),
        )

    async def _active_bundle_digest(self, session: AsyncSession, project_id: uuid.UUID) -> str | None:
        """The project's active bundle digest, or the global one when the project has none.

        Read with raw SQL rather than through `src.policies.models`, which §2.2.1's banned-api
        table forbids importing from another domain. One `ORDER BY` puts the project-scoped row
        first, so "project overrides global" is a property of the query rather than of two
        round-trips whose interleaving could change the answer.
        """
        result = await session.execute(
            text(
                "SELECT digest FROM policy_bundles WHERE active AND (project_id = :project OR project_id IS NULL) "
                "ORDER BY (project_id IS NULL), created_at DESC LIMIT 1"
            ),
            {"project": project_id},
        )
        row = result.first()
        return None if row is None else str(row[0])

    # ─── stage 1 ──────────────────────────────────────────────────────────────────────────

    async def _evaluate_policy(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        admitted: _Admitted,
        operation: str,
        items: Sequence[ChangeItemRequest],
        change_set_id: uuid.UUID | None = None,
    ) -> GovernanceDecision:
        """Stage 1, with both failure translations (§2.2, §5.5, D-25 lineage).

        An **undefined** document is 503 and never a deny. An **unavailable** engine is a deny,
        because §11.6 says an OPA outage denies and §9's convention says anything that could
        cause a wrong file to be written must refuse.
        """
        payload = {
            "operation": operation,
            "project_id": str(admitted.project_id),
            "tenant_id": None if admitted.tenant_id is None else str(admitted.tenant_id),
            "device_id": str(admitted.device_id),
            "bundle_digest": admitted.bundle_digest,
            "change_set_id": None if change_set_id is None else str(change_set_id),
            "principal": {
                "kind": principal.kind,
                "role": str(principal.role),
                "blast_radius": principal.blast_radius,
                "user_id": str(principal.user_id),
            },
            "items": [{"file_path": item.file_path, "action": item.action} for item in items],
            "now": datetime.now(UTC).isoformat(),
        }
        try:
            decision = await self._policy.evaluate(payload=payload)
        except PolicyDocumentUndefinedError as exc:
            await self._refuse(
                session,
                principal=principal,
                tenant_id=admitted.tenant_id,
                project_id=admitted.project_id,
                reason=f"governance policy document undefined: {exc}",
                problem_type="governance-policy-undefined",
                action=GovernanceAction.POLICY_UNDEFINED,
                outcome="failed",
                device_id=admitted.device_id,
            )
        except PolicySourceUnavailableError as exc:
            decision = GovernanceDecision(
                result="deny",
                reason=f"policy engine unavailable; failing closed (§2.2, §11.6): {exc}",
                rule_id=None,
            )
        if decision.result == "deny":
            await self._refuse(
                session,
                principal=principal,
                tenant_id=admitted.tenant_id,
                project_id=admitted.project_id,
                reason=f"policy denied: {decision.reason}",
                problem_type="policy-denied",
                action=GovernanceAction.MUTATION_DENIED,
                device_id=admitted.device_id,
            )
        return decision

    # ─── stage 3 and 4 helpers ────────────────────────────────────────────────────────────

    async def _insert_change_items(
        self, session: AsyncSession, change_set_id: uuid.UUID, items: Sequence[ChangeItemRequest]
    ) -> None:
        """Compile `change_items`, with the pre-image hash per row (A.3 stage 3)."""
        for ordinal, item in enumerate(items):
            await session.execute(
                text(
                    "INSERT INTO change_items (id, change_set_id, file_path, action, old_content, "
                    "new_content, old_hash, new_hash, ordinal) VALUES (:id, :cs, :path, :action, "
                    ":old, :new, :old_hash, :new_hash, :ordinal)"
                ),
                {
                    "id": uuid.uuid4(),
                    "cs": change_set_id,
                    "path": item.file_path,
                    "action": item.action,
                    "old": item.old_content,
                    "new": item.new_content,
                    "old_hash": _sha256_text(item.old_content),
                    "new_hash": _sha256_text(item.new_content),
                    "ordinal": ordinal,
                },
            )

    async def _store_blast_radius(self, session: AsyncSession, change_set_id: uuid.UUID, report: Any) -> None:
        await session.execute(
            text("UPDATE change_sets SET blast_radius_score = :score, blast_radius_verdict = :verdict WHERE id = :id"),
            {"score": int(report.score), "verdict": str(report.verdict), "id": change_set_id},
        )

    async def _set_status(self, session: AsyncSession, change_set_id: uuid.UUID, status: str) -> None:
        await session.execute(
            text("UPDATE change_sets SET status = :status WHERE id = :id"),
            {"status": status, "id": change_set_id},
        )

    async def _reserve_rollback_handle(
        self, session: AsyncSession, change_set_id: uuid.UUID, device_id: uuid.UUID
    ) -> uuid.UUID:
        """Stage 6. `ON CONFLICT DO NOTHING` keeps the handle at most one per change set (Q-02).

        The manifest is empty here and is filled by the agent's `ApplyReport`. Reserving the row
        before the envelope exists is the whole point: the handle is what makes a crash between
        mint and apply recoverable, and a handle created after the apply would not exist for
        exactly the failure it is for.
        """
        handle_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO rollback_handles (id, change_set_id, backup_manifest, agent_device_id, "
                "consumed, expires_at) VALUES (:id, :cs, '{}'::jsonb, :device, false, :expires) "
                "ON CONFLICT (change_set_id) DO NOTHING"
            ),
            {
                "id": handle_id,
                "cs": change_set_id,
                "device": str(device_id),
                "expires": datetime.now(UTC) + ROLLBACK_HANDLE_TTL,
            },
        )
        return handle_id

    async def _load_change_set(self, session: AsyncSession, change_set_id: uuid.UUID) -> Mapping[str, Any]:
        result = await session.execute(
            text(
                "SELECT id, project_id, tenant_id, status, version, blast_radius_score, blast_radius_verdict "
                "FROM change_sets WHERE id = :id FOR UPDATE"
            ),
            {"id": change_set_id},
        )
        row = result.mappings().first()
        if row is None:
            raise forbidden_problem()
        return row

    async def _reverse_items(self, session: AsyncSession, change_set_id: uuid.UUID) -> tuple[ChangeItemRequest, ...]:
        """The inverse of a change set's items, in reverse ordinal order.

        A `create` inverts to a `delete`, a `delete` to a `create`, and an `update` swaps its two
        contents. Reverse ordinal order because the forward apply wrote them in ascending order,
        and undoing in the same order would delete a directory's file before restoring the file
        that shares its parent — the agent's own `CATCH` branch unwinds backwards for the same
        reason (Appendix A.9).
        """
        result = await session.execute(
            text(
                "SELECT file_path, action, old_content, new_content FROM change_items "
                "WHERE change_set_id = :cs ORDER BY ordinal DESC"
            ),
            {"cs": change_set_id},
        )
        items: list[ChangeItemRequest] = []
        for row in result.mappings().all():
            action, old, new = str(row["action"]), row["old_content"], row["new_content"]
            if action == "create":
                items.append(ChangeItemRequest(file_path=row["file_path"], action="delete", old_content=new))
            elif action == "delete":
                items.append(ChangeItemRequest(file_path=row["file_path"], action="create", new_content=old))
            else:
                items.append(
                    ChangeItemRequest(file_path=row["file_path"], action="update", old_content=new, new_content=old)
                )
        if not items:
            raise problem("revert-unavailable", detail=f"change set {change_set_id} has no items to reverse")
        return tuple(items)

    # ─── stage 5 ──────────────────────────────────────────────────────────────────────────

    async def _append_audit(
        self,
        session: AsyncSession,
        *,
        principal: Principal | None,
        admitted: _Admitted | None,
        action: GovernanceAction,
        outcome: str,
        resource_kind: str,
        resource_id: str | None,
        reason: str,
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
        tenant_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
    ) -> Any:
        """One record, in the caller's transaction. The only audit call site in this module.

        Single call site on purpose: "exactly one record per transit" is provable by counting
        calls to this method along each path, which is a property a reader can check. Six call
        sites with their own draft construction would not be.
        """
        actor_kind = "system" if principal is None else ("agent" if principal.kind == "device" else "user")
        draft = AuditDraft(
            action=str(action),
            resource_kind=resource_kind,
            resource_id=resource_id,
            reason=reason,
            outcome=outcome,
            actor_kind=actor_kind,
            actor_user_id=None if principal is None or actor_kind != "user" else principal.user_id,
            actor_device_id=(
                None
                if principal is None or actor_kind != "agent"
                else (principal.device_id or device_id or (admitted.device_id if admitted else None))
            ),
            tenant_id=admitted.tenant_id if admitted is not None else tenant_id,
            project_id=admitted.project_id if admitted is not None else project_id,
            before_state=before_state,
            after_state=after_state,
        )
        return await self._audit.append(session, draft)

    async def _refuse(
        self,
        session: AsyncSession,
        *,
        principal: Principal | None,
        tenant_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        reason: str,
        problem_type: str,
        action: GovernanceAction = GovernanceAction.MUTATION_REFUSED,
        outcome: str = "denied",
        device_id: uuid.UUID | None = None,
    ) -> None:
        """Write one audit record, commit it, then raise. Never returns.

        The commit is the load-bearing part. `AuditWriter.append` joins the caller's transaction
        and does not commit (§11.9), so raising first would roll the record back and a refused
        transit would leave no trace — the exact failure "a denial is as auditable as an
        approval" names. Committing first means the record survives the exception.
        """
        await self._append_audit(
            session,
            principal=principal,
            admitted=None,
            action=action,
            outcome=outcome,
            resource_kind="project",
            resource_id=None if project_id is None else str(project_id),
            reason=reason,
            tenant_id=tenant_id,
            project_id=project_id,
            device_id=device_id,
        )
        await session.commit()
        raise problem(problem_type, detail=reason)

    async def _blocked(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        admitted: _Admitted,
        change_set_id: uuid.UUID,
        report: Any,
        reason: str,
    ) -> Submission:
        """Stage 4's block: persist `blocked`, audit it in the same transaction, commit, raise."""
        await self._set_status(session, change_set_id, "blocked")
        await self._append_audit(
            session,
            principal=principal,
            admitted=admitted,
            action=GovernanceAction.CHANGE_SET_BLOCKED,
            outcome="blocked",
            resource_kind="change_set",
            resource_id=str(change_set_id),
            reason=reason,
            after_state=_blast_radius_state(report),
        )
        await session.commit()
        raise problem("blast-radius-blocked", detail=reason)

    # ─── the mint ─────────────────────────────────────────────────────────────────────────

    async def _deliver(
        self,
        session: AsyncSession,
        *,
        change_set_id: uuid.UUID,
        admitted: _Admitted,
        approval_id: uuid.UUID,
        audit_seq: int,
        decision: GovernanceDecision,
        report: Any,
        operation: str,
        args: Mapping[str, Any],
        status_after_delivery: str,
        outcome: str,
        blast_radius_score: int | None = None,
        blast_radius_verdict: str | None = None,
    ) -> Submission:
        """Mint, sign, send, then advance the status.

        The status advances **after** the send, not before, and writes no audit record. Delivery
        is not a transit — it is the transit's outcome leaving the building — so a second record
        here would break Q-04's "exactly one row per transit". Advancing afterwards also means a
        failed delivery leaves the set `approved` and retryable rather than stuck in `applying`
        with nothing in flight.
        """
        command = await self._mint_and_sign(
            session,
            change_set_id=change_set_id,
            admitted=admitted,
            approval_id=approval_id,
            audit_seq=audit_seq,
            decision=decision,
            report=report,
            operation=operation,
            args=args,
        )
        await self._sink.send_command(device_id=admitted.device_id, command=command)
        await self._set_status(session, change_set_id, status_after_delivery)
        await session.commit()
        return Submission(
            change_set_id=change_set_id,
            status=status_after_delivery,
            outcome=outcome,
            audit_seq=audit_seq,
            approval_id=approval_id,
            blast_radius_score=(report.score if report is not None else (blast_radius_score or 0)),
            blast_radius_verdict=(report.verdict if report is not None else (blast_radius_verdict or "")),
            command=command,
        )

    async def _mint_and_sign(
        self,
        session: AsyncSession,
        *,
        change_set_id: uuid.UUID,
        admitted: _Admitted,
        approval_id: uuid.UUID,
        audit_seq: int,
        decision: GovernanceDecision,
        report: Any,
        operation: str,
        args: Mapping[str, Any],
    ) -> SignedCommand:
        """The ONLY path to a `MutationAuthority` and the only caller of `sign_envelope`.

        Reachable from `submit`, `approve` and `revert`, and from nothing else. `check-chokepoint`
        (leaf 7.3) asserts the Python half of that mechanically; this docstring is the reason it
        is worth asserting.
        """
        if operation not in (APPLY_OPERATION, REVERT_OPERATION):
            raise ValueError(f"{operation!r} is not a mutating operation in §7.7's catalogue")

        floor = await self._last_seq(session, admitted.device_id)
        seq = await self._sequencer.next_seq(admitted.device_id, floor=floor)
        nonce = generate_nonce()
        await self._sequencer.reserve_nonce(admitted.device_id, nonce, ttl_seconds=self._max_age)
        not_after = int(datetime.now(UTC).timestamp()) + self._max_age

        envelope = CommandEnvelope(
            command_id=str(uuid.uuid4()),
            device_id=str(admitted.device_id),
            operation=operation,
            args=dict(args),
            approval_id=str(approval_id),
            policy_context=PolicyContextPayload(bundle_digest=admitted.bundle_digest, decision=decision.result),
            nonce=nonce,
            seq=seq,
            not_after=not_after,
        )
        digest = envelope_digest(envelope)

        blast_radius = _report_radius(report)
        authority: MutationAuthority = mint_authority(
            change_set_id=change_set_id,
            approval_id=approval_id,
            policy_bundle_digest=admitted.bundle_digest,
            blast_radius=blast_radius,
            audit_seq=audit_seq,
            envelope_digest=digest,
        )
        key = await envelope_key(session, device_id=admitted.device_id, pepper=self._pepper)
        with signing_key_scope(key.get_secret_value()):
            signature = sign_envelope(envelope)
        # The authority is not passed on: nothing downstream of the mint takes one yet, because
        # the hub is leaf 8.4. It is constructed here because construction IS the check — the
        # mint refuses an `audit_seq` below 1 and an empty bundle digest, so an envelope cannot be
        # signed for a transit that wrote no audit record or named no bundle.
        assert authority.envelope_digest == digest  # noqa: S101 - the mint's own postcondition
        await session.execute(
            text("UPDATE agent_devices SET last_seq = :seq WHERE id = :id AND last_seq < :seq"),
            {"seq": seq, "id": admitted.device_id},
        )
        return SignedCommand(
            envelope=envelope.as_canonical_mapping(),
            signature=signature,
            digest=digest,
            device_id=admitted.device_id,
        )

    async def _last_seq(self, session: AsyncSession, device_id: uuid.UUID) -> int:
        result = await session.execute(text("SELECT last_seq FROM agent_devices WHERE id = :id"), {"id": device_id})
        row = result.first()
        return 0 if row is None else int(row[0] or 0)


def _report_radius(report: Any) -> str:
    """Map a blast-radius verdict onto `Principal`'s three-level radius vocabulary.

    `MutationAuthority.blast_radius` is typed as `auth.principal.BlastRadius` — the
    read_only/workspace/infrastructure ladder — while the analyser produces allow/warn/block.
    The mapping is here, once, rather than at each mint: an authority that claimed
    `infrastructure` for a warn would widen what a downstream check believes it may do.

    `None` (the approve path, which does not recompute the radius) maps to `workspace`, the
    radius a paired device can actually reach (§11.2's `DEVICE_ATTESTATION_BLAST_RADIUS`).
    Nothing here can mint `infrastructure`: Phase 1 has no attestation that reaches it (§14.3),
    so a mint that claimed it would be claiming authority the platform cannot ground.
    """
    if report is None:
        return "workspace"
    return "read_only" if report.verdict == "allow" and report.affected_resources == 0 else "workspace"


@dataclass(frozen=True, slots=True)
class _Admitted:
    """What stage 0 established. Passed forward so no later stage re-reads it.

    Private, because it is the shape of one algorithm's intermediate state rather than a contract
    anything outside this module should depend on. Frozen for the same reason `Principal` is: a
    stage that could rewrite `bundle_digest` after admission checked it would make the check
    advisory.
    """

    project_id: uuid.UUID
    tenant_id: uuid.UUID | None
    device_id: uuid.UUID
    bundle_digest: str


#: Re-exported so a caller can catch what a transit raises without importing `core.errors`.
GovernanceRefusal = ProblemException
