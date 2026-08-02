# SPDX-License-Identifier: FSL-1.1-ALv2
"""The REAL `OpaGovernancePolicy` against a REAL OPA server loading the REAL bundle.

Design: §5.5, §11.7, §14.1; task 9.2; deliverable 1.7; criterion 7.

Why this is an integration test and not a unit test with a stubbed transport
---------------------------------------------------------------------------
The exact defect this shape exists to catch already shipped once in this repository: the
MCP gateway queried `/v1/data/forgeops/mcp/filter_tools`, which does not exist in
`package mcp.gateway`, and OPA answers an undefined document with HTTP 200 and no `result`
key — so `raise_for_status()` never fired, every list came back empty, and 27/27 Rego tests
stayed green. A stub of the HTTP client asserts what the author believed the path resolved
to. Only a real server can say whether `data.forgeops.governance.decision` is a document.

The five cases task 9.2 names are all here, and each states what would pass without it:

* **allow** — `TestTheRealBundleAnswersEachResult::test_an_allow`, which would also pass for
  a client that returned a hard-coded allow, so it is paired with the deny and the
  require-approval cases against the SAME client.
* **deny** — a Friday inside the project's window, so the deny comes from the bundle's
  scheduling rule rather than from a transport error.
* **require-approval** — `environment == "prod"`, the third of phases.md §1.7's policies.
* **undefined document** — a client pointed at a real, healthy OPA and a path that names no
  rule. This must be `PolicyDocumentUndefinedError`, which the chokepoint turns into 503,
  and it must NOT be a deny (D-25).
* **transport failure** — a dead address. This must be `PolicySourceUnavailableError`,
  which the chokepoint turns into a deny.

The last two are siblings and are asserted to be siblings, because if either were a
subclass of the other, the chokepoint's two `except` clauses would collapse and one of the
translations would silently stop happening — during an outage, which is the worst time to
find out.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from src.governance.policy import (
    GovernanceDecision,
    PolicyDocumentUndefinedError,
    PolicySourceError,
    PolicySourceUnavailableError,
)
from src.policies.opa import (
    GOVERNANCE_DECISION_PATH,
    PROJECT_PARAMETER_KEYS,
    OpaGovernancePolicy,
    governance_input,
)

from .opa_server import GOVERNANCE_POLICY_DIR, opa_server

#: 2026-08-07T02:30:00Z is a Friday in UTC and Friday 08:00 in Asia/Kolkata. The same
#: instant `policies/agent/governance_test.rego` uses, so the two test suites cannot drift
#: into asserting different calendars.
FRIDAY_0230_UTC = "2026-08-07T02:30:00Z"
WEDNESDAY_1000_UTC = "2026-08-05T10:00:00Z"

BLOCKED_FRIDAYS: dict[str, Any] = {
    "timezone": "Asia/Kolkata",
    "blocked_weekdays": ["Friday"],
    "blocked_window": {"start_hour": 6, "end_hour": 20},
    "protected_globs": ["**/package.json"],
}


def payload(**overrides: Any) -> dict[str, Any]:
    """A chokepoint stage-1 payload, in the shape `_evaluate_policy` builds.

    Written out here rather than imported from the chokepoint on purpose: this test is the
    contract between the two, and deriving the fixture from one side would make the contract
    unfalsifiable. `test_the_payload_shape_matches_what_the_chokepoint_sends` below closes
    the loop by comparing the key sets.
    """
    base: dict[str, Any] = {
        "operation": "changeset.apply",
        "project_id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": None,
        "device_id": "22222222-2222-2222-2222-222222222222",
        "bundle_digest": "sha256:" + "ab" * 32,
        "change_set_id": None,
        "environment": "dev",
        "policy_parameters": dict(BLOCKED_FRIDAYS),
        "principal": {"kind": "user", "role": "maintainer", "blast_radius": "workspace", "user_id": "u-1"},
        "items": [{"file_path": "src/index.ts", "action": "modify"}],
        "now": WEDNESDAY_1000_UTC,
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="module")
def governance_opa_url():
    """A real OPA loading `policies/agent`, started for this module.

    `preset_env=None` deliberately: a preset server started for a different bundle would
    answer this document as undefined, and the failure would read like a policy bug rather
    than like a fixture pointed at the wrong tree.
    """
    with opa_server(GOVERNANCE_POLICY_DIR, preset_env=None) as url:
        yield url


@pytest.fixture()
async def policy(governance_opa_url: str):
    async with httpx.AsyncClient(timeout=5.0) as http:
        yield OpaGovernancePolicy(opa_url=governance_opa_url, http=http)


class TestTheRealBundleAnswersEachResult:
    pytestmark = pytest.mark.asyncio

    async def test_an_allow(self, policy: OpaGovernancePolicy) -> None:
        decision = await policy.evaluate(payload=payload())

        assert isinstance(decision, GovernanceDecision)
        assert decision.result == "allow"
        assert decision.rule_id == "governance.allow"
        # The bundle returns `""` for an allow because there is nothing to explain, and
        # `GovernanceDecision` requires a non-empty reason (NFR-14). The client fills it from
        # the rule id rather than making the Rego invent prose.
        assert decision.reason == "allowed by governance.allow"

    async def test_a_deny_comes_from_the_scheduling_rule(self, policy: OpaGovernancePolicy) -> None:
        decision = await policy.evaluate(payload=payload(now=FRIDAY_0230_UTC))

        assert decision.result == "deny"
        assert decision.rule_id == "schedule.blocked_window"
        assert decision.reason == "blocked deployment window: Friday 06:00-20:00 in Asia/Kolkata"

    async def test_a_deny_comes_from_the_protected_path_rule(self, policy: OpaGovernancePolicy) -> None:
        decision = await policy.evaluate(payload=payload(items=[{"file_path": "package.json", "action": "modify"}]))

        assert decision.result == "deny"
        assert decision.rule_id == "paths.protected_path"
        assert decision.reason == "protected path: package.json"

    async def test_require_approval_for_production(self, policy: OpaGovernancePolicy) -> None:
        decision = await policy.evaluate(payload=payload(environment="prod"))

        assert decision.result == "require_approval"
        assert decision.rule_id == "approval.required"
        assert decision.reason == 'environment is "prod"'

    async def test_an_unstated_environment_requires_approval(self, policy: OpaGovernancePolicy) -> None:
        """`MutationRequest.environment` defaults to `None`, and that must not auto-approve.

        The client OMITS the member rather than defaulting it, so the bundle's
        `require_approval if not input.environment` clause fires (finding 68).
        """
        decision = await policy.evaluate(payload=payload(environment=None))

        assert decision.result == "require_approval"
        assert decision.reason == "environment is absent"

    async def test_a_project_with_no_parameters_gets_a_defined_allow(self, policy: OpaGovernancePolicy) -> None:
        """The state leaf 9.2 actually ships in: no policy rows exist until leaf 9.5.

        This is the assertion that makes an empty parameter set safe to compose. Totality is
        what turns "no blocked weekday" into a defined allow instead of an undefined
        document, and an undefined document here would be a 503 on every mutation.
        """
        decision = await policy.evaluate(payload=payload(policy_parameters={}))

        assert decision.result == "allow"

    async def test_a_malformed_operation_is_denied_not_undefined(self, policy: OpaGovernancePolicy) -> None:
        decision = await policy.evaluate(payload=payload(operation=None))

        assert decision.result == "deny"
        assert decision.rule_id == "governance.malformed_input"


class TestTheTwoFailuresAreDistinguishable:
    """D-25's lesson, measured. These two must never collapse into one another."""

    pytestmark = pytest.mark.asyncio

    async def test_an_undefined_document_raises_undefined_not_a_deny(self, governance_opa_url: str) -> None:
        async with httpx.AsyncClient(timeout=5.0) as http:
            misconfigured = OpaGovernancePolicy(
                opa_url=governance_opa_url,
                http=http,
                decision_path="/v1/data/forgeops/governance/verdict",  # no such rule
            )
            with pytest.raises(PolicyDocumentUndefinedError):
                await misconfigured.evaluate(payload=payload())

    async def test_the_default_path_is_a_defined_document(self, governance_opa_url: str) -> None:
        """The control for the case above, and the exact regression the MCP client shipped.

        Asked of OPA directly rather than through the client, so it cannot pass because the
        client happened to fill in a default.
        """
        async with httpx.AsyncClient(timeout=5.0) as http:
            response = await http.post(
                f"{governance_opa_url}{GOVERNANCE_DECISION_PATH}",
                json={"input": governance_input(payload())},
            )
        assert response.status_code == 200
        assert "result" in response.json(), f"{GOVERNANCE_DECISION_PATH} is not a defined document"

    async def test_a_transport_failure_raises_unavailable(self) -> None:
        async with httpx.AsyncClient(timeout=0.05) as http:
            # Reserved-for-documentation address (RFC 5737); nothing listens.
            dead = OpaGovernancePolicy(opa_url="http://192.0.2.1:8181", http=http)
            with pytest.raises(PolicySourceUnavailableError):
                await dead.evaluate(payload=payload())

    async def test_a_non_2xx_status_is_unavailable_rather_than_undefined(self, governance_opa_url: str) -> None:
        """OPA answers 404 for a path outside `/v1/data`. That is a transport-level failure.

        The distinction matters: an undefined DOCUMENT is a bundle problem (503, look at the
        bundle), while a 404 from the wrong API root is the engine saying it does not serve
        that at all (deny, look at the URL).
        """
        async with httpx.AsyncClient(timeout=5.0) as http:
            wrong_root = OpaGovernancePolicy(
                opa_url=governance_opa_url,
                http=http,
                decision_path="/v9/nonexistent/api",
            )
            with pytest.raises(PolicySourceUnavailableError):
                await wrong_root.evaluate(payload=payload())


