# SPDX-License-Identifier: FSL-1.1-ALv2
"""The backend half of double evaluation: `OpaGovernancePolicy` (design.md §5.5, §11.7).

What this module is, and what it deliberately is not
----------------------------------------------------
It is the concrete `GovernancePolicySource` the chokepoint's stage 1 seam
(`src/governance/policy.py`) has been waiting for since leaf 7.4. It queries one document
— `data.forgeops.governance.decision`, the entry document leaf 9.1 authored — over the
**shared** `httpx.AsyncClient` the app factory already builds, and returns a
`GovernanceDecision`.

It is **not** where the two failure translations live. An undefined document becoming
`governance-policy-undefined` (503) and an unreachable engine becoming a deny are
properties of the chokepoint, and `src/governance/policy.py` explains at length why they
stay there: put them here and every future client re-implements them, and one of them
eventually reads an outage as an allow. This module's whole job is to report **which** of
the two happened, precisely, by raising one of two sibling exceptions.

Why a malformed `result` is reported as UNDEFINED rather than as unavailable
---------------------------------------------------------------------------
OPA answers an undefined document with HTTP 200 and a body that has no `result` key. It
answers a document that exists but returns the wrong shape with HTTP 200 and a `result`
that is not a decision. Both are **deployment** errors with the same operator instruction —
fix the bundle — and both must produce 503 rather than 403, because 403 tells the caller
"policy refused you" and sends nobody to look at the bundle. That is D-25's lesson stated
in terms of shape as well as presence: the thing that must never happen is a broken bundle
that is indistinguishable from a working one that denies.

The input mapping is a pure function, on purpose
------------------------------------------------
`governance_input` turns the chokepoint's stage-1 payload into the document the bundle
reads. It is module-level and side-effect free so that Q-06 (leaf 9.6) can generate inputs
and feed the *same* mapping to both evaluators without an HTTP server in the loop, and so
that this mapping — the place where a field can go missing — is testable on its own.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

import httpx

from ..governance.policy import (
    POLICY_RESULTS,
    GovernanceDecision,
    PolicyDocumentUndefinedError,
    PolicySourceUnavailableError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "GOVERNANCE_DECISION_PATH",
    "OpaGovernancePolicy",
    "governance_input",
]

#: `package forgeops.governance` in policies/agent/governance.rego, rule `decision`.
#: `tests/integration/test_governance_policy_opa.py` asserts this path resolves to a
#: DEFINED document against the real bundle, which is the assertion Phase 0's MCP client
#: did not have and paid for (D-25).
GOVERNANCE_DECISION_PATH: Final[str] = "/v1/data/forgeops/governance/decision"

#: The keys `governance_input` copies out of `payload["policy_parameters"]` into
#: `input.project`. A closed list rather than a passthrough: an unknown key would reach the
#: bundle, be ignored by every rule, and look like a parameter that was applied.
PROJECT_PARAMETER_KEYS: Final[tuple[str, ...]] = (
    "timezone",
    "blocked_weekdays",
    "blocked_operations",
    "blocked_window",
    "protected_globs",
)


def governance_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Map one chokepoint stage-1 payload onto the bundle's input document.

    The bundle reads `input.operation`, `input.now_rfc3339`, `input.environment`,
    `input.change_items` and `input.project.*` (§11.7). The chokepoint's payload uses its
    own names — `now`, `items` — because it predates the bundle, so the translation is
    here rather than in either of them.

    Two members are **omitted** rather than defaulted when the caller has none, and both
    omissions are load-bearing:

    * `environment` — absent means the caller did not state one, and `approval.rego`
      answers `require_approval`. A default of `"dev"` would have been the alternative and
      is precisely the fail-open shape finding 68 records: a caller that forgot the field
      would get an auto-approvable verdict for what might be a production change.

    * `blast_radius` — absent means "not evaluated yet", which at stage 1 is always true:
      §2.2's stage order runs policy at stage 1 and the Semantic Plan Analyzer at stage 4,
      so no verdict exists when this runs (finding 71). `approval.rego` treats an absent
      `blast_radius` as not-applicable and a **present but verdict-less** one as
      fail-closed, so a refactor that starts sending the member without filling it in
      cannot quietly disable the clause.
    """
    document: dict[str, Any] = {
        "operation": payload.get("operation"),
        "now_rfc3339": payload.get("now"),
        "change_items": list(payload.get("items") or []),
        "project": _project_parameters(payload.get("policy_parameters")),
    }

    environment = payload.get("environment")
    if environment is not None:
        document["environment"] = environment

    blast_radius = payload.get("blast_radius")
    if blast_radius is not None:
        document["blast_radius"] = blast_radius

    # Carried for explainability and for the agent's side of Q-06, never read by a rule in
    # this bundle. Kept out of `project` so a future parameter cannot collide with it.
    document["context"] = {
        "project_id": payload.get("project_id"),
        "tenant_id": payload.get("tenant_id"),
        "device_id": payload.get("device_id"),
        "bundle_digest": payload.get("bundle_digest"),
        "change_set_id": payload.get("change_set_id"),
        "principal": payload.get("principal"),
    }
    return document


