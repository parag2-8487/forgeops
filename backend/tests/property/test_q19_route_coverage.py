# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-19 — deny-by-default route coverage (design.md §4.4, §11.2, Appendix B).

Property, universally quantified:

    For every route registered by `create_app()`, the route either depends on
    `require_principal` (or another committed auth dependency) or is a member of
    `PUBLIC_ROUTES`; and for every request carrying no usable token to a non-public
    route, the response is 401 and **no handler body executes**.

Why this is a property and not an example
-----------------------------------------
The failure mode is not "one route is unprotected". It is "the protection is applied by
something other than the route definition", which a fixture over a handful of paths cannot
distinguish from real coverage: any finite set of examples is satisfied by a global
middleware that happens to match those paths. Quantifying over *every registered route* and
over *every shape of missing credential* removes that. `scripts/check-route-auth.py` asserts
the structural half in CI; this file asserts it again over the same router — through the
same code, loaded from the same file — and adds the behavioural half the script cannot see.

"No handler body executes" is measured, not inferred
----------------------------------------------------
A 401 body proves the response was not the handler's, not that the handler never ran: a
handler that read a database and then raised would look identical. So the endpoint
functions' code objects are watched with `sys.monitoring`, and the test asserts nothing in
that set started. `TestTheRecorderCanSeeExecution` proves the watcher is not blind — an
execution detector that never fires would make the strongest assertion here vacuous, which
is the §0.4.5 trap and the reason this file carries a self-test at all.

Negative control (`mutations.toml` Q-19): drop the `require_principal` dependency from
`GET /api/v1/projects` without adding that path to `PUBLIC_ROUTES`. The property must fail.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests import synthetic_secrets

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPO_ROOT / "scripts" / "check-route-auth.py"


@lru_cache(maxsize=1)
def checker() -> ModuleType:
    """`scripts/check-route-auth.py`, loaded from its path.

    Loaded rather than reimplemented, and loaded rather than imported by name: the file
    name contains hyphens, so it is not an importable module. Using the CI gate's own
    route walker means this property and the gate cannot disagree about what a route is —
    and a route walker that stopped descending into `include_router` is exactly the
    vacuity bug the gate's own docstring records finding.
    """
    spec = importlib.util.spec_from_file_location("forgeops_check_route_auth", CHECKER_PATH)
    assert spec is not None and spec.loader is not None, CHECKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Credential shapes that must all be refused. Not just "no header": the interesting
#: failures are the ones where something *is* presented — a scheme the verifier does not
#: accept, an empty bearer, a bearer holding something that is not a JWT — because a
#: verifier that returned a principal for any of these would be worse than one that merely
#: required a header.
#:
#: **Assembled at runtime, and that is not cosmetic.** Written inline, these were
#: `Bearer …`, `Basic <base64>` and an `eyJ`-prefixed JWT literal — three of the exact
#: patterns `.antigravity/steering/secret-safety.md` lists as high-risk, and the pre-push diff grep
#: fired on them. None was ever a usable credential, but a scanner cannot tell, and
#: `tests/synthetic_secrets.py` already records a real GitGuardian incident raised against a
#: JWT-shaped placeholder in this repository. The bytes sent on the wire are identical; only
#: the source no longer contains a contiguous credential-shaped string.
def _tokenless_headers() -> tuple[tuple[tuple[str, str], ...], ...]:
    return (
        (),
        (("Authorization", ""),),
        (("Authorization", synthetic_secrets.bearer_with("")),),
        (("Authorization", synthetic_secrets.bearer_with("") + " "),),
        (("Authorization", synthetic_secrets.bearer_clause()),),
        (("Authorization", synthetic_secrets.basic_clause()),),
        (("Authorization", "token " + synthetic_secrets.SYNTHETIC_MARKER),),
        # Lower-cased scheme and header name: HTTP header names are case-insensitive and
        # the scheme comparison must be too, so a verifier that only matched `Bearer`
        # exactly would accept this as "no scheme" and take a different path.
        (("authorization", synthetic_secrets.bearer_with("").lower() + " " + synthetic_secrets.SYNTHETIC_MARKER),),
        # A structurally valid JWT with `alg: none` and no signature. The verifier must
        # reject it on the algorithm allowlist, not decode it and trust the claims.
        (("Authorization", synthetic_secrets.bearer_with(synthetic_secrets.unsigned_jwt())),),
    )


TOKENLESS_HEADERS: tuple[tuple[tuple[str, str], ...], ...] = _tokenless_headers()


