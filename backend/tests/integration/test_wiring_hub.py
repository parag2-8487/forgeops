# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent hub over the REAL composition (design.md §0.4.1, §11.10; leaf 8.4).

§0.4.1's clause 1: every `app.state` name the production lifespan composes needs a test that drives
it through the real object graph, and `test_wiring_coverage.py` fails the build on any name that is
composed without a `@wires(...)` declaration. Behaviour lives in `test_agent_hub.py` against a real
Redis; this file asserts the composition and the one default that must fail closed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from .production_app import production_app  # noqa: F401 - fixture
from .wiring import wires

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


@wires("agent_hub", "client_certificate_source")
class TestTheHubIsComposedFromTheRealCollaborators:
    async def test_the_hub_is_on_app_state_and_is_the_chokepoint_sink(self, production_app: FastAPI) -> None:  # noqa: F811
        """One hub, not two. A second instance would hold a second set of sockets and a second
        pending-command table, so a result could resolve a future nobody was waiting on."""
        from src.websocket.hub import AgentHub

        hub = production_app.state.agent_hub
        assert isinstance(hub, AgentHub)
        assert production_app.state.command_sink is hub
        chokepoint = production_app.state.governance_chokepoint
        assert chokepoint._sink is hub  # noqa: SLF001

    async def test_the_hub_authenticates_through_the_composed_device_service(
        self,
        production_app: FastAPI,  # noqa: F811
    ) -> None:
        """The hub's `DeviceDirectory` is the real `DeviceService`. Declared as a Protocol in the
        hub because §2.4 bans `src.auth.devices` outside `governance/`, so this is the assertion
        that the seam is filled by the real thing rather than by something that merely fits."""
        hub = production_app.state.agent_hub
        assert hub._deps.devices is production_app.state.device_service  # noqa: SLF001

    async def test_the_heartbeat_numbers_come_from_configuration(self, production_app: FastAPI) -> None:  # noqa: F811
        """§7.3's 30 s and 90 s reach the handshake result from `HEARTBEAT_*_SECONDS`, and
        `core.config` already refuses a timeout that does not exceed the interval."""
        settings = production_app.state.settings
        deps = production_app.state.agent_hub._deps  # noqa: SLF001
        assert deps.heartbeat_interval_seconds == settings.heartbeat_interval_seconds
        assert deps.heartbeat_timeout_seconds == settings.heartbeat_timeout_seconds
        assert deps.heartbeat_timeout_seconds > deps.heartbeat_interval_seconds

    async def test_the_certificate_source_trusts_no_header(self, production_app: FastAPI) -> None:  # noqa: F811
        """The composed source reads the TLS peer certificate and nothing else.

        A plaintext scope — and a scope carrying a forged `x-forwarded-client-cert` header — both
        yield `None`, which the route turns into a refused handshake. A hub that accepted a header by
        default would authenticate anybody who could reach the port.
        """
        from src.websocket.hub import TlsPeerCertificate

        from ..synthetic_secrets import pem_armour

        source = production_app.state.client_certificate_source
        assert isinstance(source, TlsPeerCertificate)
        assert source.certificate_pem({}) is None
        # The armour is assembled rather than written out: the secret gate matches on PEM shape and
        # not on sensitivity, and this is the repository's established remedy for that.
        forged = {
            "headers": [(b"x-forwarded-client-cert", (pem_armour("CERTIFICATE") + "\nnope\n").encode("utf-8"))],
        }
        assert source.certificate_pem(forged) is None

    async def test_the_socket_route_is_served_at_the_path_the_agent_dials(
        self,
        production_app: FastAPI,  # noqa: F811
    ) -> None:
        """`AGENT_BACKEND_WSS_URL`'s default path. A rename on one side only would leave every agent
        dialling a 404, which no test that reads the router by name would notice.

        The routes are walked recursively rather than read off `app.routes`, and the descent follows
        `original_router`: FastAPI 0.139 keeps each `include_router` as one opaque `_IncludedRouter`,
        so a flat read — or a read that descends on `.routes` — finds no WebSocket route at all and
        this assertion would pass vacuously in the direction that matters.
        `scripts/check-route-auth.py` learned the same thing (its `_flatten` docstring records it),
        and this walk is duck-typed the same way.
        """
        from src.websocket.routes import WS_AGENT_PATH

        def socket_paths(routes: object, prefix: str = "") -> set[str]:
            found: set[str] = set()
            for route in routes:  # type: ignore[union-attr]
                included = getattr(route, "original_router", None)
                if included is not None:
                    context = getattr(route, "include_context", None)
                    found |= socket_paths(
                        getattr(included, "routes", []), prefix + (getattr(context, "prefix", "") or "")
                    )
                    continue
                raw = getattr(route, "path", None)
                # A WebSocket route is the one kind with no method set — the same signal
                # `check-route-auth.py` keys off.
                if raw is not None and getattr(route, "methods", None) is None:
                    found.add(prefix + raw)
            return found

        assert WS_AGENT_PATH in socket_paths(production_app.routes)

    async def test_the_socket_route_is_not_public(self, production_app: FastAPI) -> None:  # noqa: F811
        """It authenticates inside the handshake with two secrets, so it must not appear in
        `PUBLIC_ROUTES`: an entry there would be an exemption nobody needs and everybody would
        eventually read as permission."""
        from src.auth.public_routes import PUBLIC_PATHS
        from src.websocket.routes import WS_AGENT_PATH

        assert WS_AGENT_PATH not in PUBLIC_PATHS
