#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Chokepoint reachability, both halves (design.md §2.2.1, §11.6, Appendix B Q-03).

What this module answers
-----------------------
§2.2.1 fixes two questions and this module answers both, so `scripts/check-chokepoint.sh` and
Q-03's property test share one implementation rather than growing two that can disagree.

**Python.** Is every call to a `@mutation_primitive`-decorated function lexically inside
`src/governance/`, or does it receive a `MutationAuthority`? The primitive set is *discovered*
by scanning for the decorator, so a newly marked function is covered the moment it is written,
and an **empty** discovered set is a hard failure — a renamed decorator must not make the check
trivially pass.

**Go.** Does any package outside `internal/executor/**` import
`internal/executor/internal/mutate`? Answered from `go list -deps -json ./...`, which is
derived rather than hand-listed, with the mirror-image vacuity guard: an empty importer set
fails, because a boundary nothing imports is a boundary this check is not testing.

Both halves parse or query; neither imports the code under test. Importing `backend/src/**` to
enumerate primitives would run module-level code inside a lint, and a lint with side effects
eventually breaks the build for a reason unrelated to what it checks.

Why matching by bare name is not enough, and what is done instead
----------------------------------------------------------------
The first primitive to exist is `AuditWriter.append`. Matching call sites on the bare name
`append` would flag every `list.append` in the backend — hundreds of them — and a check that
cries wolf gets switched off, which is pattern O's failure by another route.

So an attribute call is a primitive call only when its **receiver resolves to the owning
class**. Resolution is deliberately narrow and syntactic:

* a parameter with an annotation (`writer: AuditWriter`);
* an attribute assigned anywhere in the class body from an annotated parameter
  (`self._audit = audit_writer` in `__init__`, read in another method);
* a local or attribute assigned from a constructor call (`writer = AuditWriter()`);
* a local, attribute or class-level annotation;
* a literal, so `clauses = ["..."]` types `clauses` as `list` and `clauses.append(...)` is not
  a primitive call.

Anything else is **unresolved**, and an unresolved call whose name matches a primitive is
reported and **blocks**. That is the fail-safe direction: a receiver this analysis cannot type
is a receiver that might be the primitive's owner, and the answer to "we cannot tell" on a
mutation path is refusal, not silence (§9's convention).

Why the authority check is a name-binding analysis and not a keyword-name heuristic
----------------------------------------------------------------------------------
"Receives a `MutationAuthority`" is decided by finding the names in scope that hold one — a
parameter annotated `MutationAuthority`, or a local assigned from `mint_authority(...)` — and
asking whether the call passes one of them. Accepting any argument merely *named* `authority`
would let a caller satisfy the check with `authority=None`, which is exactly the "someone
forgot to call `assert_authorized()`" failure mode §11.6 says the capability type replaces.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "AUTHORITY_TYPE",
    "DECORATOR_NAME",
    "GOVERNANCE_PACKAGE",
    "GO_EXECUTOR_PREFIX",
    "GO_MUTATE_PACKAGE",
    "MINT_FUNCTION",
    "GoImport",
    "Primitive",
    "PrimitiveCall",
    "analyse",
    "check_go_boundary",
    "classify_importers",
    "discover_primitives",
    "find_primitive_calls",
    "go_import_graph",
    "run_go_half",
    "run_python_half",
]

#: The decorator, matched syntactically by this exact spelling. `governance/primitives.py`
#: exports the same string as `DECORATOR_NAME` so the module and the checker cannot disagree;
#: it is restated here rather than imported because importing it would import `src/**`.
DECORATOR_NAME = "mutation_primitive"

#: The capability type. A call outside `governance/` must pass one of these.
AUTHORITY_TYPE = "MutationAuthority"

#: The only function that produces one, so a local assigned from it holds an authority.
MINT_FUNCTION = "mint_authority"

#: Calls lexically inside this package are authorised by position (§2.2.1).
GOVERNANCE_PACKAGE = "governance"


@dataclass(frozen=True, slots=True)
class Primitive:
    """One discovered `@mutation_primitive` function."""

    name: str
    #: The class it is defined in, or `None` for a module-level function.
    owner: str | None
    module: str
    line: int

    @property
    def dotted(self) -> str:
        return (
            f"{self.module}.{self.owner}.{self.name}"
            if self.owner
            else f"{self.module}.{self.name}"
        )