def _project_parameters(parameters: Any) -> dict[str, Any]:
    """Copy the closed parameter set, dropping nothing silently and inventing nothing."""
    if not isinstance(parameters, Mapping):
        return {}
    return {key: parameters[key] for key in PROJECT_PARAMETER_KEYS if key in parameters}


class OpaGovernancePolicy:
    """Query the governance bundle on the OPA server. Satisfies `GovernancePolicySource`."""

    def __init__(
        self,
        *,
        opa_url: str,
        http: httpx.AsyncClient,
        decision_path: str = GOVERNANCE_DECISION_PATH,
    ) -> None:
        if http is None:  # pragma: no cover - defended so the shared client is never optional
            raise ValueError(
                "OpaGovernancePolicy requires the app's shared httpx client; §11.7 forbids a "
                "second connection pool for one more HTTP dependency"
            )
        self._opa_url = opa_url.rstrip("/")
        self._http = http
        self._decision_path = decision_path

    async def evaluate(self, *, payload: Mapping[str, Any]) -> GovernanceDecision:
        """Return the bundle's decision for one transit.

        Raises:
            PolicySourceUnavailableError: OPA could not be reached, timed out, answered a
                non-2xx status, or answered something that is not JSON. The chokepoint
                turns this into a deny (§2.2, §11.6).
            PolicyDocumentUndefinedError: OPA answered, and the queried document is either
                undefined or not a decision. The chokepoint turns this into 503, never a
                deny (D-25).
        """
        document = governance_input(payload)
        url = f"{self._opa_url}{self._decision_path}"
        try:
            response = await self._http.post(url, json={"input": document})
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # transport, status, or a body that is not JSON
            raise PolicySourceUnavailableError(f"OPA at {url} did not answer: {exc}") from exc

        if not isinstance(body, Mapping) or "result" not in body:
            logger.error("governance policy document undefined at %s", self._decision_path)
            raise PolicyDocumentUndefinedError(
                f"the governance document at '{self._decision_path}' is not defined in OPA; "
                "the bundle is missing, renamed, or failed to load"
            )

        return _decision_from(body["result"], path=self._decision_path)


def _decision_from(result: Any, *, path: str) -> GovernanceDecision:
    """Validate the bundle's answer and build the decision.

    Every rejection here is `PolicyDocumentUndefinedError`, for the reason the module
    docstring gives: a document that answers the wrong shape is a broken deployment, and
    the operator instruction is identical to a missing one.
    """
    if not isinstance(result, Mapping):
        raise PolicyDocumentUndefinedError(
            f"the governance document at '{path}' returned {type(result).__name__}, not a decision object"
        )

    outcome = result.get("result")
    if outcome not in POLICY_RESULTS:
        raise PolicyDocumentUndefinedError(
            f"the governance document at '{path}' returned result={outcome!r}, which is not one of {POLICY_RESULTS}"
        )

    rule_id = result.get("rule") or None
    reason = str(result.get("reason") or "").strip()
    if not reason:
        # `GovernanceDecision` requires a non-empty reason (NFR-14), and the bundle returns
        # `""` for an allow because there is nothing to explain. Filling it in here rather
        # than making the rule invent prose keeps the Rego honest about having no reason.
        reason = f"allowed by {rule_id}" if outcome == "allow" and rule_id else f"{outcome} with no stated reason"

    return GovernanceDecision(result=outcome, reason=reason, rule_id=rule_id if isinstance(rule_id, str) else None)
