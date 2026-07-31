# SPDX-License-Identifier: FSL-1.1-ALv2
"""The chokepoint's policy seam (design.md §2.2 stage 1, §5.5, §11.6, §11.7, D-25 lineage).

Why the seam exists here and the client does not
------------------------------------------------
Appendix A.3's stage 1 is `decision ← OPA.Evaluate(GovernanceInput(...))`. The concrete
client — `OpaGovernancePolicy`, querying the governance bundle over the shared `httpx`
client and persisting a `policy_evaluations` row per decision — is leaf 9.2's work, and it
cannot land earlier because the bundle it would query (`policies/agent/governance.rego`)
is authored by leaf 9.1. What *can* land now, and must, is the contract and the two
failure translations, because those are properties of the chokepoint rather than of the
client:

* an **undefined** document is `governance-policy-undefined` (503) and never a deny;
* an **unavailable** engine is a deny (§2.2, §11.6: "fail closed — an OPA outage denies").

Both translations live in `GovernanceChokepoint`, not in the client, on purpose. Put them
in the client and each future client re-implements them, and one of them eventually reads
an outage as an allow; put them in the chokepoint and the seam only has to *report* what
happened.

Why "undefined" is not a deny
-----------------------------
This is D-25's lesson carried forward. An undefined OPA document means the query named a
rule that does not exist — a deployment error, not a decision. Reading it as a deny makes
a broken bundle indistinguishable from a working one that refuses everything, and the
operator gets 403s that look like policy working correctly. 503 says "this cannot be
decided", which is a different instruction to the caller and to the on-call engineer.

The default implementation refuses rather than allows
-----------------------------------------------------
`UnavailableGovernancePolicy` is what `create_app()` composes until 9.2 replaces it, and
it raises `PolicySourceUnavailableError` on every call — so with no policy engine wired,
every mutation transit denies and writes an audit record saying why. A permissive default
would have been the alternative and is precisely the shape §9's convention forbids: "if
the failure could cause a wrong file to be written, it must refuse."
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, runtime_checkable

__all__ = [
    "POLICY_RESULTS",
    "GovernanceDecision",
    "GovernancePolicySource",
    "PolicyDocumentUndefinedError",
    "PolicySourceError",
    "PolicySourceUnavailableError",
    "UnavailableGovernancePolicy",
]

#: The closed decision vocabulary. `require_approval` is a first-class result rather than
#: "allow plus a flag", because the approval gate consumes it as an input (A.3 stage 2:
#: `gate = REQUIRES_APPROVAL OR decision.result = REQUIRE_APPROVAL`) and a boolean beside
#: an allow is one refactor away from being dropped.
POLICY_RESULTS: Final[tuple[str, ...]] = ("allow", "deny", "require_approval")

PolicyResult = Literal["allow", "deny", "require_approval"]


class PolicySourceError(Exception):
    """Base for every way a policy evaluation can fail to produce a decision."""


class PolicySourceUnavailableError(PolicySourceError):
    """The engine could not be reached, or answered unintelligibly.

    A **sibling** of `PolicyDocumentUndefinedError`, never its parent. If one were a
    subclass of the other, the chokepoint's two `except` clauses would collapse into
    whichever came first and one of the two translations would silently stop happening —
    which is the kind of bug that only shows up during an outage.
    """


class PolicyDocumentUndefinedError(PolicySourceError):
    """The queried document is undefined: `governance-policy-undefined` (503)."""


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    """One evaluation's outcome.

    `reason` is required and reaches the audit record, because `audit_events.reason` is
    non-empty by contract (NFR-14, §11.9) and a policy decision with no stated reason is
    the exact record that is useless six months later.

    `rule_id` is optional and names the rule that decided, for FR-37. Optional rather than
    required because a future engine may not be able to attribute a decision, and a
    mandatory field that gets filled with `"unknown"` is worse than an absent one.
    """

    result: PolicyResult
    reason: str
    rule_id: str | None = None

    def __post_init__(self) -> None:
        if self.result not in POLICY_RESULTS:
            raise ValueError(f"policy result must be one of {POLICY_RESULTS}, got {self.result!r}")
        if not self.reason.strip():
            raise ValueError("a governance decision must carry a non-empty reason (NFR-14, §11.9)")


@runtime_checkable
class GovernancePolicySource(Protocol):
    """Evaluate the governance bundle against one transit's input.

    Raises `PolicyDocumentUndefinedError` when the queried document is undefined, and
    `PolicySourceUnavailableError` when the engine cannot be reached. Returning a deny for
    either would move both translations into every implementation.
    """

    async def evaluate(self, *, payload: Mapping[str, Any]) -> GovernanceDecision: ...


class UnavailableGovernancePolicy:
    """The composed default until leaf 9.2 lands: every evaluation is an outage.

    Not a stub in the sense §1.3 warns about — it has real, correct behaviour, and the
    behaviour is the honest one for a deployment with no governance bundle. The chokepoint
    turns it into a deny with an audit record, so a Phase 1 backend at this wave refuses
    every mutation and says exactly why, rather than allowing one because nothing objected.
    """

    #: Named so the chokepoint's audit reason can quote it and the wiring test can assert
    #: that the composed source is this one rather than something permissive.
    DETAIL: Final[str] = (
        "no governance policy engine is composed; leaf 9.2 wires OpaGovernancePolicy "
        "(design §11.7). Until then every mutation transit fails closed."
    )

    async def evaluate(self, *, payload: Mapping[str, Any]) -> GovernanceDecision:
        raise PolicySourceUnavailableError(self.DETAIL)