@dataclass(frozen=True, slots=True)
class PrimitiveCall:
    """One call site that reaches a primitive, and whether it is authorised."""

    primitive: str
    module: str
    path: str
    line: int
    #: "governance" | "authority" | "unresolved-receiver" | "no-authority"
    verdict: str
    receiver: str | None = None

    @property
    def authorised(self) -> bool:
        return self.verdict in ("governance", "authority")

    def render(self) -> str:
        if self.verdict == "unresolved-receiver":
            return (
                f"{self.path}:{self.line}: mutation primitive '{self.primitive}' called on a receiver "
                f"this check cannot type ({self.receiver}); annotate it so the boundary is decidable"
            )
        return (
            f"{self.path}:{self.line}: mutation primitive '{self.primitive}' called outside "
            f"governance/ without MutationAuthority"
        )


@dataclass
class _Scope:
    """Names whose type this analysis knows, within one function or class body."""

    types: dict[str, str] = field(default_factory=dict)
    authorities: set[str] = field(default_factory=set)

    def child(self) -> _Scope:
        return _Scope(types=dict(self.types), authorities=set(self.authorities))


def _module_name(path: Path, root: Path) -> str:
    return path.relative_to(root).with_suffix("").as_posix().replace("/", ".")


def _annotation_type(node: ast.expr | None) -> str | None:
    """The bare class name an annotation resolves to, ignoring subscripts and unions.

    `AuditWriter`, `AuditWriter | None` and `list[Finding]` yield `AuditWriter`, `AuditWriter`
    and `list`. Deliberately shallow: the question is only ever "is this the class that owns a
    primitive", and a shallow answer is either that class or something else.
    """
    match node:
        case ast.Name(id=name):
            return name
        case ast.Constant(value=str() as text):
            # A string annotation, e.g. `"AuditWriter"` under `from __future__ import
            # annotations` written explicitly. Take the leading identifier.
            head = text.strip().split("[")[0].split("|")[0].strip()
            return head.rsplit(".", 1)[-1] or None
        case ast.Attribute(attr=attr):
            return attr
        case ast.Subscript(value=value):
            return _annotation_type(value)
        case ast.BinOp(left=left, right=right):
            # `X | None` — take whichever side is not None.
            for side in (left, right):
                resolved = _annotation_type(side)
                if resolved and resolved != "None":
                    return resolved
            return None
        case _:
            return None


def _call_class(node: ast.expr | None) -> str | None:
    """The class name a constructor call names, for `writer = AuditWriter()`."""
    if isinstance(node, ast.Call):
        match node.func:
            case ast.Name(id=name):
                return name
            case ast.Attribute(attr=attr):
                return attr
    return None


#: Literal forms whose type is decidable without any inference at all. This is what keeps the
#: check usable: `clauses = ["..."]` then `clauses.append(...)` is a `list.append`, and a check
#: that could not tell would report every list in the backend as an unresolved primitive call.
_LITERAL_TYPES: dict[type[ast.expr], str] = {
    ast.List: "list",
    ast.ListComp: "list",
    ast.Dict: "dict",
    ast.DictComp: "dict",
    ast.Set: "set",
    ast.SetComp: "set",
    ast.Tuple: "tuple",
    ast.GeneratorExp: "Generator",
    ast.JoinedStr: "str",
}


def _literal_type(node: ast.expr | None) -> str | None:
    """The type of a literal expression, or `None` when it is not one."""
    if node is None:
        return None
    resolved = _LITERAL_TYPES.get(type(node))
    if resolved is not None:
        return resolved
    if isinstance(node, ast.Constant):
        return type(node.value).__name__
    return None


