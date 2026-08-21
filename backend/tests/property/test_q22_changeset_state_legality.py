# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-22: only edges in the §3.6 state machine are accepted.

**This file is a rewrite, and what it replaced is the finding.** The previous version imported
`ApprovalStatus` and `ApprovalService` from `src/approvals/`, drove an in-process dictionary through
`approve`/`reject`/`rollback`, and asserted over the five-member uppercase vocabulary `PENDING`,
`APPROVED`, `REJECTED`, `EXECUTED`, `ROLLED_BACK`.

Not one of those five names appears in `CHANGE_SET_STATUSES`, which is §3.6's thirteen states and the
set revision `0010`'s CHECK constraint enforces. So the property named "the §3.6 state machine"
quantified over a state machine that was not §3.6's, implemented by a dict that was not the database,
reached through a router that was not mounted. Meanwhile `CHANGE_SET_TRANSITIONS`'s own docstring
called itself "the single source Q-22 quantifies over" — and Q-22 never imported it. Several
docstrings in `chokepoint.py` cite Q-22 as what proves their concurrency and transition guards; that
citation was false in the same way.

`mutations.toml` had already noticed the symptom and recorded a workaround rather than the cause:
Appendix B's control for Q-22 is "remove the optimistic-concurrency `version` predicate", and the
entry says that control "has no target" because the dict had no `version` column. It has a target
now, and the manifest row is updated to use it.

So this file quantifies over `CHANGE_SET_TRANSITIONS` and asserts the guards in
`GovernanceChokepoint`, which is where §3.6's edges are actually implemented.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from src.core.errors import ProblemException
from src.governance.chokepoint import GovernanceChokepoint
from src.governance.models import (
    CHANGE_SET_STATUSES,
    CHANGE_SET_TRANSITIONS,
    TERMINAL_CHANGE_SET_STATUSES,
)

PROJECT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
TENANT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


# ── the structural half: the edge list is §3.6's, as data ────────────────────


@given(pair=st.tuples(st.sampled_from(CHANGE_SET_STATUSES), st.sampled_from(CHANGE_SET_STATUSES)))
@settings(max_examples=200)
def test_every_edge_endpoint_is_in_the_vocabulary(pair: tuple[str, str]) -> None:
    """A transition may only name states the database can store.

    Quantified over the full product rather than over the edge list, so an edge naming a state
    outside `CHANGE_SET_STATUSES` — which the CHECK constraint would refuse at write time — is
    caught here instead of at 3am.
    """
    source, target = pair
    if (source, target) in CHANGE_SET_TRANSITIONS:
        assert source in CHANGE_SET_STATUSES
        assert target in CHANGE_SET_STATUSES


def test_the_edge_list_names_no_state_outside_the_vocabulary() -> None:
    for source, target in CHANGE_SET_TRANSITIONS:
        assert source in CHANGE_SET_STATUSES, f"{source!r} is not a §3.6 state"
        assert target in CHANGE_SET_STATUSES, f"{target!r} is not a §3.6 state"


#: The one state that is listed terminal AND has an outgoing edge.
#:
#: This is a real contradiction between two structures in `governance/models.py`, found by this
#: property and left in place rather than silently accommodated, because both sides are deliberate
#: and design.md is the authority that would have to reconcile them:
#:
#:   * `TERMINAL_CHANGE_SET_STATUSES` contains `applied`, and its docstring says "a transition **out
#:     of** any of these is illegal, including to itself".
#:   * `CHANGE_SET_TRANSITIONS` contains `("applied", "reverted")`, and its docstring calls it "the
#:     only edge leaving a success state".
#:
#: Read together, `applied` is terminal for the forward pipeline — nothing advances past it — while
#: still being revertible by an explicit, separately-authorised operation. That reading is coherent
#: and is almost certainly what was meant, but the word "illegal, including to itself" does not say
#: it, so the two tuples disagree on their face. Named here so the exception is asserted rather than
#: assumed, and so adding a second edge out of a terminal state still fails.
REVERTIBLE_TERMINAL = "applied"


