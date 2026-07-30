#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Signature-enforcing test doubles, enforced by tooling (design.md §0.4.3).

Why a bespoke AST lint rather than a Ruff rule
----------------------------------------------
Ruff cannot express "an assignment whose target is an attribute on a name that was
bound from a `Mock(spec=...)` call". That sentence is the Phase 0 defect (D-23):
`test_mcp_e2e.py` built `AsyncMock(spec=OpaGatewayPolicy)` and then reassigned the
spec'd child, `policy.filter_tools = AsyncMock(...)`. Reassignment discards
`spec`'s signature enforcement, so the double implemented the contract the caller
wanted while the real collaborator implemented a different one. 419 tests stayed
green over a gateway that raised `TypeError` on every request.

Rules
-----
FO-TD001  assignment over a `spec=`'d child with a bare Mock/AsyncMock/MagicMock
FO-TD002  `spec=` / `create_autospec(...)` without `spec_set=True`
FO-TD003  `patch` / `patch.object` without `autospec=True` on a project-owned target
FO-TD004  any Mock at all under `tests/integration/**`

Invocation
----------
    python scripts/check-test-doubles.py backend/tests

Input is every `.py` file under the given roots, parsed with `ast`. Nothing is
imported, so the check is safe on an untrusted tree and runs in well under a
second. Failure is exit 1 with one line per finding as
`path:line: FO-TD00N message`; zero findings exits 0.

Suppression requires an explicit reason:

    m.method = AsyncMock()  # noqa: FO-TD001 - transport shim, not a collaborator

A suppression **without** a reason is itself reported as FO-TD001, because a bare
`# noqa` is how this class of defect gets waved through.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Constructors that produce a signature-free double.
BARE_MOCK_NAMES = frozenset({"Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock"})
#: Constructors that can carry a spec.
SPECCABLE_NAMES = BARE_MOCK_NAMES | {"create_autospec"}

RULE_MESSAGES = {
    "FO-TD001": "assignment over a spec='d double discards signature enforcement (D-23); "
    "configure the child instead: m.{attr}.return_value = ... / .side_effect = ...",
    "FO-TD002": "{ctor}(...) without spec_set=True; spec_set also rejects NEW attribute "
    "names, closing the sibling hole",
    "FO-TD003": "{call}(...) on a project-owned target without autospec=True; "
    "a patch without autospec is a reassignment by another name",
    "FO-TD004": "Mock is forbidden under tests/integration/**; integration tests "
    "substitute transports, not objects (design.md 0.4.1)",
    "FO-TD001-noqa": "reasonless suppression; write `# noqa: {code} - <reason>`",
}

_NOQA = re.compile(r"#\s*noqa\s*:\s*(?P<codes>FO-TD\d{3}(?:\s*,\s*FO-TD\d{3})*)(?P<rest>.*)$")
#: Anything that is only punctuation or whitespace is not a reason.
_REASON_IS_EMPTY = re.compile(r"^[\s:\-\u2013\u2014#]*$")


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.message}"


