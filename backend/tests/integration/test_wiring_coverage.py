# SPDX-License-Identifier: FSL-1.1-ALv2
"""Wiring coverage over the real composition (design.md §0.4.1, §7.8, §11.1).

`app.state` is the production composition's public surface. Every attribute the
lifespan places there MUST be named by at least one `@wires(...)` declaration in
some wiring test, so a newly composed component cannot arrive untested — the
Phase 0 failure mode where `app.state.mcp_gateway` was composed, covered by
line-coverage, and still raised `TypeError` on every request (D-23).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from .production_app import composed_state_names
from .wiring import collect_wiring_declarations, wires

pytestmark = pytest.mark.mandatory

INTEGRATION_ROOT = Path(__file__).resolve().parent


@wires("settings", "engine", "sessionmaker", "redis")
class TestCompositionSurfaceIsDeclared:
    """The infrastructure edges of the composition, declared here.

    These four are exercised as a graph by `test_lifespan_health.py`, which drives
    `/health` and `/health/ready` through the real engine and Redis client. The
    declaration lives beside the coverage assertion rather than in that file
    because these are the composition's *infrastructure* edges rather than a
    domain seam; the MCP collaborators declare themselves in their own wiring test.
    """

    def test_the_composed_surface_is_not_empty(self, production_app: FastAPI) -> None:
        """A collector that returns nothing would make this whole clause vacuous.

        This is the same guard §0.4.5 puts on the mutation harness and §0.4.4 puts
        on the mandatory selection: an enumeration that silently yields nothing
        passes every subset assertion, so emptiness is a failure, not a pass.
        Starlette stores `app.state` behind a single private `_state` dict, so the
        naive `vars(...)` spelling really does return nothing — see
        `production_app.composed_state_names`.
        """
        composed = composed_state_names(production_app)
        assert composed, (
            "no app.state attributes were discovered on the real composition; "
            "the enumeration is broken and every subset assertion below is vacuous"
        )
        # The lifespan composes strictly more than the four infrastructure edges;
        # a composition this small means the factory did not run.
        assert len(composed) >= 4, f"implausibly small composition: {sorted(composed)}"

    def test_declarations_are_collected_from_source(self) -> None:
        """The collector must find this file's own declaration.

        Proves the AST collector is wired to the right root and matches the real
        decorator spelling, so a green coverage assertion means something.
        """
        declared = collect_wiring_declarations(INTEGRATION_ROOT)
        assert {"settings", "engine", "sessionmaker", "redis"} <= declared

    def test_every_composed_collaborator_has_a_wiring_test(self, production_app: FastAPI) -> None:
        """The clause itself: composed ⊆ declared."""
        composed = composed_state_names(production_app)
        declared = collect_wiring_declarations(INTEGRATION_ROOT)
        undeclared = sorted(composed - declared)
        assert not undeclared, (
            "composed but never wiring-tested: "
            f"{undeclared}. Add a @wires(...) declaration to the test that drives "
            "each one through the real object graph — see design.md §0.4.1."
        )
