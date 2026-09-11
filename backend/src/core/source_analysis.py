# SPDX-License-Identifier: FSL-1.1-ALv2
"""Two checks that were satisfiable without doing the work, made to measure the work.

WHAT WAS WRONG

    automated_tests_present   (35 points)  passed on `_has_test_evidence(paths)` - a PATH matched
    centralised_configuration (25 points)  passed on `bool(config_dir)`   - a DIRECTORY existed

Both are presence checks, so both could be satisfied by a generator emitting an empty file. 60 points
for `tests/test_placeholder.py` containing nothing and a `config/` directory containing nothing. That
is the recurring defect in this codebase in its purest form: something with the right name that
nothing verifies, and the score moving while the project does not improve.

WHY NOT TREE-SITTER, WHICH IS WHAT WAS ASKED FOR

Because it does not work, and that is worth stating precisely rather than working around silently.
`agent/internal/scanner/ast` loads the embedded Wasm grammars, verifies their SHA-256 and compiles
them through wazero - all of that is real. Then `Parser.Parse` ignores them and returns a single
fabricated node:

    root := &Node{Type: "program", StartByte: 0, EndByte: uint32(len(src))}

No children, for any input, in any language. Measured on 63 bytes of Python containing an import, a
call and a function definition: `root type="python"... children=0`. The comment above it reads
"Structural AST tree generation over Wasm module runtime", which is the thing it does not do. There is
also no `tree_sitter` in the backend's dependencies, so there was no working parser on either side.

So this module uses Python's `ast`, which IS a real parser, for Python sources. That is not a
substitute for tree-sitter across every language and the limits are stated on each function rather
than left to be discovered. Fixing the Wasm parser is the correct long-term answer and is a separate
piece of work; scoring a project against a parser that returns nothing would have been worse than
either.

WHAT THIS CAN AND CANNOT CATCH is documented per function, because a check whose blind spots are
unknown is a check that will be trusted further than it deserves.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

#: Directories whose contents constitute the configuration module.
_CONFIG_DIRS: Final[tuple[str, ...]] = ("config/", "configs/", "settings/")

#: FILENAMES that are the configuration module, matched on the base name so `myconfig.jsx` and
#: `config.test.js` do not qualify by containing the word.
#:
#: `config.js` and its module variants were missing from the first version and it cost a real project a
#: real 25 points: the demo project's `src/config.js` opens with "Centralized Configuration Module",
#: holds all five of its environment reads, and was reported as "5 of 5 reads outside any configuration
#: module" because the pattern list only knew Python and TypeScript names. A check that penalises the
#: exact thing it is asking for is worse than the presence check it replaced.
_CONFIG_FILENAMES: Final[frozenset[str]] = frozenset(
    {
        "settings.py",
        "config.py",
        "conf.py",
        "config.js",
        "config.mjs",
        "config.cjs",
        "config.ts",
        "settings.js",
        "settings.ts",
        "env.py",
        "env.js",
        "env.ts",
    }
)

#: Suffix forms like `app.config.ts` or `vite.config.js`, which are configuration by convention.
_CONFIG_SUFFIXES: Final[tuple[str, ...]] = (".config.ts", ".config.js", ".config.mjs", ".config.cjs")

#: Source extensions this module will analyse. Anything else is not counted in either direction: a
#: language we cannot read must not be reported as compliant OR as scattered, because both would be
#: invented.
_PY = (".py",)
_JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")

#: `process.env.NAME` and `process.env["NAME"]`. A token pattern, not a parse: the note on
#: `env_reads_in_javascript` says what that costs.
_JS_ENV = re.compile(r"process\s*\.\s*env\s*(?:\.\s*([A-Za-z_$][\w$]*)|\[\s*['\"]([^'\"]+)['\"]\s*\])")

#: A vendored or generated tree is not the project's own source. Reads inside one are neither the
#: project's fault nor its credit.
_EXCLUDED_DIRS: Final[tuple[str, ...]] = (
    "node_modules/",
    ".venv/",
    "venv/",
    "site-packages/",
    "dist/",
    "build/",
    ".next/",
    "vendor/",
    "__pycache__/",
    ".git/",
)


def _normalise(path: str) -> str:
    return path.replace("\\", "/").lower()


def is_config_module(path: str) -> bool:
    """Whether this path is the place configuration is supposed to be read."""
    lowered = _normalise(path)
    if any(directory in lowered for directory in _CONFIG_DIRS):
        return True
    base = lowered.rsplit("/", 1)[-1]
    if base in _CONFIG_FILENAMES:
        return True
    return any(base.endswith(suffix) for suffix in _CONFIG_SUFFIXES)


def _is_project_source(path: str) -> bool:
    lowered = _normalise(path)
    if any(excluded in lowered for excluded in _EXCLUDED_DIRS):
        return False
    return lowered.endswith(_PY) or lowered.endswith(_JS)


@dataclass(frozen=True)
class EnvRead:
    """One place the source reads an environment variable."""

    path: str
    line: int
    name: str
    #: The expression as recognised, for the finding's evidence.
    form: str


def env_reads_in_python(path: str, body: str) -> tuple[EnvRead, ...]:
    """Environment reads in a Python source, found by parsing it.

    RECOGNISED: `os.getenv(...)`, `os.environ.get(...)`, `os.environ[...]`, and the bare `getenv(...)`
    or `environ[...]` that `from os import ...` produces.

    CANNOT CATCH, and these are real gaps rather than theoretical ones:
      * a read through an alias - `e = os.environ` then `e["KEY"]`, because that needs dataflow
      * a read built dynamically - `os.getenv(prefix + name)`, where the name is not a literal. The
        SITE is still counted; only the variable's name is unknown, which is the right trade: the
        check is about where reads happen, not which keys exist.
      * a read inside `exec`/`eval` or a C extension
      * a file this project cannot parse at all - a SyntaxError yields no reads, which fails OPEN.
        That is deliberate and stated: inventing reads in a file we could not read would be worse, and
        an unparseable source is usually not the file doing the configuration.
    """
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError, RecursionError):
        return ()

    reads: list[EnvRead] = []
    for node in ast.walk(tree):
        # os.getenv("X") / os.environ.get("X") / getenv("X")
        if isinstance(node, ast.Call):
            target = node.func
            name = ""
            form = ""
            if isinstance(target, ast.Attribute) and target.attr in {"getenv", "get"}:
                owner = target.value
                if isinstance(owner, ast.Attribute) and owner.attr == "environ":
                    form = "os.environ.get"
                elif isinstance(owner, ast.Name) and owner.id in {"os", "environ"}:
                    form = f"{owner.id}.{target.attr}"
                if form:
                    name = _first_string_argument(node)
            elif isinstance(target, ast.Name) and target.id == "getenv":
                form = "getenv"
                name = _first_string_argument(node)
            if form:
                reads.append(EnvRead(path=path, line=getattr(node, "lineno", 0), name=name, form=form))
        # os.environ["X"] / environ["X"]
        elif isinstance(node, ast.Subscript):
            owner = node.value
            if (isinstance(owner, ast.Attribute) and owner.attr == "environ") or (
                isinstance(owner, ast.Name) and owner.id == "environ"
            ):
                key = node.slice
                name = key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else ""
                reads.append(EnvRead(path=path, line=getattr(node, "lineno", 0), name=name, form="os.environ[...]"))
    return tuple(reads)


def _first_string_argument(call: ast.Call) -> str:
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


def env_reads_in_javascript(path: str, body: str) -> tuple[EnvRead, ...]:
    """Environment reads in a JavaScript or TypeScript source.

    A TOKEN SCAN, NOT A PARSE, and that is a real limitation rather than a shortcut worth hiding.
    `tree_sitter` is not a backend dependency and the agent's Wasm parser does not parse, so there is
    no JS parser available here. The consequences, stated:

      * a `process.env` written inside a string or a comment is counted as a read it is not
      * destructuring - `const {PORT} = process.env` - is counted once at the `process.env`, which is
        correct as a SITE but understates the number of variables
      * a read through an alias - `const env = process.env` then `env.PORT` - is missed entirely

    It fails towards counting reads it can see rather than claiming a project is centralised, which is
    the safe direction for a check whose purpose is to refuse an unearned pass.
    """
    reads: list[EnvRead] = []
    for index, line in enumerate(body.splitlines(), start=1):
        for match in _JS_ENV.finditer(line):
            reads.append(
                EnvRead(path=path, line=index, name=match.group(1) or match.group(2) or "", form="process.env")
            )
    return tuple(reads)


def env_reads(paths: Iterable[str], contents: Mapping[str, str]) -> tuple[EnvRead, ...]:
    """Every environment read this analysis can see, across the project's own sources."""
    found: list[EnvRead] = []
    for path in paths:
        if not _is_project_source(path):
            continue
        body = contents.get(path.lower()) or contents.get(path) or ""
        if not body:
            continue
        lowered = _normalise(path)
        if lowered.endswith(_PY):
            found.extend(env_reads_in_python(path, body))
        else:
            found.extend(env_reads_in_javascript(path, body))
    return tuple(found)


