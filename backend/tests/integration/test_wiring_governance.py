# SPDX-License-Identifier: FSL-1.1-ALv2
"""The chokepoint over the REAL composition (design.md §0.4.1, §2.2, §11.1, §11.6; leaf 7.5).

§0.4.1's clause 1: every component the production lifespan composes has a test that drives it
through the **real** object graph. `test_wiring_coverage.py` fails the build if any `app.state`
name is composed without a `@wires(...)` declaration somewhere, so the five names below need this
file to exist — the mechanism rather than the intention.

`production_app` points the app at unreachable Postgres and Redis, which is why this file asserts
**composition and defaults** rather than transits. The transits are asserted against real services
in `test_governance_chokepoint.py`. That division is the same one the audit surface already uses:
this file proves the chokepoint is assembled from the real collaborators and fails closed, that one
proves the six stages behave.

The two fail-closed defaults are the point of the assertions below. A backend at this wave has no
policy engine and no hub, and the honest behaviour is to refuse every mutation and say why. A
permissive default would let a mutation through on the strength of nothing objecting, and it would
do so silently — which is exactly the shape §9's convention forbids.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from .production_app import production_app  # noqa: F401 - fixture
from .wiring import wires

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


@wires(
    "governance_chokepoint",
    "governance_policy",
    "command_sink",
    "envelope_sequencer",
    "device_service",
)
class TestTheChokepointIsComposedFromTheRealCollaborators:
    async def test_the_chokepoint_is_on_app_state(self, production_app: FastAPI) -> None:  # noqa: F811
        from src.governance.chokepoint import GovernanceChokepoint

        assert isinstance(production_app.state.governance_chokepoint, GovernanceChokepoint)

    async def test_it_shares_the_composed_audit_writer(self, production_app: FastAPI) -> None:  # noqa: F811
        """Not a second writer. Q-04's "same transaction" only holds if the record the chokepoint
        writes goes through the writer the rest of the app uses — two instances with different
        advisory-lock keys would fork the chain under concurrency."""
        chokepoint = production_app.state.governance_chokepoint
        assert chokepoint._audit is production_app.state.audit_writer  # noqa: SLF001 - composition assertion

    async def test_it_shares_the_composed_policy_source_and_sink(self, production_app: FastAPI) -> None:  # noqa: F811
        """The same objects that are on `app.state`, so replacing either in leaf 8.4 or 9.2 is one
        edit rather than two that could disagree."""
        chokepoint = production_app.state.governance_chokepoint
        assert chokepoint._policy is production_app.state.governance_policy  # noqa: SLF001
        assert chokepoint._sink is production_app.state.command_sink  # noqa: SLF001
        assert chokepoint._sequencer is production_app.state.envelope_sequencer  # noqa: SLF001

    async def test_the_gate_and_analyzer_are_the_phase_zero_implementations(
        self,
        production_app: FastAPI,  # noqa: F811
    ) -> None:
        """§2.2: "every stage is an existing, tested component where one exists". A new gate or a
        new analyser here would mean the chokepoint had grown its own copy of P-11's monotonicity."""
        from src.analysis.plan_analyzer.approval import ThresholdApprovalGate
        from src.analysis.plan_analyzer.semantic import SemanticPlanAnalyzer

        chokepoint = production_app.state.governance_chokepoint
        assert isinstance(chokepoint._gate, ThresholdApprovalGate)  # noqa: SLF001
        assert isinstance(chokepoint._analyzer, SemanticPlanAnalyzer)  # noqa: SLF001

    async def test_the_envelope_max_age_comes_from_configuration(self, production_app: FastAPI) -> None:  # noqa: F811
        """`not_after` is `now + ENVELOPE_MAX_AGE_SECONDS` (§7.6), so the running app must be
        derived from the setting rather than from a default baked into the class — the same
        provenance question Q-27 asks of the tier config."""
        settings = production_app.state.settings
        chokepoint = production_app.state.governance_chokepoint
        assert chokepoint._max_age == settings.envelope_max_age_seconds  # noqa: SLF001

    async def test_the_device_service_uses_the_configured_pepper(self, production_app: FastAPI) -> None:  # noqa: F811
        """D-62: the key-encryption key is derived from `ENVELOPE_PEPPER` and from nothing else.

        Compares the DERIVED key rather than the pepper itself, so a passing assertion cannot be
        satisfied by a service that stored the pepper and then derived its KEK from something else.
        """
        from src.auth.devices import derive_key_encryption_key

        settings = production_app.state.settings
        service = production_app.state.device_service
        expected = derive_key_encryption_key(settings.envelope_pepper.get_secret_value())
        assert derive_key_encryption_key(service._pepper) == expected  # noqa: SLF001


@wires("governance_policy", "command_sink")
class TestTheDefaultsFailClosed:
    async def test_the_composed_policy_source_refuses_rather_than_allowing(
        self,
        production_app: FastAPI,  # noqa: F811
    ) -> None:
        from src.governance.policy import PolicySourceUnavailableError, UnavailableGovernancePolicy

        policy = production_app.state.governance_policy
        assert isinstance(policy, UnavailableGovernancePolicy)
        with pytest.raises(PolicySourceUnavailableError):
            await policy.evaluate(payload={})

    async def test_the_composed_sink_refuses_when_no_agent_is_connected(self, production_app: FastAPI) -> None:  # noqa: F811
        """The sink is the real hub from leaf 8.4, and it keeps `UnavailableCommandSink`'s refusal.

        `production_app` points at an unreachable Redis, so the hub cannot find a live session for
        any device — which is the same answer it gives in production for a device that is not
        connected, and the same 409 the placeholder sink used to give. What changed is that the
        refusal is now a fact about the device rather than a fact about the backend.
        """
        import uuid

        from src.core.errors import ProblemException
        from src.governance.chokepoint import SignedCommand
        from src.websocket.hub import AgentHub

        sink = production_app.state.command_sink
        assert isinstance(sink, AgentHub)
        assert sink is production_app.state.agent_hub
        with pytest.raises(ProblemException) as raised:
            await sink.send_command(
                device_id=uuid.uuid4(),
                command=SignedCommand(envelope={}, signature="s", digest="d", device_id=uuid.uuid4()),
            )
        assert raised.value.problem.status == 409

    async def test_the_chokepoint_exposes_no_route(self, production_app: FastAPI) -> None:  # noqa: F811
        """There is deliberately no HTTP surface yet. §1.6's Change Approval Center API is leaf
        16.x; a route added here would be a mutation entry point with no Cerbos scoping and no
        request schema, and `check-route-auth.py` would be the only thing looking at it."""
        paths = set(production_app.openapi()["paths"])
        assert not [path for path in paths if "change-set" in path or "changeset" in path]
        assert not [path for path in paths if path.startswith("/api/v1/governance")]