@contextmanager
def watching(code_objects: Iterable[CodeType]) -> Iterator[set[str]]:
    """Record which of `code_objects` start executing, using `sys.monitoring`.

    `sys.monitoring` with per-code *local* events rather than `sys.settrace`: settrace is
    global, would replace whatever pytest-cov installed, and slows every frame in the
    process. Local events attach to exactly the code objects under test and cost nothing
    elsewhere.

    A tool id already in use is reported, never worked around. Silently degrading to
    "recorded nothing" would turn the strongest assertion in this file into a tautology.
    """
    monitoring = sys.monitoring
    watched = {code: code.co_qualname for code in code_objects}
    started: set[str] = set()

    def on_start(code: CodeType, _offset: int) -> Any:
        name = watched.get(code)
        if name is not None:
            started.add(name)
        return None

    tool_id = monitoring.DEBUGGER_ID
    monitoring.use_tool_id(tool_id, "forgeops-q19")
    try:
        monitoring.register_callback(tool_id, monitoring.events.PY_START, on_start)
        for code in watched:
            monitoring.set_local_events(tool_id, code, monitoring.events.PY_START)
        try:
            yield started
        finally:
            for code in watched:
                monitoring.set_local_events(tool_id, code, 0)
            monitoring.register_callback(tool_id, monitoring.events.PY_START, None)
    finally:
        monitoring.free_tool_id(tool_id)


def _concrete_path(path: str) -> str:
    """A requestable path, with every `{param}` replaced by a plausible value.

    A UUID for every parameter, because every path parameter in Phase 1's surface is an
    identifier. The value is never expected to exist: an unauthenticated request must be
    refused before anything looks it up, which is part of what this property asserts.
    """
    out = path
    while "{" in out and "}" in out:
        start = out.index("{")
        end = out.index("}", start)
        out = out[:start] + "00000000-0000-4000-8000-000000000000" + out[end + 1 :]
    return out


class RouteUnderTest:
    """One (method, path) pair from the real router, with its endpoint's code object."""

    __slots__ = ("code", "guard", "method", "path", "public", "raw_path")

    def __init__(self, *, method: str, raw_path: str, public: bool, code: CodeType | None, guard: str) -> None:
        self.method = method
        self.raw_path = raw_path
        self.path = _concrete_path(raw_path)
        self.public = public
        self.code = code
        #: `"app"` or `"mcp"` — which authentication family refuses this route. §4.4 keeps
        #: `/api/v1/mcp*` and `/api/v1/ai/*` on Phase 0's token contract "without changing
        #: it", so those routes answer the `mcp-*` problem types rather than
        #: `unauthenticated`. Two families is the design, not an inconsistency, and this
        #: property asserts each route answers with ITS family rather than accepting either.
        self.guard = guard

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"<{self.method} {self.raw_path} public={self.public} guard={self.guard}>"


@lru_cache(maxsize=1)
def expected_suffixes() -> dict[str, frozenset[str]]:
    """The registered problem-type suffixes each family may answer with.

    Read out of the verifiers rather than restated, so a renamed type breaks here instead
    of quietly widening what this property accepts.
    """
    from src.auth.verifier import AppTokenVerifier
    from src.core.security import OidcTokenVerifier

    return {
        "app": frozenset(AppTokenVerifier.problem_types.values()),
        "mcp": frozenset(OidcTokenVerifier.problem_types.values()),
    }


def _guard_family(dependency_names: set[str]) -> str:
    for name in dependency_names:
        if name == "require_mcp_principal" or name.endswith(".require_mcp_principal"):
            return "mcp"
    return "app"