@dataclass(frozen=True)
class Centralisation:
    """How much of the project's configuration actually flows through the config module."""

    #: Reads found in the configuration module - the ones done in the right place.
    centralised: int
    #: Reads found anywhere else, each one a place configuration enters the program unvalidated.
    scattered: int
    #: The worst offenders, for the finding. Path and line only.
    examples: tuple[str, ...]
    #: Whether a configuration module exists at all.
    module_path: str
    #: A source file this analysis actually read, so a pass with nothing to centralise can still cite
    #: the file it examined rather than passing with empty evidence.
    examined_path: str = ""

    @property
    def total(self) -> int:
        return self.centralised + self.scattered

    @property
    def passed(self) -> bool:
        """Every read the analysis can see happens in the configuration module.

        A project with NO reads at all passes: there is nothing to centralise, and that is a true
        statement about the project rather than a loophole - it cannot be reached by adding an empty
        config module, only by genuinely not reading the environment.
        """
        return self.scattered == 0

    @property
    def earned(self) -> int | None:
        """Proportional credit, so fixing four of five reads moves the number."""
        if self.total == 0:
            return None
        return self.centralised


def centralisation(paths: Iterable[str], contents: Mapping[str, str]) -> Centralisation:
    """Whether configuration reads resolve through the module, and in what proportion.

    THE QUESTION CHANGED, DELIBERATELY. It used to be "does a `config/` directory exist". It is now
    "does configuration actually go through it". A directory proves nothing: the failure this check
    exists to catch is a service that reads `os.getenv("DATABASE_URL")` in six modules and dies in
    production because the seventh spelled it differently, and that service has a `config/` directory.
    """
    reads = env_reads(paths, contents)
    module = ""
    examined = ""
    for path in sorted(paths):
        if not _is_project_source(path):
            continue
        body = contents.get(path.lower()) or contents.get(path) or ""
        if body and not examined:
            examined = path
        if is_config_module(path) and not module:
            module = path

    centralised = 0
    scattered = 0
    examples: list[str] = []
    for read in reads:
        if is_config_module(read.path):
            centralised += 1
        else:
            scattered += 1
            if len(examples) < 5:
                named = f" reading {read.name}" if read.name else ""
                examples.append(f"{read.path}:{read.line}{named} via {read.form}")
    return Centralisation(
        centralised=centralised,
        scattered=scattered,
        examples=tuple(examples),
        module_path=module,
        examined_path=examined,
    )


