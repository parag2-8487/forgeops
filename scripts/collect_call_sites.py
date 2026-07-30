#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Derive the cross-component call-site inventory by AST scan (design.md §0.4.2).

Why this is generated and never hand-written
--------------------------------------------
Phase 0's D-23 defect was a *caller/callee signature disagreement*: the MCP
gateway called `policy.filter_tools(server=…, tools=…, claims=…, blast_radius=…)`
while the real `OpaGatewayPolicy` implemented something else, and the test doubles
implemented the caller's shape because they reassigned a `spec=`'d child. Nothing
could see it, because collaborators arrive by constructor injection and the call
sites dispatch dynamically — there is no static edge for a type checker to follow.

`tests/unit/test_mcp_contract.py` fixed that for eleven hand-listed gateway call
sites. A hand-written list is exactly the wrong shape for a rule that must hold
for every future component, so this module derives the list instead.

What counts as a cross-component call site
-----------------------------------------
Two binding forms, both of which defeat static analysis and both of which the
Phase 0 defect used:

1. **Constructor injection.** `__init__(self, *, policy: OpaGatewayPolicy)` stores
   the parameter as `self._policy`, and some method later calls
   `self._policy.filter_tools(...)`. The annotation names the collaborator type.

2. **`app.state` reads.** A route does `request.app.state.mcp_gateway.handle(...)`.
   The type comes from what the composition root actually assigns, so
   `src/main.py` is parsed for `app.state.<name> = <ClassName>(...)` and the name
   resolved through that module's own imports. This keeps the mapping derived from
   the composition rather than restated beside it.

Only *project-owned* targets are reported. Binding a call against
`httpx.AsyncClient.post` would test httpx, not ForgeOps, and third-party
signatures are already pinned by the lockfile.

Invocation
----------
    python scripts/collect_call_sites.py            # human-readable listing
    python scripts/collect_call_sites.py --json     # machine-readable

Exit status is 1 when the inventory is empty, because a collector that silently
returns nothing would make `test_contract_conformance.py` vacuously green — the
same trap §0.4.4 and §0.4.5 close for the mandatory selection and the mutation
harness.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
SRC_ROOT = BACKEND_ROOT / "src"
COMPOSITION_ROOT = SRC_ROOT / "main.py"

#: Only these top-level packages are considered project-owned.
PROJECT_PACKAGE = "src"

#: Dunder and framework methods whose signatures are not a ForgeOps contract.
_UNINTERESTING_METHODS = frozenset({"__init__", "__call__"})


@dataclass(frozen=True)
class CallSite:
    """One cross-component call, resolved to the class that must accept it."""

    module: str
    line: int
    target_dotted: str
    method: str
    positional_count: int
    keywords: tuple[str, ...]
    has_star_args: bool
    has_star_kwargs: bool
    binding: str  # "ctor" or "app.state"

    def __str__(self) -> str:
        # ASCII only, deliberately. A check script must be able to print its own
        # findings on the console the developer actually has; a `…` in this string
        # raises UnicodeEncodeError on a cp1252 Windows terminal and the finding is
        # lost, which is worse than an ugly placeholder.
        kwargs = ", ".join(f"{k}=..." for k in self.keywords)
        args = ", ".join(["..."] * self.positional_count)
        shown = ", ".join(p for p in (args, kwargs) if p)
        return f"{self.module}:{self.line} {self.target_dotted}.{self.method}({shown})"

    def as_dict(self) -> dict[str, object]:
        return {
            "module": self.module,
            "line": self.line,
            "target": self.target_dotted,
            "method": self.method,
            "positional_count": self.positional_count,
            "keywords": list(self.keywords),
            "has_star_args": self.has_star_args,
            "has_star_kwargs": self.has_star_kwargs,
            "binding": self.binding,
        }

    def resolve_target(self) -> type:
        """Import and return the real class this call must bind against."""
        module_path, _, class_name = self.target_dotted.rpartition(".")
        module = importlib.import_module(module_path)
        target = getattr(module, class_name)
        if not isinstance(target, type):
            raise TypeError(f"{self.target_dotted} is not a class")
        return target


# ── import resolution ────────────────────────────────────────────────────────


