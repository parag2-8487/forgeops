# SPDX-License-Identifier: FSL-1.1-ALv2
"""The GitHub link service over the REAL composition (design.md §0.4.1).

§0.4.1's clause 1: every `app.state` name the production lifespan composes needs a test that drives it
through the real object graph, and `test_wiring_coverage.py` fails the build on any name composed
without a `@wires(...)` declaration. Behaviour lives in `test_github_link_routes.py` against a real
database and a real HTTP server; this file asserts the composition and the two properties that a
composition mistake would silently break.

WHY THE KEY IS ASSERTED HERE. The service's whole safety rests on a key derived from `ENVELOPE_PEPPER`
under its own label. A service composed with the WRONG key still works — it seals and unseals
consistently — so nothing in the behavioural tests could tell. What it loses is domain separation: seal
under the envelope-key KEK and a leak of one is a leak of both. That is a composition property, so it is
asserted against the composition.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from .production_app import production_app  # noqa: F401 - fixture
from .wiring import wires

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


@wires("github_link_service")
class TestTheLinkServiceIsComposedFromTheRealCollaborators:
    async def test_it_is_on_app_state_with_the_real_type(self, production_app: FastAPI) -> None:  # noqa: F811
        from src.integrations.service import GitHubLinkService

        assert isinstance(production_app.state.github_link_service, GitHubLinkService)

    async def test_its_key_is_derived_from_the_pepper_under_its_own_label(
        self,
        production_app: FastAPI,  # noqa: F811
    ) -> None:
        """The right key AND not the envelope key. Both halves matter, for the reason above."""
        from src.auth.devices import derive_key_encryption_key
        from src.integrations.github_link import derive_link_key

        settings = production_app.state.settings
        pepper = settings.envelope_pepper.get_secret_value()
        service = production_app.state.github_link_service

        assert service._key == derive_link_key(pepper)  # noqa: SLF001
        assert service._key != derive_key_encryption_key(pepper)  # noqa: SLF001

    async def test_an_unconfigured_deployment_still_composes_and_says_so(
        self,
        production_app: FastAPI,  # noqa: F811
    ) -> None:
        """A fresh install has no GitHub App, and that must not be a composition failure.

        `production_app` runs on the committed baseline environment, in which the App credentials are
        empty — so this asserts the state every fresh install is in: the service exists and reports that
        it cannot link yet, rather than the lifespan refusing to start.
        """
        service = production_app.state.github_link_service

        assert service.is_configured() is False

    async def test_the_two_routers_are_served_and_only_the_callback_is_public(
        self,
        production_app: FastAPI,  # noqa: F811
    ) -> None:
        """The split the module argues for, asserted rather than described.

        A rename on one side only — the route moved, the registry entry left behind — would leave either
        a callback nobody can reach or an exemption applying to whatever takes the old path next.
        """
        from src.auth.public_routes import PUBLIC_PATHS

        paths = set(production_app.openapi()["paths"])
        assert "/api/v1/integrations/github" in paths
        assert "/api/v1/integrations/github/connect" in paths
        assert "/api/v1/integrations/github/callback" in paths
        assert "/api/v1/integrations/github/repositories" in paths

        assert "/api/v1/integrations/github/callback" in PUBLIC_PATHS
        assert "/api/v1/integrations/github" not in PUBLIC_PATHS
        assert "/api/v1/integrations/github/connect" not in PUBLIC_PATHS
        assert "/api/v1/integrations/github/repositories" not in PUBLIC_PATHS