@contextmanager
def _temporary_environment() -> Iterator[None]:
    """Mutate `os.environ` for the duration of composition, then put it back.

    Held around **both** `create_app()` and the lifespan entry, because the factory reads
    configuration through pydantic-settings and the lifespan builds the engine and Redis
    client from it. Restoring in between would compose the app against the developer's real
    configuration, which is the opposite of the point.

    Self-contained on purpose rather than left to a module fixture. `test_q20_rbac_confinement.py`
    imports `built_app` to enumerate the real router, and a module-scoped guard in *this* file
    does not protect *that* module — which is precisely what happened: six `test_config*.py`
    assertions about production settings ran against a `test` environment and failed, while
    passing in isolation. Containing the mutation where it is made means no importer can leak
    it, however it is reached.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
        from src.core.config import get_settings

        if hasattr(get_settings, "cache_clear"):
            # The cached Settings was built from the mutated environment. Leaving it cached
            # would hand the next module a configuration it never asked for — the same defect
            # one layer along.
            get_settings.cache_clear()


@lru_cache(maxsize=1)
def _composed() -> tuple[Any, Any]:
    """The app and a client over it, composed once under a temporary environment."""
    from fastapi.testclient import TestClient

    with _temporary_environment():
        app = _build_app()
        client = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)
        # Entered here — `__enter__` is what runs the lifespan — because a non-public route
        # that answered 500 for a missing verifier would satisfy no clause of this property,
        # and the composition error is a wiring bug rather than deny-by-default behaviour.
        client.__enter__()
    return app, client


def built_app() -> Any:
    """The app `create_app()` builds — the same callable uvicorn runs.

    Infrastructure is pointed at closed loopback ports rather than substituted. §4.4's
    lifespan is non-destructive by contract: it validates local configuration and performs
    no mandatory network handshake, so an unreachable Postgres and Redis change *readiness*,
    not startup. That gives a fully composed app — including the real `AppTokenVerifier` —
    with no database, which is exactly the right shape for this property: every non-public
    route must be refused before it could touch one.

    Composing `app.state.app_token_verifier` by hand was rejected. It would be the one
    collaborator this property is really about, hand-placed by the test, and a route that
    lost its dependency would then be indistinguishable from one whose dependency worked.
    """
    return _composed()[0]


def sync_client() -> Any:
    """A synchronous client over the real ASGI app, with the real lifespan started.

    `TestClient` rather than `httpx.ASGITransport`: the transport is async-only, and these
    assertions are synchronous because Hypothesis drives them.

    `sys.monitoring` local events are process-wide rather than per-thread, so the recorder
    still sees the handler even though `TestClient` dispatches through an anyio portal
    thread. That is why `TestTheRecorderCanSeeExecution` exercises a real route through this
    same client rather than only a plain function.
    """
    return _composed()[1]


def _build_app() -> Any:
    """`create_app()` with infrastructure pointed at closed ports. Call under the guard."""
    import socket
    from contextlib import closing

    from src.core.config import get_settings, load_project_dotenv
    from src.main import create_app

    def closed_port() -> int:
        with closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    # The committed baseline supplies the `${VAR}` expansions `load_tier_config` refuses to
    # leave unexpanded, so this is a CONFIGURATION substitution and simultaneously asserts
    # `.env.example` is complete enough to boot the app.
    for key, value in load_project_dotenv((".env.example",)).items():
        os.environ.setdefault(key, value)
    os.environ["APP_ENV"] = "test"
    os.environ["DATABASE_URL"] = f"postgresql+asyncpg://forgeops:pw@127.0.0.1:{closed_port()}/forgeops"
    os.environ["REDIS_URL"] = f"redis://127.0.0.1:{closed_port()}/0"
    os.environ.setdefault("ENVELOPE_PEPPER", "test-only-not-a-real-secret-pepper")

    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()
    return create_app()


@lru_cache(maxsize=1)
def discovered_routes() -> tuple[RouteUnderTest, ...]:
    from src.auth.dependencies import route_requires_principal

    module = checker()
    out: list[RouteUnderTest] = []
    for prefix, route in module._flatten(built_app().routes):  # noqa: SLF001 - the gate's own walker
        raw_path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if raw_path is None or methods is None:
            continue
        path = f"{prefix}{raw_path}"
        if path in module.INFRASTRUCTURE_PATHS:
            continue
        endpoint = getattr(route, "endpoint", None)
        code = getattr(endpoint, "__code__", None)
        guard = _guard_family(module._dependency_names(route))  # noqa: SLF001
        for method in sorted(m for m in methods if m != "HEAD"):
            out.append(
                RouteUnderTest(
                    method=method,
                    raw_path=path,
                    public=not route_requires_principal(path, {method}),
                    code=code,
                    guard=guard,
                )
            )
    return tuple(out)


def protected_routes() -> tuple[RouteUnderTest, ...]:
    return tuple(route for route in discovered_routes() if not route.public)


def _classify_offenders(*, want_protected: bool) -> list[str]:
    """Routes whose attachment disagrees with their public/protected classification."""
    module = checker()
    offenders: list[str] = []
    examined = 0
    for prefix, route in module._flatten(built_app().routes):  # noqa: SLF001
        raw_path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if raw_path is None or methods is None:
            continue
        path = f"{prefix}{raw_path}"
        if path in module.INFRASTRUCTURE_PATHS:
            continue
        examined += 1
        from src.auth.dependencies import route_requires_principal

        is_protected = route_requires_principal(path, set(methods))
        if is_protected is not want_protected:
            continue
        attached = module._protects(module._dependency_names(route))  # noqa: SLF001
        if attached is not want_protected:
            offenders.append(f"{sorted(m for m in methods if m != 'HEAD')} {path}")
    assert examined > 0, "no routes were examined; this assertion would be vacuous"
    return offenders


class TestTheInventoryIsRealBeforeAnythingIsAsserted:
    def test_routes_were_discovered_at_all(self) -> None:
        assert discovered_routes(), "no routes discovered; every assertion below is vacuous"

    def test_both_kinds_of_route_are_present(self) -> None:
        """A router with only public routes, or only protected ones, would leave one half of
        the property untested while the run stayed green."""
        routes = discovered_routes()
        assert any(route.public for route in routes), routes
        assert any(not route.public for route in routes), routes

    def test_the_checker_was_loaded_from_its_real_path(self) -> None:
        module = checker()
        assert CHECKER_PATH.is_file(), CHECKER_PATH
        assert callable(module._flatten)  # noqa: SLF001
        assert callable(module._protects)  # noqa: SLF001

    def test_both_authentication_families_are_represented(self) -> None:
        """§4.4's split is deliberate, so both sides must be exercised. If the MCP family
        vanished from the router, the branch that accepts `mcp-*` types would stop being
        tested and would silently widen what a future route may answer with."""
        families = {route.guard for route in discovered_routes() if not route.public}
        assert families == {"app", "mcp"}, families

    def test_the_two_families_answer_disjoint_problem_types(self) -> None:
        """If they overlapped, asserting "its own family" would prove nothing."""
        suffixes = expected_suffixes()
        assert not suffixes["app"] & suffixes["mcp"], suffixes


class TestTheStructuralHalf:
    def test_every_protected_route_carries_an_auth_dependency(self) -> None:
        """Restated over the same router the CI gate reads, so this property still fails on
        its own if the script is ever dropped from the pipeline."""
        assert not _classify_offenders(want_protected=True)

    def test_no_public_route_carries_an_auth_dependency(self) -> None:
        """A route cannot be both exempt and protected; one of the two statements is wrong,
        and an exemption that is also enforced hides which."""
        assert not _classify_offenders(want_protected=False)


class TestTheRecorderCanSeeExecution:
    """The vacuity guard for the strongest assertion in this file."""

    def test_a_function_that_runs_is_recorded(self) -> None:
        def ran() -> int:
            return 1

        with watching([ran.__code__]) as started:
            ran()
        assert any(name.endswith("ran") for name in started), started

    def test_a_function_that_does_not_run_is_not_recorded(self) -> None:
        def never() -> int:  # pragma: no cover - deliberately not called
            return 1

        with watching([never.__code__]) as started:
            pass
        assert not started, started

    def test_a_real_public_handler_is_recorded_when_called(self) -> None:
        """End of the argument: the recorder sees a REAL route handler execute through the
        real ASGI stack. Without this, "no handler executed" could mean "the recorder does
        not work on route handlers", which is the shape of the P-09 defect exactly."""
        health = next(r for r in discovered_routes() if r.raw_path == "/health" and r.method == "GET")
        assert health.code is not None
        with watching([health.code]) as started:
            assert sync_client().get("/health").status_code == 200
        assert started, "the recorder did not see a real handler that definitely ran"