def _module_name_for(path: Path) -> str:
    """`backend/src/mcp/gateway.py` → `src.mcp.gateway`."""
    rel = path.relative_to(BACKEND_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_map(tree: ast.Module, module_name: str) -> dict[str, str]:
    """Local name → dotted path, for project-owned imports only.

    Relative imports are resolved against `module_name`, because this codebase
    uses parent-relative imports on purpose (see the TID252 note in
    `backend/pyproject.toml`) and a collector that ignored them would miss most of
    the graph.
    """
    package_parts = module_name.split(".")[:-1]
    mapping: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == PROJECT_PACKAGE:
                    mapping[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - (node.level - 1)] if node.level > 1 else package_parts
                prefix = ".".join([*base, node.module] if node.module else base)
            else:
                prefix = node.module or ""
                if prefix.split(".")[0] != PROJECT_PACKAGE:
                    continue
            for alias in node.names:
                mapping[alias.asname or alias.name] = f"{prefix}.{alias.name}"
    return mapping


def _annotation_name(node: ast.expr | None) -> str | None:
    """Extract a bare class name from an annotation, unwrapping `X | None`."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # A string annotation, e.g. "OpaGatewayPolicy"; parse it.
        try:
            return _annotation_name(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return None
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        # `Foo | None` — take whichever side is not None.
        for side in (node.left, node.right):
            if isinstance(side, ast.Constant) and side.value is None:
                continue
            name = _annotation_name(side)
            if name and name != "None":
                return name
    if isinstance(node, ast.Subscript):
        return _annotation_name(node.value)
    return None


# ── composition-root map: app.state.<name> → dotted class ───────────────────


def _composition_state_types() -> dict[str, str]:
    """`{"mcp_gateway": "src.mcp.gateway.McpGateway", ...}` from `src/main.py`.

    Derived from the composition root so the mapping cannot drift from what the
    lifespan actually assigns.
    """
    source = COMPOSITION_ROOT.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(COMPOSITION_ROOT))
    imports = _import_map(tree, _module_name_for(COMPOSITION_ROOT))

    result: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            # Match `app.state.<name> = ...`
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "state"
                and isinstance(target.value.value, ast.Name)
            ):
                continue
            state_name = target.attr
            value = node.value
            if isinstance(value, ast.Call):
                ctor = value.func
                cls_name = ctor.id if isinstance(ctor, ast.Name) else getattr(ctor, "attr", None)
                if cls_name and cls_name in imports:
                    result[state_name] = imports[cls_name]
            elif isinstance(value, ast.Name) and value.id in imports:
                result[state_name] = imports[value.id]
    return result


# ── per-module scan ──────────────────────────────────────────────────────────


@dataclass
class _ClassScope:
    """Attribute → dotted collaborator type, for one class."""

    attr_types: dict[str, str] = field(default_factory=dict)


def _collect_ctor_bindings(cls: ast.ClassDef, imports: dict[str, str]) -> _ClassScope:
    """Map `self.<attr>` to a collaborator type when it comes from `__init__`."""
    scope = _ClassScope()
    init = next(
        (n for n in cls.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == "__init__"),
        None,
    )
    if init is None:
        return scope

    param_types: dict[str, str] = {}
    args = init.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        name = _annotation_name(arg.annotation)
        if name and name in imports:
            param_types[arg.arg] = imports[name]

    # Also honour annotated attribute assignments: `self._policy: OpaGatewayPolicy = policy`
    for node in ast.walk(init):
        if isinstance(node, ast.Assign):
            value = node.value
            if not isinstance(value, ast.Name) or value.id not in param_types:
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                    scope.attr_types[target.attr] = param_types[value.id]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"):
                continue
            name = _annotation_name(node.annotation)
            if name and name in imports:
                scope.attr_types[target.attr] = imports[name]
    return scope


def _state_name_from(node: ast.expr) -> str | None:
    """Match `<anything>.app.state.<name>` or `<anything>.state.<name>`."""
    if not isinstance(node, ast.Attribute):
        return None
    owner = node.value
    if isinstance(owner, ast.Attribute) and owner.attr == "state":
        return node.attr
    return None


def _calls_in(
    tree: ast.Module,
    module_name: str,
    scopes: dict[str, _ClassScope],
    state_types: dict[str, str],
) -> list[CallSite]:
    sites: list[CallSite] = []

    def record(target_dotted: str, call: ast.Call, method: str, binding: str) -> None:
        if method.startswith("__") or method in _UNINTERESTING_METHODS:
            return
        sites.append(
            CallSite(
                module=module_name,
                line=call.lineno,
                target_dotted=target_dotted,
                method=method,
                positional_count=sum(1 for a in call.args if not isinstance(a, ast.Starred)),
                keywords=tuple(k.arg for k in call.keywords if k.arg is not None),
                has_star_args=any(isinstance(a, ast.Starred) for a in call.args),
                has_star_kwargs=any(k.arg is None for k in call.keywords),
                binding=binding,
            )
        )

    for cls_name, scope in scopes.items():
        cls_node = _class_nodes(tree).get(cls_name)
        if cls_node is None:
            continue
        for node in ast.walk(cls_node):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            receiver = node.func.value
            if (
                isinstance(receiver, ast.Attribute)
                and isinstance(receiver.value, ast.Name)
                and receiver.value.id == "self"
                and receiver.attr in scope.attr_types
            ):
                record(scope.attr_types[receiver.attr], node, node.func.attr, "ctor")

    # `app.state` reads can happen anywhere, including module-level route bodies.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        state_name = _state_name_from(node.func.value)
        if state_name and state_name in state_types:
            record(state_types[state_name], node, node.func.attr, "app.state")

    return sites


def _class_nodes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def collect_call_sites() -> list[CallSite]:
    """Every project-owned cross-component call site under `backend/src/**`."""
    state_types = _composition_state_types()
    sites: list[CallSite] = []

    for path in sorted(SRC_ROOT.rglob("*.py")):
        module_name = _module_name_for(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = _import_map(tree, module_name)
        scopes = {name: _collect_ctor_bindings(node, imports) for name, node in _class_nodes(tree).items()}
        sites.extend(_calls_in(tree, module_name, scopes, state_types))

    # Deduplicate identical shapes at the same location, keep deterministic order.
    unique = sorted(set(sites), key=lambda s: (s.module, s.line, s.target_dotted, s.method))
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a listing")
    args = parser.parse_args(argv)

    sites = collect_call_sites()
    if args.json:
        json.dump([s.as_dict() for s in sites], sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for site in sites:
            print(site)
        print(f"\n{len(sites)} cross-component call sites")

    if not sites:
        print("ERROR: the call-site inventory is EMPTY; the collector is broken", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