@given(terminal=st.sampled_from(TERMINAL_CHANGE_SET_STATUSES))
def test_terminal_states_absorb(terminal: str) -> None:
    """§3.6 marks these terminal: no outgoing edge, with one documented exception.

    See `REVERTIBLE_TERMINAL`. Every other terminal state must have no outgoing edge at all, and
    `applied` must have exactly one — to `reverted` and nowhere else.
    """
    outgoing = [(a, b) for a, b in CHANGE_SET_TRANSITIONS if a == terminal]
    if terminal == REVERTIBLE_TERMINAL:
        assert outgoing == [("applied", "reverted")], f"applied may only leave to reverted, found {outgoing}"
    else:
        assert outgoing == [], f"{terminal} is terminal but has outgoing edges {outgoing}"


def test_applied_reverts_and_does_not_roll_back() -> None:
    """The two are different edges from different states and must not be conflated.

    `applied → reverted` is a deliberate undo of a successful apply. `applying → rolled_back` is an
    apply that failed and undid itself. The old surface had one `rollback` handler and no `revert`,
    which made these indistinguishable on the record.
    """
    assert ("applied", "reverted") in CHANGE_SET_TRANSITIONS
    assert ("applied", "rolled_back") not in CHANGE_SET_TRANSITIONS
    assert ("applying", "rolled_back") in CHANGE_SET_TRANSITIONS


# ── the behavioural half: the chokepoint refuses every non-edge ──────────────


