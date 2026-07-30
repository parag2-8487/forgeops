#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every route either depends on `require_principal` or is in `PUBLIC_ROUTES`.

design.md §4.4, §11.2, §14.1; Q-19; tasks.md leaf 6.1.

Deny-by-default is attached **per route** rather than globally, because a global
dependency would have to carve out the public set by path matching — and a path matcher
is where an unauthenticated route hides: it is invisible in the route definition, so a
reviewer reading a handler cannot tell whether it is protected. The cost of per-route
attachment is that a new router can forget it. This is the check that pays that cost.

It enumerates `create_app().routes` — the same callable uvicorn runs, not a
hand-maintained list — and fails when a route lacks the dependency and is not public.
That is the difference between asserting the router and asserting a document about the
router, which is the mistake Phase 0's Appendix E made about CI jobs (§8.3).

Usage:
    check-route-auth.py [--app src.main:create_app]

Exit 0 when every route is accounted for, 1 when any route is unprotected or the
public set names a path the router does not serve, and 2 when the app cannot be built
— never a silent pass.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Iterable
from typing import Any

#: Starlette mounts these itself and they carry no application data.
INFRASTRUCTURE_PATHS = frozenset({"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"})


def _load_factory(spec: str) -> Any:
    module_name, _, attribute = spec.partition(":")
    if not attribute:
        # `ValueError`, not `SystemExit`. `SystemExit` derives from `BaseException`, so
        # `main()`'s `except Exception` would not catch it and the exit code would be
        # 1 instead of the 2 this check promises for "could not build the app".
        raise ValueError(f"--app must be 'module:callable', got {spec!r}")
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


def _flatten(routes: Iterable[Any], prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Yield `(effective_path, route)` for every real route, descending into includes.

    FastAPI 0.139 does not flatten `include_router` into `app.routes`: each inclusion
    appears as one opaque `_IncludedRouter` holding the real router. Walking only the
    top level therefore sees the three health endpoints and nothing else — the checker
    would have reported success while examining none of the API surface, which is the
    vacuity failure it exists to prevent. Discovered by printing `type(r).__name__` for
    every route rather than trusting the shape, and the reason `check()` asserts a
    non-zero examined count as well.

    The descent is duck-typed rather than importing the private class: `_IncludedRouter`
    is private API, and a name that changes should degrade to "found no sub-routes",
    which the examined-count assertion then turns into a loud failure.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            sub_prefix = getattr(context, "prefix", "") or ""
            yield from _flatten(getattr(included, "routes", []), prefix + sub_prefix)
            continue
        yield prefix, route


def _dependency_names(route: Any) -> set[str]:
    """Every callable named in a route's dependency tree, by qualified name.

    Walks `dependant.dependencies` recursively rather than only the top level, because
    a router-level dependency and a sub-dependency both count: what matters is whether
    `require_principal` runs before the handler, not where it was attached.
    """
    names: set[str] = set()
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return names

    stack = [dependant]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        call = getattr(current, "call", None)
        if call is not None:
            names.add(getattr(call, "__qualname__", "") or getattr(call, "__name__", ""))
            # A dependency built by a factory (`require_role(...)`) is a closure, so
            # its own name is useless. Record the enclosing function too.
            module = getattr(call, "__module__", "")
            if module:
                names.add(f"{module}.{getattr(call, '__qualname__', '')}")
        stack.extend(getattr(current, "dependencies", []) or [])
    return names


def _protects(names: Iterable[str]) -> bool:
    """True when any named callable is one of the committed auth dependencies.

    Matched against `AUTH_DEPENDENCY_QUALNAMES` from `src/auth/dependencies.py` rather
    than by substring: a substring test would accept any function whose name happened
    to contain `require_`, and would silently stop accepting a dependency that was
    renamed. Importing the tuple means the checker and the runtime cannot disagree about
    what counts as protection.
    """
    from src.auth.dependencies import AUTH_DEPENDENCY_QUALNAMES

    for name in names:
        for accepted in AUTH_DEPENDENCY_QUALNAMES:
            if name == accepted or name.endswith(f".{accepted}"):
                return True
    return False


def check(factory_spec: str = "src.main:create_app") -> list[str]:
    from src.auth.dependencies import route_requires_principal
    from src.auth.public_routes import PUBLIC_ROUTES, STAGED_PATHS

    factory = _load_factory(factory_spec)
    app = factory()

    failures: list[str] = []
    served_paths: set[str] = set()
    examined = 0

    for prefix, route in _flatten(app.routes):
        raw_path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        path = None if raw_path is None else f"{prefix}{raw_path}"
        if path is None or methods is None:
            # Mounts, WebSocket routes and static files have no method set. A
            # WebSocket route authenticates inside the handshake (§7.3), which this
            # check cannot see, so it is reported rather than silently passed.
            if path is not None and getattr(route, "endpoint", None) is not None:
                served_paths.add(path)
            continue

        served_paths.add(path)
        if path in INFRASTRUCTURE_PATHS:
            continue

        examined += 1
        if not route_requires_principal(path, set(methods)):
            continue

        names = _dependency_names(route)
        if not _protects(names):
            failures.append(
                f"{sorted(m for m in methods if m != 'HEAD')} {path} has no "
                f"require_principal/require_role dependency and is not in "
                f"PUBLIC_ROUTES. Attach the dependency to its router, or add the path "
                f"to src/auth/public_routes.py with a reason (design §4.4)."
            )

    if examined == 0:
        failures.append(
            "no routes were examined at all. An empty inventory would make this check "
            "pass vacuously, which is the exact failure it exists to prevent."
        )

    # The allowlist must not name a path the router does not serve: a stale entry is a
    # route that was renamed while its public exemption stayed behind, ready to apply
    # to whatever takes the old path next. An entry whose route a later leaf adds is
    # marked `arrives_in=...` and reported rather than failed — and the marker is
    # self-clearing, because once the route appears the check fails until it is removed.
    for route_spec in PUBLIC_ROUTES:
        served = route_spec.path in served_paths
        if not served and route_spec.arrives_in is None:
            failures.append(
                f"PUBLIC_ROUTES names {route_spec.path!r}, which the router does not "
                f"serve. Remove the entry, or restore the route. A stale exemption "
                f"applies to whatever takes the path next."
            )
        elif served and route_spec.arrives_in is not None:
            failures.append(
                f"PUBLIC_ROUTES marks {route_spec.path!r} as arriving in "
                f"{route_spec.arrives_in}, but the router serves it now. Remove the "
                f"`arrives_in` marker so the staleness rule applies to it again."
            )

    staged = sorted(path for path in STAGED_PATHS if path not in served_paths)
    if staged:
        print(f"staged (route owned by a later task, not yet served): {len(staged)}")
        for path in staged:
            owner = next(r.arrives_in for r in PUBLIC_ROUTES if r.path == path)
            print(f"  {path}  <- {owner}")

    print(f"check-route-auth: examined {examined} route(s) across {len(served_paths)} path(s)")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", default="src.main:create_app")
    args = parser.parse_args()

    try:
        failures = check(args.app)
    except Exception as exc:  # noqa: BLE001 - any build failure must be exit 2, not 0
        print(f"FAIL: could not build the app from {args.app!r}: {exc!r}", file=sys.stderr)
        return 2

    if failures:
        print("FAIL: deny-by-default routing is incomplete:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("OK: every route requires a principal or is an explicit PUBLIC_ROUTES entry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