@dataclass(frozen=True)
class TestSubstance:
    """Whether the indexed test files contain tests that assert anything."""

    #: Files that look like tests and were parseable.
    files: int
    #: Test functions or cases found.
    cases: int
    #: Assertions that are not trivially true.
    meaningful_assertions: int
    #: Assertions whose truth does not depend on the program - `assert True`, `expect(1).toBe(1)`.
    vacuous_assertions: int
    examples: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """A suite must have at least one case and at least one assertion that could fail.

        WHAT THIS CANNOT DO, STATED PLAINLY: it does not run anything. A suite that parses, declares
        cases and asserts on real expressions passes here even if it errors on the first line at
        runtime. Execution is the correct check and needs an agent operation that does not exist yet;
        this closes the hole that mattered most - an empty or vacuous file taking 35 points - without
        claiming to be the execution check.
        """
        return self.cases > 0 and self.meaningful_assertions > 0


_VACUOUS_PY: Final[tuple[str, ...]] = ("True", "1", "not False", "...")


def test_substance(paths: Iterable[str], contents: Mapping[str, str]) -> TestSubstance:
    """Read the indexed test files and report whether they test anything.

    RECOGNISED as a case: a Python `def test_*` or a method on a `Test*` class; a JavaScript
    `it(...)`, `test(...)` or `describe(...)` body.

    RECOGNISED as vacuous: `assert True`, `assert 1`, `assert not False`, a bare `...`, and
    `expect(<literal>).toBe(<same literal>)`. This is a floor, not a judgement of quality - a test
    asserting something true but irrelevant cannot be distinguished from a useful one by reading it,
    and pretending otherwise would be the same overreach as the presence check it replaces.
    """
    files = 0
    cases = 0
    meaningful = 0
    vacuous = 0
    examples: list[str] = []

    for path in sorted(paths):
        lowered = _normalise(path)
        if any(excluded in lowered for excluded in _EXCLUDED_DIRS):
            continue
        looks_like_test = (
            "test" in lowered.rsplit("/", 1)[-1] or "spec" in lowered.rsplit("/", 1)[-1] or "/tests/" in lowered
        )
        if not looks_like_test or not _is_project_source(path):
            continue
        body = contents.get(path.lower()) or contents.get(path) or ""
        if not body.strip():
            # An EMPTY test file is the exact artifact this check exists to refuse.
            files += 1
            if len(examples) < 5:
                examples.append(f"{path} is empty")
            continue
        files += 1

        if lowered.endswith(_PY):
            found_cases, found_meaningful, found_vacuous, note = _python_test_substance(path, body)
        else:
            found_cases, found_meaningful, found_vacuous, note = _javascript_test_substance(path, body)
        cases += found_cases
        meaningful += found_meaningful
        vacuous += found_vacuous
        if note and len(examples) < 5:
            examples.append(note)

    return TestSubstance(
        files=files,
        cases=cases,
        meaningful_assertions=meaningful,
        vacuous_assertions=vacuous,
        examples=tuple(examples),
    )