class TestTheErrorHierarchy:
    def test_the_two_errors_are_siblings_not_subclasses(self) -> None:
        assert issubclass(PolicyDocumentUndefinedError, PolicySourceError)
        assert issubclass(PolicySourceUnavailableError, PolicySourceError)
        assert not issubclass(PolicyDocumentUndefinedError, PolicySourceUnavailableError)
        assert not issubclass(PolicySourceUnavailableError, PolicyDocumentUndefinedError)


class TestTheInputMapping:
    """`governance_input` is where a field can go missing, so it is asserted on its own."""

    def test_the_chokepoint_names_are_translated_to_the_bundle_names(self) -> None:
        document = governance_input(payload())

        assert document["operation"] == "changeset.apply"
        assert document["now_rfc3339"] == WEDNESDAY_1000_UTC
        assert document["change_items"] == [{"file_path": "src/index.ts", "action": "modify"}]
        assert document["environment"] == "dev"

    def test_an_absent_environment_is_omitted_rather_than_defaulted(self) -> None:
        assert "environment" not in governance_input(payload(environment=None))

    def test_an_absent_blast_radius_is_omitted(self) -> None:
        """Stage 1 has no verdict: §2.2 runs the analyzer at stage 4 (finding 71)."""
        assert "blast_radius" not in governance_input(payload())

    def test_a_supplied_blast_radius_is_passed_through(self) -> None:
        document = governance_input(payload(blast_radius={"verdict": "review"}))

        assert document["blast_radius"] == {"verdict": "review"}

    def test_only_the_closed_parameter_set_reaches_the_bundle(self) -> None:
        document = governance_input(payload(policy_parameters={**BLOCKED_FRIDAYS, "not_a_parameter": "surprise"}))

        assert set(document["project"]) <= set(PROJECT_PARAMETER_KEYS)
        assert "not_a_parameter" not in document["project"]

    def test_a_non_mapping_parameter_set_yields_an_empty_project(self) -> None:
        assert governance_input(payload(policy_parameters="nonsense"))["project"] == {}

    def test_the_context_is_carried_but_never_inside_project(self) -> None:
        document = governance_input(payload())

        assert document["context"]["bundle_digest"] == "sha256:" + "ab" * 32
        assert document["context"]["principal"]["role"] == "maintainer"
        assert "principal" not in document["project"]

    def test_the_payload_shape_matches_what_the_chokepoint_sends(self) -> None:
        """Closes the loop on the hand-written fixture above.

        `_evaluate_policy` builds its payload as a literal, so its key set is readable from
        the source. Comparing key sets rather than values keeps this a contract test: if the
        chokepoint grows or drops a key, this fails and the author has to decide whether the
        mapping should read it.
        """
        import inspect
        import re

        from src.governance import chokepoint

        source = inspect.getsource(chokepoint.GovernanceChokepoint._evaluate_policy)  # noqa: SLF001
        body = source.split("payload = {", 1)[1].split("\n        }", 1)[0]
        # Anchored to the literal's OWN indent level (12 spaces). Without the anchor the
        # nested `principal` keys are picked up too, and the comparison fails for a reason
        # that has nothing to do with drift — which is how a contract test becomes noise
        # somebody loosens.
        keys = set(re.findall(r'^ {12}"([a-z_]+)":', body, flags=re.MULTILINE))

        assert keys, "could not read the chokepoint payload's keys; this test is now vacuous"
        assert keys == set(payload()), f"payload drift: {keys ^ set(payload())}"
