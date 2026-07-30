# SPDX-License-Identifier: FSL-1.1-ALv2
"""OAuth 2.1 / OIDC bearer verification with strict issuer checking.

The implementation moved to `core/security.py` in Phase 1 and is re-exported here.

The move was forced by the modular-monolith rule rather than chosen for tidiness.
§11.2's `AppTokenVerifier` must extend this verifier rather than duplicate it — two
copies of a token verifier means two places for a verification bug to live, and only
one of them gets fixed — but `src/auth` importing `src/mcp` is exactly the
domain-to-domain coupling the Ruff `flake8-tidy-imports` ban forbids. `core` already
owned the `TokenVerifier` Protocol, and a bearer verifier is a cross-cutting primitive
rather than an MCP concern, so the implementation belongs beside the contract.

This module is kept, rather than its importers repointed, because every gateway import
of `..mcp.auth` stays valid and the gateway's 27 Rego tests and its wiring tests keep
exercising the same names. Deleting it would have made a mechanical move look like a
behavioural change in the diff.
"""

from __future__ import annotations

from ..core.security import Claims, OidcTokenVerifier

__all__ = ["Claims", "OidcTokenVerifier"]