def _python_test_substance(path: str, body: str) -> tuple[int, int, int, str]:
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError, RecursionError):
        return (0, 0, 0, f"{path} could not be parsed, so nothing in it counts as a test")

    cases = 0
    meaningful = 0
    vacuous = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test"):
            cases += 1
        elif isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) or ast.unparse(node.test).strip() in _VACUOUS_PY:
                vacuous += 1
            else:
                meaningful += 1
    if cases and not meaningful:
        return (cases, meaningful, vacuous, f"{path} declares {cases} test(s) and asserts nothing that can fail")
    return (cases, meaningful, vacuous, "")


_JS_CASE = re.compile(r"\b(?:it|test)\s*(?:\.\s*\w+\s*)?\(")
_JS_EXPECT = re.compile(r"\bexpect\s*\(([^)]*)\)\s*\.\s*(\w+)\s*\(([^)]*)\)")

#: `node:assert` and its methods: `assert(...)`, `assert.ok(...)`, `assert.strictEqual(a, b)`.
#:
#: MISSING THIS COST A REAL PROJECT 35 POINTS. The demo project's suite declares two cases and makes
#: five `assert.ok(...)` calls against values the application computes, and it was reported as
#: "declares 2 case(s) and asserts nothing that can fail" because only `expect(...)` was recognised.
#: The standard library assertion module is the most likely thing a project with no test framework
#: will use, so omitting it made the check wrong for exactly the projects it was aimed at.
_JS_ASSERT = re.compile(r"\bassert\s*(?:\.\s*(\w+)\s*)?\(([^)]*)\)")

#: Arguments whose truth does not depend on the program.
_VACUOUS_JS: Final[frozenset[str]] = frozenset({"true", "1", "!false", "!0"})


def _javascript_test_substance(path: str, body: str) -> tuple[int, int, int, str]:
    """Token-based, for the same reason `env_reads_in_javascript` is: there is no JS parser here."""
    cases = len(_JS_CASE.findall(body))
    meaningful = 0
    vacuous = 0
    for match in _JS_EXPECT.finditer(body):
        actual, _matcher, expected = (part.strip() for part in match.groups())
        if actual and actual == expected:
            vacuous += 1
        else:
            meaningful += 1
    for match in _JS_ASSERT.finditer(body):
        _method, args = match.groups()
        first = args.split(",", 1)[0].strip().lower()
        if first in _VACUOUS_JS or not first:
            vacuous += 1
        else:
            meaningful += 1
    if cases and not meaningful:
        return (cases, meaningful, vacuous, f"{path} declares {cases} case(s) and asserts nothing that can fail")
    return (cases, meaningful, vacuous, "")
