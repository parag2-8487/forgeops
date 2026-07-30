# SPDX-License-Identifier: FSL-1.1-ALv2
"""The exhaustive public-route set (design.md §4.4, Q-19).

Everything not listed here requires a verified principal. The list is committed here
in code, not described in a document, because `scripts/check-route-auth.py` asserts it
against the real router — so the allowlist cannot drift from what is actually served.

Each entry needs a reason on the record, and the two that are not obvious are:

* the auth flow itself, because the endpoints that *create* a principal cannot require
  one;
* `POST /api/v1/agents/pair/exchange`, because the agent has no credential yet — this
  is the exchange that gives it one. It is protected instead by single-use codes, a
  five-attempt cap, per-IP and global rate limits, and five-minute expiry (§10.3).

Adding an entry here is the one way to make a route unauthenticated, which makes it a
deliberate, reviewable act rather than an omission.
"""

from __future__ import annotations

from typing import Final, NamedTuple


class PublicRoute(NamedTuple):
    """A route that serves without a principal, and why.

    `arrives_in` names the task that adds the route, for entries whose route does not
    exist yet. §4.4's set is committed whole in this leaf, but four of the paths are
    served by later leaves, so without the marker the checker would have to choose
    between failing on every one of them or dropping its staleness rule — and dropping
    it would leave a renamed route's exemption lying around, ready to apply to whatever
    takes the old path next.

    The marker is self-clearing: once the route IS served, the checker fails until
    `arrives_in` is removed. That is what stops it becoming a permanent excuse, which
    is the way this kind of escape hatch normally rots.
    """

    path: str
    methods: frozenset[str]
    reason: str
    arrives_in: str | None = None


#: §4.4's table, verbatim. The tuple is ordered as the design lists it so a diff
#: against the design is a straight read.
PUBLIC_ROUTES: Final[tuple[PublicRoute, ...]] = (
    PublicRoute("/health", frozenset({"GET"}), "Container liveness contract (Phase 0 §4.4)"),
    PublicRoute("/health/ready", frozenset({"GET"}), "Orchestrator readiness contract"),
    PublicRoute("/api/v1/health", frozenset({"GET"}), "Versioned informational echo"),
    PublicRoute(
        "/api/v1/openapi.json",
        frozenset({"GET"}),
        "Schema document; contains no data",
    ),
    PublicRoute("/api/v1/docs", frozenset({"GET"}), "Documentation UI; contains no data"),
    PublicRoute(
        "/api/v1/auth/login",
        frozenset({"GET", "POST"}),
        "The flow that creates a principal cannot require one",
    ),
    PublicRoute(
        "/api/v1/auth/callback",
        frozenset({"GET", "POST"}),
        "The flow that creates a principal cannot require one",
    ),
    PublicRoute(
        "/api/v1/auth/refresh",
        frozenset({"POST"}),
        "Presents a refresh token, not an access token",
    ),
    PublicRoute(
        "/api/v1/auth/logout",
        frozenset({"GET", "POST"}),
        "Must succeed even when the access token has already expired",
    ),
    PublicRoute(
        "/api/v1/agents/pair/exchange",
        frozenset({"POST"}),
        "The agent has no credential yet; protected by single-use codes, a 5-attempt "
        "cap, per-IP and global rate limits, and 5-minute expiry (§10.3)",
        arrives_in="task 8.1",
    ),
)

#: Paths only, for the O(1) membership test the dependency and the checker both need.
PUBLIC_PATHS: Final[frozenset[str]] = frozenset(route.path for route in PUBLIC_ROUTES)

#: The subset whose route a later leaf still has to add.
STAGED_PATHS: Final[frozenset[str]] = frozenset(route.path for route in PUBLIC_ROUTES if route.arrives_in)

#: Documentation and schema paths FastAPI mounts itself. They are in `PUBLIC_ROUTES`
#: above under the versioned prefix; these are the unversioned defaults, which
#: `create_app` disables. Listed so the checker can explain itself if they reappear.
FASTAPI_DEFAULT_DOC_PATHS: Final[frozenset[str]] = frozenset(
    {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
)


def is_public(path: str, method: str) -> bool:
    """True when `path` serves `method` without a principal.

    Matches on the exact path and the exact method. A prefix match was rejected: it
    would make `/api/v1/auth/login/../../projects` a judgement call, and more
    importantly it would silently make every future route under a public prefix public
    too.
    """
    upper = method.upper()
    for route in PUBLIC_ROUTES:
        if route.path == path and upper in route.methods:
            return True
    return False
