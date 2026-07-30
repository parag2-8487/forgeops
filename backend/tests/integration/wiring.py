# SPDX-License-Identifier: FSL-1.1-ALv2
"""Wiring declarations and their collector (design.md §0.4.1).

`app.state` is the production composition's public surface. Every attribute the
lifespan puts there is a collaborator that some route will reach for at runtime,
so every one of them needs a test that drives it through the *real* object graph.
Phase 0 shipped 419 green tests over an MCP gateway that raised `TypeError` on
every request precisely because that was not true of `app.state.mcp_gateway`
(D-23).

A hand-maintained list of "components that have a wiring test" would rot on the
first refactor, so the relationship is declared at the test and collected
mechanically:

    @wires("mcp_gateway", "mcp_registry")
    class TestToolsListThroughTheRealGraph: ...

`test_wiring_coverage.py` then compares the declared set against the attributes
the real lifespan actually composed, and fails on anything composed but never
declared. The failure direction is deliberate: a *new* component arriving without
a wiring test breaks the build, while a stale declaration for a component that no
longer exists does not — the latter is harmless noise, the former is the Phase 0
defect.

The collector parses with `ast` and never imports the test modules. Importing them
would execute module-level code and make the meta-check depend on the very
collaborators it is auditing; parsing cannot.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

__all__ = ["WIRES_ATTR", "collect_wiring_declarations", "wires"]

#: Attribute stamped onto the decorated object. Read by nothing at runtime — the
#: collector works from source — but it makes the declaration visible to anyone
#: introspecting a live test object, and it keeps the decorator honest by giving
#: it an observable effect.
WIRES_ATTR = "__forgeops_wires__"

T = TypeVar("T")


def wires(*state_attributes: str) -> Callable[[T], T]:
    """Declare which `app.state` attributes this test drives through real wiring.

    Names are the `app.state` attribute names exactly as `create_app()`'s lifespan
    assigns them, e.g. `"mcp_gateway"`, not the class name.
    """
    if not state_attributes:
        raise ValueError("@wires requires at least one app.state attribute name")
    for name in state_attributes:
        if not isinstance(name, str) or not name:
            raise TypeError(f"@wires expects non-empty attribute names, got {name!r}")

    def decorate(target: T) -> T:
        existing: tuple[str, ...] = tuple(getattr(target, WIRES_ATTR, ()))
        setattr(target, WIRES_ATTR, existing + tuple(state_attributes))
        return target

    return decorate


def _decorator_name(node: ast.expr) -> str | None:
    """Resolve `wires`, `wiring.wires` or `x.y.wires` to its final attribute."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _declared_in(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if _decorator_name(decorator.func) != wires.__name__:
                continue
            for arg in decorator.args:
                # Only literal strings count. A computed name could not be
                # resolved without importing, and an unresolvable declaration
                # that silently counted as coverage would reintroduce exactly
                # the vacuity this clause exists to prevent.
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)
    return found


def collect_wiring_declarations(root: Path) -> set[str]:
    """Every `app.state` name declared by a `@wires(...)` under `root`.

    Parses every `.py` file beneath `root`. A file that does not parse is a hard
    error: silently skipping it would drop its declarations and could only ever
    make the coverage check weaker.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"wiring declaration root does not exist: {root}")

    declared: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - a broken test tree
            raise SyntaxError(f"cannot parse {path}: {exc}") from exc
        declared |= _declared_in(tree)
    return declared