_ROUTE_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class TestEveryProtectedRouteRefusesEveryTokenlessRequest:
    @_ROUTE_SETTINGS
    @given(
        index=st.integers(min_value=0, max_value=1_000_000),
        headers=st.sampled_from(TOKENLESS_HEADERS),
        query=st.text(max_size=24),
    )
    def test_the_answer_is_401_and_the_handler_never_ran(
        self, index: int, headers: tuple[tuple[str, str], ...], query: str
    ) -> None:
        candidates = protected_routes()
        assert candidates, "no non-public routes; this property would be vacuous"
        route = candidates[index % len(candidates)]
        watch = [route.code] if route.code is not None else []

        with watching(watch) as started:
            response = sync_client().request(
                route.method,
                route.path,
                headers=dict(headers),
                params={"q": query} if query else None,
            )

        assert response.status_code == 401, (
            f"{route.method} {route.raw_path} answered {response.status_code} to a request "
            f"with headers {dict(headers)}; deny-by-default requires 401. "
            f"Body: {response.text[:200]}"
        )
        suffix = response.json()["type"].rsplit("/", 1)[-1]
        allowed = expected_suffixes()[route.guard]
        assert suffix in allowed, (
            f"{route.method} {route.raw_path} is guarded by the {route.guard!r} family, which "
            f"answers one of {sorted(allowed)}; it answered {suffix!r}. §4.4 keeps the MCP "
            "surface on Phase 0's token contract, so the two families are deliberate — but a "
            "route must answer with its own."
        )
        assert not started, (
            f"{route.method} {route.raw_path} executed its handler {sorted(started)} for an "
            "unauthenticated request; the auth dependency must run before the handler"
        )