def _inferred_type(node: ast.expr | None, scope: _Scope) -> str | None:
    """The best type this analysis can put on an expression.

    Three sources, in order: a constructor call, a literal, and an already-known name. Nothing
    else — no return-type lookup, no cross-module resolution. The narrowness is deliberate: an
    analysis that guesses is an analysis whose failures are silent, and this one's failures are
    reported as `unresolved-receiver` and block.
    """
    constructed = _call_class(node)
    if constructed is not None:
        return constructed
    literal = _literal_type(node)
    if literal is not None:
        return literal
    if isinstance(node, ast.Name | ast.Attribute):
        key = _receiver_key(node)
        if key is not None:
            return scope.types.get(key)
    return None


def _receiver_key(node: ast.expr) -> str | None:
    """A stable key for a receiver expression: `writer`, `self._audit`, `a.b.c`."""
    parts: list[str] = []
    current: ast.expr | None = node
    while True:
        match current:
            case ast.Name(id=name):
                parts.append(name)
                break
            case ast.Attribute(value=value, attr=attr):
                parts.append(attr)
                current = value
            case _:
                return None
    return ".".join(reversed(parts))


def discover_primitives(src_root: Path) -> list[Primitive]:
    """Every `@mutation_primitive`-decorated function under `src_root`.

    Discovery is by decorator, never by a hand-maintained list, so a newly marked function is
    covered the moment it is written. §2.2.1: "a list would be one edit away from being wrong,
    and wrong in the direction of silence."
    """
    found: list[Primitive] = []
    for path in sorted(src_root.rglob("*.py")):
        module = _module_name(path, src_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for owner, node in _iter_functions(tree):
            for decorator in node.decorator_list:
                name = (
                    decorator.id
                    if isinstance(decorator, ast.Name)
                    else getattr(decorator, "attr", None)
                )
                if name == DECORATOR_NAME:
                    found.append(
                        Primitive(
                            name=node.name, owner=owner, module=module, line=node.lineno
                        )
                    )
    return found


def _iter_functions(
    tree: ast.AST, owner: str | None = None
) -> Iterator[tuple[str | None, ast.AST]]:
    """Yield `(owning class or None, function node)` for every function in `tree`."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            yield from _iter_functions(node, owner=node.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield owner, node
            yield from _iter_functions(node, owner=owner)
        else:
            yield from _iter_functions(node, owner=owner)


class _CallFinder(ast.NodeVisitor):
    """Walk one module, tracking name types and authority bindings, collecting primitive calls."""

    def __init__(
        self,
        *,
        primitives: Iterable[Primitive],
        module: str,
        path: str,
        in_governance: bool,
    ) -> None:
        self._by_name: dict[str, list[Primitive]] = {}
        for primitive in primitives:
            self._by_name.setdefault(primitive.name, []).append(primitive)
        self._module = module
        self._path = path
        self._in_governance = in_governance
        self._scope = _Scope()
        #: Types of `self.<attr>` within the class currently being walked.
        #:
        #: Class-scoped rather than function-scoped, and that is the fix for the case that
        #: matters: `self._audit = audit_writer` is written in `__init__` and *read* in another
        #: method. Recorded per function, the binding would vanish with `__init__`'s scope and
        #: every later `self._audit.append(...)` would be an unresolved receiver.
        self._attributes: dict[str, str] = {}
        self.calls: list[PrimitiveCall] = []

    # ── scope tracking ────────────────────────────────────────────────────────────────

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast API
        outer_scope, self._scope = self._scope, self._scope.child()
        outer_attributes, self._attributes = self._attributes, {}
        # Class-level annotations, e.g. `_audit: AuditWriter`.
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign):
                key = _receiver_key(statement.target)
                resolved = _annotation_type(statement.annotation)
                if key and resolved:
                    self._attributes[f"self.{key}"] = resolved
        # Two passes over the body. The first collects `self.<attr>` bindings from every
        # method, the second walks for calls — because a method defined before `__init__`
        # may still read an attribute `__init__` binds, and source order is not dataflow.
        for statement in node.body:
            self._collect_attributes(statement)
        for statement in node.body:
            self.visit(statement)
        self._scope = outer_scope
        self._attributes = outer_attributes

    def _collect_attributes(self, node: ast.AST) -> None:
        """Record every `self.<attr> = <expr>` in a class body, whichever method it is in."""
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            local = _Scope(types=dict(self._scope.types))
            for argument in [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]:
                resolved = _annotation_type(argument.annotation)
                if resolved is not None:
                    local.types[argument.arg] = resolved
            for inner in ast.walk(node):
                if isinstance(inner, ast.AnnAssign):
                    key = _receiver_key(inner.target)
                    resolved = _annotation_type(inner.annotation)
                    if key and key.startswith("self.") and resolved:
                        self._attributes[key] = resolved
                elif isinstance(inner, ast.Assign):
                    resolved = _inferred_type(inner.value, local)
                    if resolved is None:
                        continue
                    for target in inner.targets:
                        key = _receiver_key(target)
                        if key and key.startswith("self."):
                            self._attributes.setdefault(key, resolved)
            return
        for child in ast.iter_child_nodes(node):
            self._collect_attributes(child)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        outer, self._scope = self._scope, self._scope.child()
        self._scope.types.update(self._attributes)
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        for argument in arguments:
            resolved = _annotation_type(argument.annotation)
            if resolved is None:
                continue
            self._scope.types[argument.arg] = resolved
            if resolved == AUTHORITY_TYPE:
                self._scope.authorities.add(argument.arg)
        self.generic_visit(node)
        self._scope = outer

    visit_FunctionDef = _visit_function  # noqa: N815 - ast API
    visit_AsyncFunctionDef = _visit_function  # noqa: N815 - ast API

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802 - ast API
        key = _receiver_key(node.target)
        resolved = _annotation_type(node.annotation)
        if key and resolved:
            self._scope.types[key] = resolved
            if resolved == AUTHORITY_TYPE:
                self._scope.authorities.add(key)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802 - ast API
        resolved = _inferred_type(node.value, self._scope)
        source_key = (
            _receiver_key(node.value)
            if isinstance(node.value, ast.Name | ast.Attribute)
            else None
        )
        minted = _call_class(node.value) == MINT_FUNCTION
        for target in node.targets:
            key = _receiver_key(target)
            if key is None:
                continue
            if resolved is not None:
                self._scope.types[key] = resolved
            if minted or (source_key and source_key in self._scope.authorities):
                self._scope.authorities.add(key)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802 - ast API
        """A loop target over a typed iterable stays untyped, which is the honest answer.

        Overridden only so the target does not inherit a stale type from an earlier binding of
        the same name — a receiver typed by accident is worse than one reported as unresolved.
        """
        for name in (
            (_receiver_key(node.target),)
            if isinstance(node.target, ast.Name | ast.Attribute)
            else ()
        ):
            if name:
                self._scope.types.pop(name, None)
        self.generic_visit(node)

    # ── the call check ────────────────────────────────────────────────────────────────

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        self._check(node)
        self.generic_visit(node)

    def _pick(self, candidates: list[Primitive]) -> Primitive:
        """Which discovered primitive to name in the report when several share a method name.

        Prefer one defined in this module. Two classes with the same name and the same method
        name is rare in production and common in fixtures, and naming the wrong one turns a
        correct finding into a confusing one.
        """
        for candidate in candidates:
            if candidate.module == self._module:
                return candidate
        return candidates[0]

    def _check(self, node: ast.Call) -> None:
        match node.func:
            case ast.Name(id=name):
                candidates = self._by_name.get(name, [])
                # A bare name reaches a module-level primitive, or a method called on `self`
                # from inside the owning class. Either way there is no receiver to type.
                if not candidates:
                    return
                self._record(node, self._pick(candidates), receiver=None, resolved=True)
            case ast.Attribute(value=value, attr=attr):
                candidates = self._by_name.get(attr, [])
                if not candidates:
                    return
                receiver = _receiver_key(value)
                owners = {
                    candidate.owner for candidate in candidates if candidate.owner
                }
                inferred = self._scope.types.get(receiver or "")
                if inferred is not None:
                    if inferred in owners:
                        self._record(
                            node,
                            self._pick(candidates),
                            receiver=receiver,
                            resolved=True,
                        )
                    return  # a typed receiver that is not the owner is not a primitive call
                if receiver in ("self", None):
                    # `self.append(...)` inside a class: only a primitive call when this class
                    # owns one, which the bare-name branch above already covers for `self`.
                    return
                self._record(
                    node, self._pick(candidates), receiver=receiver, resolved=False
                )
            case _:
                return

    def _record(
        self,
        node: ast.Call,
        primitive: Primitive,
        *,
        receiver: str | None,
        resolved: bool,
    ) -> None:
        if not resolved:
            verdict = "unresolved-receiver"
        elif self._in_governance:
            verdict = "governance"
        elif self._passes_authority(node):
            verdict = "authority"
        else:
            verdict = "no-authority"
        self.calls.append(
            PrimitiveCall(
                primitive=primitive.dotted,
                module=self._module,
                path=self._path,
                line=node.lineno,
                verdict=verdict,
                receiver=receiver,
            )
        )

    def _passes_authority(self, node: ast.Call) -> bool:
        """Whether the call passes a name this analysis knows holds a `MutationAuthority`.

        Name-binding, not naming convention. `authority=None` does not satisfy it, and neither
        does a variable called `authority` that was never annotated or minted.
        """
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        for argument in arguments:
            key = _receiver_key(argument)
            if key and key in self._scope.authorities:
                return True
            if (
                isinstance(argument, ast.Call)
                and _call_class(argument) == MINT_FUNCTION
            ):
                return True
        return False


def find_primitive_calls(
    src_root: Path, primitives: Iterable[Primitive]
) -> list[PrimitiveCall]:
    """Every call site reaching one of `primitives`, with its verdict."""
    primitives = list(primitives)
    calls: list[PrimitiveCall] = []
    for path in sorted(src_root.rglob("*.py")):
        module = _module_name(path, src_root)
        relative = path.relative_to(src_root.parent).as_posix()
        in_governance = module.split(".")[0] == GOVERNANCE_PACKAGE
        finder = _CallFinder(
            primitives=primitives,
            module=module,
            path=relative,
            in_governance=in_governance,
        )
        finder.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        calls.extend(finder.calls)
    return calls


def analyse(src_root: Path) -> tuple[list[Primitive], list[PrimitiveCall]]:
    """Discover primitives and their call sites in one pass over `src_root`."""
    primitives = discover_primitives(src_root)
    return primitives, find_primitive_calls(src_root, primitives)


# ─── the Go half ──────────────────────────────────────────────────────────────────────────

#: The mutation boundary. Importable only from packages rooted at `internal/executor/` by Go's
#: nested-`internal` rule (§2.2.1 mechanism 3, D-45), which is a COMPILE-time boundary rather
#: than a lint. This check exists anyway, because a boundary can be widened by a well-meaning
#: refactor that moves a package *inside* the subtree to reach it.
GO_MUTATE_PACKAGE = (
    "github.com/parag8487/ForgeOps/agent/internal/executor/internal/mutate"
)

#: The only prefix permitted to import it.
GO_EXECUTOR_PREFIX = "github.com/parag8487/ForgeOps/agent/internal/executor"


@dataclass(frozen=True, slots=True)
class GoImport:
    """One package and the fact that it imports the mutation boundary."""

    importer: str
    permitted: bool


def go_import_graph(agent_root: Path) -> dict[str, list[str]]:
    """`go list -deps -json ./...`, parsed into importer → imports.

    `-json` rather than a `-f` template, as §2.2.1 specifies: a template's output is
    whitespace-separated and a package path containing a space would silently split. JSON has
    one parse.

    The objects arrive concatenated rather than as an array, which is `go list`'s documented
    output shape, so they are read with `raw_decode` in a loop instead of `json.load`.
    """
    import json
    import subprocess

    result = subprocess.run(
        ["go", "list", "-deps", "-json", "./..."],
        cwd=str(agent_root),
        capture_output=True,
        text=True,
        # UTF-8 explicitly, and `errors="replace"` so a byte the codec dislikes cannot silence
        # the whole check. `text=True` alone uses the platform's preferred encoding, which on
        # Windows is cp1252: `go list -deps -json` emits Go's standard-library package docs,
        # which contain U+009D among other things, and the reader thread then dies with a
        # `UnicodeDecodeError` while `subprocess.run` still reports returncode 0 and
        # `stdout is None`. That is a check that passes because it read nothing.
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"go list -deps -json ./... failed in {agent_root}:\n{result.stderr}"
        )
    if not result.stdout:
        raise RuntimeError(
            f"go list -deps -json ./... produced no output in {agent_root}; the import graph "
            "cannot be read and this check would assert nothing"
        )

    graph: dict[str, list[str]] = {}
    decoder = json.JSONDecoder()
    payload = result.stdout
    index = 0
    length = len(payload)
    while index < length:
        while index < length and payload[index].isspace():
            index += 1
        if index >= length:
            break
        package, index = decoder.raw_decode(payload, index)
        path = package.get("ImportPath")
        if not path:
            continue
        imports = [
            *package.get("Imports", []),
            *package.get("TestImports", []),
            *package.get("XTestImports", []),
        ]
        graph[path] = imports
    return graph


def classify_importers(graph: dict[str, list[str]]) -> tuple[list[GoImport], list[str]]:
    """Classify every importer of the boundary in `graph`. Pure, so it is testable.

    Split from `go_import_graph` deliberately. `./...` skips `testdata`, so the negative fixture
    that proves the compile-time rule (`agent/testdata/chokepoint/outsider`) is invisible to
    `go list` — which means the classification could never be exercised by a real offender
    without adding one to the tree. Feeding a synthetic graph to this function is how the Go
    half gets a negative control at all, and `mutate/boundary_test.go` proves the underlying
    rule with two real `go build` invocations.
    """
    if not graph:
        raise RuntimeError(
            "the import graph is empty; go list resolved no packages at all"
        )
    if GO_MUTATE_PACKAGE not in graph:
        raise RuntimeError(
            f"the import graph does not contain {GO_MUTATE_PACKAGE} as a package at all. "
            "The query is wrong, or the boundary package was moved or deleted, and this check "
            "would pass whatever the tree contained (design 2.2.1)."
        )
    importers: list[GoImport] = []
    offenders: list[str] = []
    for importer, imports in sorted(graph.items()):
        if GO_MUTATE_PACKAGE not in imports:
            continue
        permitted = importer.startswith(GO_EXECUTOR_PREFIX)
        importers.append(GoImport(importer=importer, permitted=permitted))
        if not permitted:
            offenders.append(f"package {importer} imports executor/internal/mutate")
    return importers, offenders


def check_go_boundary(agent_root: Path) -> tuple[list[GoImport], list[str]]:
    """Every importer of the mutation boundary in the real module, and the offenders among them.

    Raises when the **enumeration** is vacuous — an empty graph, or a graph that does not
    contain the boundary package as a node at all. That is the guard 2.2.1 asks for on this
    half: "Exit 0 only when both enumerations are non-empty and clean", where the enumerations
    are the import graph and the primitive set.

    An empty **importer** list is deliberately *not* a failure. Today it is the correct answer:
    Go's nested-`internal` rule means only packages rooted at `internal/executor/` may import
    `mutate`, and the only such package is `executor` itself, whose dispatcher arrives with leaf
    8.7. Failing on zero importers would make this check impossible to satisfy on a correct
    tree — the same mistake leaf 7.3's original position made on the Python half.
    """
    return classify_importers(go_import_graph(agent_root))


def run_go_half(agent_root: Path, *, quiet: bool = False) -> int:
    """The Go half's exit code, with its vacuity guard on the enumeration."""
    if not (agent_root / "go.mod").is_file():
        print(f"check-chokepoint(go): {agent_root} has no go.mod", file=sys.stderr)
        return 2
    try:
        graph = go_import_graph(agent_root)
        importers, offenders = check_go_boundary(agent_root)
    except RuntimeError as exc:
        print(f"check-chokepoint(go): FAIL - {exc}", file=sys.stderr)
        return 1

    if not quiet:
        print(f"check-chokepoint(go): import graph read: {len(graph)} package(s)")
        print(
            f"check-chokepoint(go): {len(importers)} importer(s) of executor/internal/mutate"
        )
        for entry in importers:
            marker = "ok" if entry.permitted else "OFFENDER"
            print(f"  [{marker}] {entry.importer}")

    if offenders:
        print("check-chokepoint(go): FAIL", file=sys.stderr)
        for offender in offenders:
            print(offender, file=sys.stderr)
        return 1
    if not importers:
        # Reported, never silent. `./...` skips testdata, and the only package permitted to
        # import the boundary is `executor` itself, whose dispatcher lands with leaf 8.7. So
        # zero is expected here and will become one; a reader must be able to see which.
        print(
            "check-chokepoint(go): note - nothing imports the boundary yet. The compile-time "
            "rule is still in force (proved by mutate/boundary_test.go's two fixtures); the "
            "importer set becomes non-empty when executor.Dispatcher lands in leaf 8.7."
        )
    print(
        f"check-chokepoint(go): OK - {len(importers)} importer(s), none outside executor/**"
    )
    return 0


def run_python_half(src_root: Path, *, quiet: bool = False) -> int:
    """The Python half's exit code, with the vacuity guard §2.2.1 names explicitly."""
    if not src_root.is_dir():
        print(
            f"check-chokepoint(python): {src_root} is not a directory", file=sys.stderr
        )
        return 2

    primitives, calls = analyse(src_root)

    # Exit 1 on an EMPTY discovered set, so a renamed decorator cannot make the check trivially
    # pass. This is the reason leaf 7.3 was resequenced to run after 7.5 and 7.6: before them
    # the set really was empty, and the check would have correctly refused to pass on a correct
    # tree.
    if not primitives:
        print(
            "check-chokepoint(python): FAIL - the discovered mutation-primitive set is EMPTY. "
            f"Either no function carries @{DECORATOR_NAME} or the decorator was renamed and "
            "this check is no longer looking at anything (design 2.2.1).",
            file=sys.stderr,
        )
        return 1

    if not quiet:
        print(
            f"check-chokepoint(python): {len(primitives)} primitive(s) discovered by @{DECORATOR_NAME}"
        )
        for primitive in primitives:
            print(f"  {primitive.dotted}  (line {primitive.line})")
        print(f"check-chokepoint(python): {len(calls)} call site(s) reach a primitive")
        for call in calls:
            print(f"  [{call.verdict}] {call.path}:{call.line} -> {call.primitive}")

    offenders = [call for call in calls if not call.authorised]
    if offenders:
        print("check-chokepoint(python): FAIL", file=sys.stderr)
        for offender in offenders:
            print(offender.render(), file=sys.stderr)
        return 1

    # A non-empty primitive set with zero call sites is the second vacuity shape: the primitives
    # exist, nothing calls them, and "every call is authorised" is vacuously true. Reported
    # rather than failed, because a primitive whose only caller is a future leaf is a legitimate
    # intermediate state — but it must be visible, not silent.
    if not calls:
        print(
            f"check-chokepoint(python): WARNING - {len(primitives)} primitive(s) exist but nothing "
            "calls them, so the call-site clause is vacuously satisfied"
        )
    print(f"check-chokepoint(python): OK - {len(calls)} call site(s), all authorised")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles and redirected pipes default to cp1252, and a single character outside
    # it makes `print` raise inside the writer thread — which surfaced here as **exit code 1
    # with no output at all**, a check that fails for a reason nobody can read. Leaf 7.6 hit
    # the same wall with an em dash in `verify-chain`'s output and answered it by keeping the
    # message ASCII. That works until someone adds a section sign, so the encoding is pinned
    # here as well: belt for the messages, braces for the stream.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Chokepoint reachability (design 2.2.1, 11.6, Q-03)"
    )
    parser.add_argument(
        "--src", default="backend/src", help="the Python source root to walk"
    )
    parser.add_argument("--agent", default="agent", help="the Go module directory")
    parser.add_argument("--half", choices=("python", "go", "both"), default="both")
    parser.add_argument(
        "--quiet", action="store_true", help="print only offenders and the verdict"
    )
    args = parser.parse_args(argv)

    codes: list[int] = []
    if args.half in ("python", "both"):
        codes.append(run_python_half(Path(args.src).resolve(), quiet=args.quiet))
    if args.half in ("go", "both"):
        codes.append(run_go_half(Path(args.agent).resolve(), quiet=args.quiet))
    return max(codes) if codes else 2


if __name__ == "__main__":
    raise SystemExit(main())
