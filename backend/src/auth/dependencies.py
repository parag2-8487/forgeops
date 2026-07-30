# SPDX-License-Identifier: FSL-1.1-ALv2
"""The per-route auth dependencies (design.md §4.3 row 7, §11.2, §14.1, Q-19).

**Per route, never global.** A global dependency or a middleware that authenticated
everything would have to carve out the public set by path matching, and a path matcher
is where an unauthenticated route hides: it is invisible in the route definition, so a
reviewer reading a handler cannot tell whether it is protected. Attaching the
dependency to each router makes the protection visible where the route is declared, and
`scripts/check-route-auth.py` makes completeness mechanical rather than a review
obligation.

The cost of that choice is that a new router can forget the dependency. That is
exactly what the checker exists for, and Q-19 asserts it over the real
`create_app().routes`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.requests import Request

from ..core.errors import forbidden_problem
from .models import UserRole
from .principal import Principal
from .public_routes import is_public

#: Where the resolved principal is cached on the request. Read by the tenant-context
#: middleware and by the audit writer, so both see exactly what the verifier produced.
PRINCIPAL_STATE_ATTR = "forgeops_principal"

#: The dependencies that satisfy deny-by-default, committed here rather than matched by
#: substring in `scripts/check-route-auth.py`.
#:
#: A substring heuristic would accept any function whose name happened to contain
#: `require_`, and — worse — would silently stop accepting a dependency that was
#: renamed. Naming the set in one place means adding a third way to authenticate is a
#: deliberate edit to this tuple, which is exactly the review the addition deserves.
AUTH_DEPENDENCY_QUALNAMES: Final[tuple[str, ...]] = (
    "require_principal",
    "require_role.<locals>.dependency",
    "require_mcp_principal",
)


async def require_principal(request: Request) -> Principal:
    """Resolve and return the verified caller, or raise 401.

    Caches the principal on `request.state` so two dependencies on one route — say
    `require_principal` and a `require_permission` that also needs it — verify the
    token once. Verification is a JWKS lookup plus a signature check; doing it twice
    per request is measurable, and more importantly two verifications could in
    principle disagree if the JWKS rotated between them.
    """
    cached = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
    if isinstance(cached, Principal):
        return cached

    verifier = getattr(request.app.state, "app_token_verifier", None)
    if verifier is None:
        # Deliberately NOT a 401, and deliberately not an RFC 9457 problem either.
        # A missing verifier is a composition error in the app factory, not a fact
        # about the caller: reporting it as "unauthenticated" would let a broken
        # deployment look like a wall of correctly-rejected clients, which is the
        # failure mode D-23 is a case study in. Appendix C.1 registers no
        # `internal-error` type and inventing one at a raise site is what the registry
        # exists to prevent, so this is a `RuntimeError` — a 500 with a stack trace in
        # the server log, which is what a wiring bug deserves. The wiring test in
        # §0.4.1 is what stops it reaching a deployment.
        raise RuntimeError(
            "app.state.app_token_verifier is not composed; every non-public route "
            "depends on it (design §11.2). create_app() must build it in the lifespan."
        )

    principal = await verifier.verify_principal(request.headers.get("authorization"))
    setattr(request.state, PRINCIPAL_STATE_ATTR, principal)
    return principal


def require_role(*allowed: UserRole) -> Callable[[Request], Awaitable[Principal]]:
    """Coarse role gate for routes whose authorisation needs no resource attributes.

    Returns 403 with the fixed `forbidden` body, not a message naming the required
    role. A caller who learns "you need admin" has learned the shape of the
    authorisation model for free, and §4.2 already requires the 403 body to be
    byte-identical whether or not the resource exists — a role-specific detail here
    would reintroduce exactly the distinction that rule removes.

    Roles are coarse and static. Anything that depends on the resource belongs in
    Cerbos (§11.2), and keeping that split explicit is what stops role checks creeping
    into handlers.
    """
    if not allowed:
        raise ValueError(
            "require_role() with no roles would admit nobody, which is never the "
            "intent; use require_principal for 'any authenticated caller'."
        )
    permitted = frozenset(allowed)

    async def dependency(request: Request) -> Principal:
        principal = await require_principal(request)
        if principal.role not in permitted:
            raise forbidden_problem()
        return principal

    return dependency


def route_requires_principal(path: str, methods: set[str] | frozenset[str]) -> bool:
    """Whether a route must carry the dependency, given its path and methods.

    Shared by the dependency's own tests and by `scripts/check-route-auth.py`, so the
    checker and the runtime cannot disagree about what "public" means.
    """
    return not all(is_public(path, method) for method in methods if method != "HEAD")


async def require_mcp_principal(request: Request) -> Principal:
    """Resolve a principal from the MCP gateway's OWN verification.

    §4.4 is explicit that `/api/v1/mcp*` and `/api/v1/ai/complete` "keep their Phase 0
    OIDC verification and additionally now resolve a principal ... **without changing
    its token contract**". That rules out `require_principal` on those routes: the app
    verifier requires the product audience, so attaching it would reject every existing
    gateway token — RBAC gained by breaking the surface it was gaining RBAC for.

    So the gateway audience is verified, and the principal is derived from those claims.
    The role comes from the same `forgeops_role` claim; a machine token that carries no
    role resolves to `viewer`, which is the one place a default role is correct: a
    machine client's authority is bounded by the gateway's Rego policy and its blast
    radius, and refusing the request outright would break Phase 0's clients for a claim
    Phase 0 never minted. It cannot mutate anything — `read_only` is the resulting
    blast radius, and every mutating path requires a minted authority regardless.
    """
    cached = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
    if isinstance(cached, Principal):
        return cached

    verifier = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        # Fall back to the concrete name. `token_verifier` is an ALIAS the app factory
        # sets so this dependency need not know it is the MCP verifier — if the two
        # surfaces ever diverge, only that one line changes. But a test graph, or any
        # composition predating the alias, sets `mcp_verifier` only, and failing there
        # would be this dependency insisting on a name rather than on a capability.
        verifier = getattr(request.app.state, "mcp_verifier", None)
    if verifier is None:
        raise RuntimeError(
            "neither app.state.token_verifier nor app.state.mcp_verifier is composed; "
            "the MCP surface depends on one of them (design §4.4, §11.2)."
        )

    claims = await verifier.verify(request.headers.get("authorization"))
    raw = getattr(claims, "raw", {}) or {}
    role_claim = raw.get("forgeops_role")
    try:
        role = UserRole(role_claim) if isinstance(role_claim, str) else UserRole.VIEWER
    except ValueError:
        role = UserRole.VIEWER

    principal = Principal.for_user(
        user_id=_subject_uuid(claims.sub),
        subject=claims.sub,
        email=str(raw.get("email") or ""),
        role=role,
    )
    setattr(request.state, PRINCIPAL_STATE_ATTR, principal)
    return principal


def _subject_uuid(subject: str) -> uuid.UUID:
    """A stable UUID for a machine subject that is not itself a UUID.

    Derived with UUID5 over a fixed namespace, so the same subject always yields the
    same id and the audit log can join on it. Not a random UUID: a fresh id per request
    would make one client look like thousands of actors.
    """
    try:
        return uuid.UUID(subject)
    except ValueError:
        return uuid.uuid5(MCP_SUBJECT_NAMESPACE, subject)


#: Fixed namespace for UUID5 derivation of machine subjects. A constant, so the mapping
#: from subject to id is stable across processes and releases.
MCP_SUBJECT_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f2a1c3e-0b7d-5e4a-9c81-2f6d4b8a1e05")