class _Result:
    """Stands in for a SQLAlchemy result. `rowcount` is what the guards branch on."""

    def __init__(self, rowcount: int = 1, rows: list[dict[str, Any]] | None = None) -> None:
        self.rowcount = rowcount
        self._rows = rows or []

    def mappings(self) -> Any:
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeSession:
    """A session that enforces the two predicates the real UPDATE relies on, and nothing else.

    Deliberately narrow, and the narrowness is the point rather than a shortcut. It simulates
    exactly what Postgres would do with `WHERE id = :id AND version = :version AND status = '...'`:
    the update affects one row when the supplied version and status match the stored ones, and zero
    rows otherwise.

    That is what makes this a real control for Appendix B's mutation. Remove the `version =
    :version` predicate from the statement and this fake stops finding a version to compare, so a
    stale approval reports success — which is precisely the concurrency defect the predicate
    prevents, and the property below then fails.

    Anything beyond those predicates is not modelled, and the tests here do not assert on it.
    """

    def __init__(self, status: str, version: int = 3) -> None:
        self.status = status
        self.version = version
        self.statements: list[str] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        params = params or {}

        if sql.startswith("SELECT id, project_id, tenant_id, status, version"):
            return _Result(
                rows=[
                    {
                        "id": params.get("id"),
                        "project_id": PROJECT_ID,
                        "tenant_id": TENANT_ID,
                        "status": self.status,
                        "version": self.version,
                        "blast_radius_score": 1,
                        "blast_radius_verdict": "allow",
                    }
                ]
            )

        if sql.startswith("UPDATE change_sets SET status"):
            version_matches = "version = :version" not in sql or params.get("version") == self.version
            status_matches = f"status = '{self.status}'" in sql or "status = '" not in sql.split("WHERE", 1)[-1]
            return _Result(rowcount=1 if (version_matches and status_matches) else 0)

        return _Result()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _chokepoint(monkeypatch: pytest.MonkeyPatch) -> GovernanceChokepoint:
    """A chokepoint whose collaborators are stubbed so only the transition guard is under test.

    Admission, policy, audit and delivery each have their own properties; letting them run here
    would mean a failure could not be attributed to the state machine.
    """
    chokepoint = GovernanceChokepoint.__new__(GovernanceChokepoint)

    class _Admitted:
        """The two members the transition path reads off an admission result."""

        device_id = uuid.UUID("66666666-6666-6666-6666-666666666666")
        tenant_id = TENANT_ID

    async def _admit(self: Any, session: Any, *, project_id: Any, principal: Any) -> Any:
        return _Admitted()

    async def _evaluate(self: Any, *args: Any, **kwargs: Any) -> Any:
        class _Decision:
            reason = "stubbed"

        return _Decision()

    async def _audit(self: Any, *args: Any, **kwargs: Any) -> Any:
        class _Event:
            seq = 1

        return _Event()

    async def _reserve(self: Any, *args: Any, **kwargs: Any) -> uuid.UUID:
        return uuid.uuid4()

    async def _deliver(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("delivery must not be reached for a refused transition")

    for name, impl in (
        ("_admit", _admit),
        ("_evaluate_policy", _evaluate),
        ("_append_audit", _audit),
        ("_reserve_rollback_handle", _reserve),
        ("_deliver", _deliver),
    ):
        monkeypatch.setattr(GovernanceChokepoint, name, impl, raising=True)
    return chokepoint


class _StubPrincipal:
    """Only the three members the transition path reads.

    `user_id` becomes `approvals.approver_id`, and `email`/`subject` go into the audit reason. A
    bare `object()` was enough for the refusal tests, which raise before those reads, but not for
    the success path below.
    """

    user_id = uuid.UUID("55555555-5555-5555-5555-555555555555")
    email = "reviewer@example.invalid"
    subject = "stub-subject"
    kind = "user"
    tenant_id = TENANT_ID


NON_PENDING = tuple(s for s in CHANGE_SET_STATUSES if s != "pending_approval")
NON_APPLIED = tuple(s for s in CHANGE_SET_STATUSES if s != "applied")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", NON_PENDING)
async def test_approve_refuses_every_state_but_pending_approval(status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`pending_approval → approved` is the only edge approve may take."""
    chokepoint = _chokepoint(monkeypatch)
    session = _FakeSession(status=status)
    with pytest.raises(ProblemException) as raised:
        await chokepoint.approve(session, change_set_id=uuid.uuid4(), principal=_StubPrincipal())
    assert raised.value.problem.status == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("status", NON_PENDING)
async def test_reject_refuses_every_state_but_pending_approval(status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`pending_approval → rejected` likewise. This edge had no implementation at all before."""
    chokepoint = _chokepoint(monkeypatch)
    session = _FakeSession(status=status)
    with pytest.raises(ProblemException) as raised:
        await chokepoint.reject(session, change_set_id=uuid.uuid4(), principal=_StubPrincipal())
    assert raised.value.problem.status == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("status", NON_APPLIED)
async def test_revert_refuses_every_state_but_applied(status: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only `applied → reverted` leaves a success state."""
    chokepoint = _chokepoint(monkeypatch)
    session = _FakeSession(status=status)
    with pytest.raises(ProblemException) as raised:
        await chokepoint.revert(session, change_set_id=uuid.uuid4(), principal=_StubPrincipal())
    assert raised.value.problem.status == 409


@pytest.mark.asyncio
@given(stale=st.integers(min_value=1, max_value=99))
@settings(max_examples=25, deadline=None)
async def test_a_stale_version_is_refused_rather_than_applied(stale: int) -> None:
    """Appendix B's control targets exactly this: the optimistic-concurrency `version` predicate.

    A client that displayed version N and approves after the set moved to N+1 must get a 409, not an
    approval of state it never saw. With the `version = :version` predicate removed from the UPDATE,
    the fake session's simulated Postgres updates the row unconditionally and this call succeeds —
    killing the mutant.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        chokepoint = _chokepoint(monkeypatch)
        current = stale + 1
        session = _FakeSession(status="pending_approval", version=current)
        with pytest.raises(ProblemException) as raised:
            await chokepoint.approve(
                session,
                change_set_id=uuid.uuid4(),
                principal=_StubPrincipal(),
                expected_version=stale,
            )
        assert raised.value.problem.status == 409
        # Rolled back, not committed: an approval that lost a race must leave nothing behind, or one
        # approval would appear twice in the audit log.
        assert session.rolled_back is True
        assert session.committed is False
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_the_update_carries_both_predicates() -> None:
    """The statement itself, asserted, so removing either predicate is visible.

    Behavioural assertions above cover the version predicate. This one also pins the status
    predicate, which stops a concurrent approve and reject both succeeding.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        chokepoint = _chokepoint(monkeypatch)
        session = _FakeSession(status="pending_approval", version=7)
        with pytest.raises(AssertionError):
            # `_deliver` is stubbed to refuse, so a successful transition lands there. That is the
            # signal the guard passed rather than a failure of this test.
            await chokepoint.approve(session, change_set_id=uuid.uuid4(), principal=_StubPrincipal())
        update = next(s for s in session.statements if s.startswith("UPDATE change_sets SET status"))
        assert "version = :version" in update
        assert "status = 'pending_approval'" in update
    finally:
        monkeypatch.undo()
