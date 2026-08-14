#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""No credential-SHAPED literal in a test source (`.antigravity/steering/secret-safety.md`).

Why this exists
---------------
The steering rules already say it: "Never use a value that resembles a real provider token
format. Do not paste realistic-looking JWTs, `sk-...` keys, or AWS access keys, even as
examples." Until now that rule had **no mechanical enforcement**, so it held exactly as long
as whoever was writing the test remembered it — and it stopped holding in
`backend/tests/property/test_q19_route_coverage.py`, which shipped `Bearer …`,
`Basic <base64>` and an `eyJ`-prefixed JWT as source literals. None was ever a usable
credential. That is not the point: a scanner cannot tell, a reviewer cannot tell at a glance,
and `backend/tests/synthetic_secrets.py` records a real GitGuardian incident raised against a
JWT-shaped placeholder in this very repository. A blocked scan that people learn to wave
through is worse than no scan.

What it checks, and what it deliberately does not
------------------------------------------------
Only **string literals that are not docstrings**. Prose about a credential shape is exactly
what a reader needs — `synthetic_secrets.py`'s own docstring explains the problem — and a
check that forbade discussing the shape would push the explanation out of the file. A
docstring is a statement about the code; a bare literal is a value the code uses.

The remedy is never to weaken this check: `backend/tests/synthetic_secrets.py` composes every
shape from fragments at runtime, so the bytes reaching the code under test are identical while
no contiguous credential-shaped string exists in any source file. Use it.

Usage:
    check-test-credentials.py [path ...]        # defaults to backend/tests

Failure is exit 1 listing `path:line: FO-SEC001 <rule>: <redacted excerpt>`. Exit 1 **also**
when no file was scanned or no literal was examined, because an enumeration that silently
matched nothing would pass forever — the same vacuity trap §0.4.5 closes for the mutation
harness.

Suppression requires a reason, following the `FO-TD00N` convention already in use:
`# noqa: FO-SEC001 — <reason>`. A reasonless suppression is itself a finding.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: `synthetic_secrets.py` is the sanctioned place to BUILD these shapes from fragments. It
#: contains no contiguous credential-shaped literal itself — that is its whole purpose — so
#: it is exempt from the literal scan rather than from the rule.
EXEMPT_FILES = frozenset({Path("backend/tests/synthetic_secrets.py").as_posix()})

#: This check's own fixtures live under `backend/tests/meta/fixtures/`. `bad_credential.py`
#: exists precisely to be flagged, so scanning it would keep the tree permanently red and the
#: check would get switched off. Excluded here rather than allowlisted, exactly as
#: `check-test-doubles.py` excludes its own `bad_double.py`, and
#: `tests/meta/test_check_test_credentials.py` drives the fixtures directly so they are still
#: fully exercised.
_FIXTURE_DIR_SUFFIX = ("tests", "meta", "fixtures")

#: Each rule names the shape it catches, so a failure says what to do rather than only that
#: something matched. Ordered most-specific first, purely for message quality.
#:
#: **Every pattern requires a token-shaped PAYLOAD, not just a scheme name.** The first draft
#: used `\bBearer\s+\S` and reported twenty-one findings, most of them English prose — the
#: phrase "an HTTP Bearer credential" matches it, as does `"Bearer " + marker` where the
#: marker is four characters. A check that fires on its own error messages is a check people
#: switch off, so the bar is a contiguous run of at least sixteen token characters: long
#: enough that no sentence reaches it, short enough that every real provider format does.
_TOKEN_CHARS = r"[A-Za-z0-9_\-.=+/]"

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a JWT (`eyJ` header)", re.compile(rf"eyJ{_TOKEN_CHARS}{{12,}}")),
    ("an HTTP Bearer credential", re.compile(rf"\bBearer\s+{_TOKEN_CHARS}{{16,}}")),
    ("an HTTP Basic credential", re.compile(r"\bBasic\s+[A-Za-z0-9+/]{16,}={0,2}")),
    ("an OpenAI-style key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9]{16,}")),
    ("a GitHub token", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{16,}")),
    ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}")),
    ("a Google API key", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}")),
    ("a Slack token", re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{12,}")),
    ("a PEM block", re.compile(r"-{5}BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)")),
)