def _callee_name(node: ast.expr) -> str | None:
    """`Mock` from `Mock(...)`, `Mock` from `mock.Mock(...)`, `object` from `patch.object`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _patch_call_kind(func: ast.expr) -> str | None:
    """Return `patch` or `patch.object` when `func` denotes one, else None."""
    name = _callee_name(func)
    if name == "patch":
        return "patch"
    if name == "object" and isinstance(func, ast.Attribute) and _callee_name(func.value) == "patch":
        return "patch.object"
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


class _ProjectImports:
    """Names in this module that came from the project's own `src` package."""

    def __init__(self, tree: ast.Module) -> None:
        self.names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root == "src" or node.level:
                    for alias in node.names:
                        self.names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "src":
                        self.names.add(alias.asname or alias.name.split(".")[0])

    def owns(self, node: ast.expr) -> bool:
        """True when `node` names a project-owned target."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.startswith(("src.", "backend.src."))
        base = node
        while isinstance(base, ast.Attribute):
            base = base.value
        return isinstance(base, ast.Name) and base.id in self.names


class _SpecBoundNames:
    """Names bound to a double that carries a spec, per enclosing scope.

    Scope tracking is deliberately coarse — one set per module plus one per
    function — because a double is nearly always created and mutated inside the
    same test. Coarseness here can only cause a *false positive*, which a reasoned
    `# noqa` resolves; the alternative, missing a rebinding across scopes, would
    let the D-23 defect back in.
    """

    def __init__(self) -> None:
        self.names: set[str] = set()

    def learn(self, node: ast.Assign) -> None:
        value = node.value
        if not isinstance(value, ast.Call):
            return
        ctor = _callee_name(value.func)
        if ctor not in SPECCABLE_NAMES:
            return
        has_spec = _keyword(value, "spec") is not None or _keyword(value, "spec_set") is not None
        if ctor == "create_autospec":
            has_spec = has_spec or bool(value.args)
        if not has_spec:
            return
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.names.add(target.id)


class _Checker(ast.NodeVisitor):
    def __init__(self, path: Path, display: str, source: str, tree: ast.Module, *, integration: bool) -> None:
        self.path = path
        self.display = display
        self.lines = source.splitlines()
        self.integration = integration
        self.imports = _ProjectImports(tree)
        self.spec_bound = _SpecBoundNames()
        self.findings: list[Finding] = []
        self.suppressed: list[Finding] = []

    # ── suppression ──────────────────────────────────────────────────────────

    def _suppression(self, line: int) -> tuple[set[str], bool] | None:
        """(codes, has_reason) for a noqa on `line`, or None when absent."""
        if not (1 <= line <= len(self.lines)):
            return None
        match = _NOQA.search(self.lines[line - 1])
        if not match:
            return None
        codes = {c.strip() for c in match.group("codes").split(",")}
        return codes, not _REASON_IS_EMPTY.match(match.group("rest") or "")

    def report(self, line: int, code: str, message: str) -> None:
        suppression = self._suppression(line)
        if suppression is not None and code in suppression[0]:
            if suppression[1]:
                self.suppressed.append(Finding(self.display, line, code, message))
                return
            self.findings.append(
                Finding(
                    self.display,
                    line,
                    "FO-TD001",
                    RULE_MESSAGES["FO-TD001-noqa"].format(code=code),
                )
            )
            return
        self.findings.append(Finding(self.display, line, code, message))

    # ── rules ────────────────────────────────────────────────────────────────

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802 - ast API
        self.spec_bound.learn(node)

        value = node.value
        if isinstance(value, ast.Call) and _callee_name(value.func) in BARE_MOCK_NAMES:
            bare = _keyword(value, "spec") is None and _keyword(value, "spec_set") is None
            for target in node.targets:
                if (
                    bare
                    and isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in self.spec_bound.names
                ):
                    self.report(
                        node.lineno,
                        "FO-TD001",
                        RULE_MESSAGES["FO-TD001"].format(attr=target.attr),
                    )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        ctor = _callee_name(node.func)

        if ctor in SPECCABLE_NAMES:
            if self.integration:
                self.report(node.lineno, "FO-TD004", RULE_MESSAGES["FO-TD004"])
            spec_given = _keyword(node, "spec") is not None or (ctor == "create_autospec" and bool(node.args))
            if (spec_given or _keyword(node, "spec_set") is not None) and not _is_true(_keyword(node, "spec_set")):
                self.report(
                    node.lineno,
                    "FO-TD002",
                    RULE_MESSAGES["FO-TD002"].format(ctor=ctor),
                )

        kind = _patch_call_kind(node.func)
        if kind is not None:
            target = node.args[0] if node.args else None
            if target is not None and self.imports.owns(target) and not _is_true(_keyword(node, "autospec")):
                self.report(
                    node.lineno,
                    "FO-TD003",
                    RULE_MESSAGES["FO-TD003"].format(call=kind),
                )
        self.generic_visit(node)


def _is_integration(path: Path) -> bool:
    return "integration" in path.parts


#: The lint's own fixtures live under `backend/tests/meta/fixtures/`. `bad_double.py`
#: exists to be flagged, so scanning it as ordinary test code would make the real
#: tree permanently red and the check would get switched off. It is excluded here
#: and fed to `check_file` directly by `tests/meta/test_check_test_doubles.py`,
#: which asserts that it IS flagged — so the exclusion cannot hide a regression.
_FIXTURE_DIR_SUFFIX = ("tests", "meta", "fixtures")


def _is_lint_fixture(path: Path) -> bool:
    parts = path.parts
    for i in range(len(parts) - len(_FIXTURE_DIR_SUFFIX) + 1):
        if parts[i : i + len(_FIXTURE_DIR_SUFFIX)] == _FIXTURE_DIR_SUFFIX:
            return True
    return False


def check_file(path: Path, *, display: str | None = None) -> tuple[list[Finding], list[Finding]]:
    """Return (findings, suppressed) for one file."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    shown = display if display is not None else path.as_posix()
    checker = _Checker(path, shown, source, tree, integration=_is_integration(path))
    checker.visit(tree)
    return sorted(checker.findings), sorted(checker.suppressed)


def check_paths(roots: list[Path]) -> tuple[list[Finding], list[Finding], int]:
    """Return (findings, suppressed, files_scanned) across `roots`."""
    findings: list[Finding] = []
    suppressed: list[Finding] = []
    scanned = 0
    for root in roots:
        candidates = sorted(root.rglob("*.py")) if root.is_dir() else [root]
        for path in candidates:
            if root.is_dir() and _is_lint_fixture(path):
                continue
            try:
                display = path.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                display = path.as_posix()
            found, waived = check_file(path, display=display)
            findings.extend(found)
            suppressed.extend(waived)
            scanned += 1
    return sorted(findings), sorted(suppressed), scanned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enforce signature-enforcing test doubles (design.md §0.4.3)")
    parser.add_argument("paths", nargs="*", default=["backend/tests"], help="files or directories to scan")
    parser.add_argument("--quiet", action="store_true", help="print findings only")
    args = parser.parse_args(argv)

    roots = [Path(p) for p in (args.paths or ["backend/tests"])]
    missing = [r for r in roots if not r.exists()]
    if missing:
        for root in missing:
            print(f"ERROR: no such path: {root}", file=sys.stderr)
        return 2

    findings, suppressed, scanned = check_paths(roots)

    for finding in findings:
        print(str(finding))

    if not args.quiet:
        summary = f"check-test-doubles: {scanned} files, {len(findings)} findings"
        if suppressed:
            summary += f", {len(suppressed)} suppressed with a reason"
        print(summary, file=sys.stderr)

    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