SUPPRESSION = re.compile(r"#\s*noqa:\s*FO-SEC001\s*(?:[-—:]\s*(?P<reason>\S.*))?")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """`id()` of every string constant that is a docstring, so prose is not a finding."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            out.add(id(first.value))
    return out


def _redact(text: str) -> str:
    """Never echo the matched value — report its shape and length instead."""
    head = text[:6]
    return f"{head}…({len(text)} chars)"


def _folded_strings(tree: ast.AST, docstrings: set[int]) -> Iterator[tuple[int, str]]:
    """Yield `(lineno, value)` for every statically-known string in the module.

    **Constant folding is the point, not a nicety.** Splitting a value across `+` is the
    obvious way to slip a credential-shaped string past a scanner — `"Bea" + "rer …"` — and a
    checker that only read `ast.Constant` would be defeated by the very trick the rule exists
    to discourage. `"-" * 5` is folded for the same reason: it is how a PEM delimiter gets
    written without a contiguous literal.

    What is deliberately NOT folded is anything involving a name, a call or a parameter. That
    is what makes `backend/tests/synthetic_secrets.py` the sanctioned remedy rather than a
    loophole: its shapes are assembled from function arguments and module constants at
    runtime, so no static reading of the file yields a credential-shaped string — which is
    also exactly what keeps GitHub's and GitGuardian's scanners quiet.
    """

    def fold(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            for text_side, count_side in ((node.left, node.right), (node.right, node.left)):
                text = fold(text_side)
                if text is None:
                    continue
                if isinstance(count_side, ast.Constant) and isinstance(count_side.value, int):
                    # Bounded: a folded repeat is only interesting up to the length of the
                    # longest pattern, and an unbounded one is a memory hazard in a linter.
                    if 0 <= count_side.value <= 512:
                        return text * count_side.value
            return None
        return None

    seen: set[int] = set()
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if not isinstance(node, ast.Constant | ast.BinOp):
            continue
        # Only fold from the OUTERMOST expression, so `"a" + "b"` is examined once as `"ab"`
        # rather than three times. Children of a folded BinOp are recorded as seen.
        if id(node) in seen:
            continue
        value = fold(node)
        if value is None:
            continue
        for child in ast.walk(node):
            seen.add(id(child))
        yield node.lineno, value


def scan_file(path: Path) -> tuple[list[str], int]:
    """`(findings, literals_examined)` for one file."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - a broken test file fails elsewhere too
        return ([f"{path}:{exc.lineno}: FO-SEC001 could not parse: {exc.msg}"], 0)

    docstrings = _docstring_nodes(tree)
    findings: list[str] = []
    examined = 0

    for line, value in _folded_strings(tree, docstrings):
        examined += 1
        for rule, pattern in RULES:
            match = pattern.search(value)
            if match is None:
                continue
            context = lines[line - 1] if 0 < line <= len(lines) else ""
            suppression = SUPPRESSION.search(context)
            if suppression is not None:
                if suppression.group("reason"):
                    break
                findings.append(
                    f"{path}:{line}: FO-SEC001 suppression without a reason; write "
                    f"`# noqa: FO-SEC001 — <reason>`"
                )
                break
            findings.append(
                f"{path}:{line}: FO-SEC001 literal resembling {rule}: {_redact(match.group(0))}. "
                f"Assemble it at runtime via backend/tests/synthetic_secrets.py instead."
            )
            break

    return findings, examined


def _is_lint_fixture(path: Path) -> bool:
    parts = path.parts
    for i in range(len(parts) - len(_FIXTURE_DIR_SUFFIX) + 1):
        if parts[i : i + len(_FIXTURE_DIR_SUFFIX)] == _FIXTURE_DIR_SUFFIX:
            return True
    return False


def iter_python_files(roots: Iterable[Path]) -> Iterator[Path]:
    """Every `.py` under `roots`, skipping caches and this check's own fixtures.

    A fixture named EXPLICITLY on the command line is still scanned — that is how
    `test_check_test_credentials.py` drives `bad_credential.py` — so the exclusion applies to
    directory traversal only. Otherwise the tests could not reach the file whose whole job is
    to be flagged.
    """
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            if "__pycache__" not in root.parts:
                yield root
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if _is_lint_fixture(path):
                continue
            yield path


def check(roots: Iterable[Path]) -> tuple[list[str], int, int]:
    findings: list[str] = []
    files = 0
    literals = 0
    for path in iter_python_files(roots):
        try:
            relative = path.resolve().relative_to(REPO_ROOT).as_posix()
        except ValueError:
            relative = path.as_posix()
        if relative in EXEMPT_FILES:
            continue
        files += 1
        file_findings, examined = scan_file(path)
        findings.extend(file_findings)
        literals += examined
    return findings, files, literals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=None)
    args = parser.parse_args()

    roots = [Path(p) for p in (args.paths or [str(REPO_ROOT / "backend" / "tests")])]
    for root in roots:
        if not root.exists():
            print(f"FAIL: {root} does not exist", file=sys.stderr)
            return 1

    findings, files, literals = check(roots)

    if files == 0 or literals == 0:
        print(
            f"FAIL: scanned {files} file(s) and examined {literals} string literal(s). "
            "An empty scan would pass forever; check the paths.",
            file=sys.stderr,
        )
        return 1

    if findings:
        print("FAIL: credential-shaped literals in test sources:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        print(
            "\nNone of these need to be real to be a problem: a scanner cannot tell, and a "
            "blocked scan that gets waved through is worse than no scan. See "
            ".antigravity/steering/secret-safety.md and backend/tests/synthetic_secrets.py.",
            file=sys.stderr,
        )
        return 1

    print(f"check-test-credentials: {files} files, {literals} literals, 0 findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
