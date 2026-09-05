# SPDX-License-Identifier: FSL-1.1-ALv2
"""Reconcile what the code imports against what the manifests declare.

WHY THIS IS DERIVED AND NOT ASKED FOR

After a scan the product knows what the project actually imports: `file_dependencies` holds one row per
import with `resolved` saying whether it pointed at another file in the repository or at something
outside it. The unresolved ones, minus each language's standard library, ARE the third-party
dependencies. Nothing has to be guessed and nothing has to be asked.

That makes two real defects checkable for the first time:

  * an UNDECLARED import — the code imports a package no manifest names, so the build works on the
    machine where it was installed by hand and fails everywhere else. This is the stronger finding of
    the two, because it is a build that is already broken and does not know it.
  * an UNUSED declaration — a manifest names a package nothing imports. That is install time, image
    size and attack surface bought for nothing, and it is usually the residue of a removed feature.

WHERE THIS REFUSES TO ANSWER

A version is either KNOWN EXACTLY, CONSTRAINED, or UNKNOWN, and the three are kept apart:

  * exact, from a lockfile — safe to pin
  * constrained, from a manifest ("^4.18.2") — carried through verbatim, never resolved, because
    resolving means running the package manager against the operator's tree
  * unknown — reported as unknown. A FABRICATED VERSION IS THE WORST OUTCOME AVAILABLE: it installs, it
    differs from what the developer tested, and nothing reports the substitution. The same rule this
    codebase applies to an absent embedding and to `served_from`.

An ecosystem this module cannot parse produces NO findings rather than wrong ones. A missing answer is
visible; a confident wrong answer is not.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from pydantic import BaseModel

#: Standard-library module names that must never be reported as undeclared dependencies.
#:
#: Deliberately a CURATED LIST rather than a heuristic. "Has no dot and is not in the manifest" would
#: flag every `os`, `sys` and `fs` in the repository as a missing package, which is the kind of noise
#: that teaches people to ignore a report. Only the names that actually appear in scanned code need to be
#: here, and an unlisted stdlib module produces a false finding — so this errs towards completeness on
#: the common modules and the checks say what they examined.
PYTHON_STDLIB: Final[frozenset[str]] = frozenset(
    {
        "abc",
        "argparse",
        "asyncio",
        "base64",
        "collections",
        "contextlib",
        "copy",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "glob",
        "hashlib",
        "hmac",
        "html",
        "http",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "logging",
        "math",
        "os",
        "pathlib",
        "pickle",
        "platform",
        "queue",
        "random",
        "re",
        "secrets",
        "shutil",
        "signal",
        "socket",
        "sqlite3",
        "ssl",
        "statistics",
        "string",
        "struct",
        "subprocess",
        "sys",
        "tempfile",
        "textwrap",
        "threading",
        "time",
        "tomllib",
        "traceback",
        "types",
        "typing",
        "unittest",
        "urllib",
        "uuid",
        "warnings",
        "weakref",
        "zipfile",
    }
)

NODE_BUILTINS: Final[frozenset[str]] = frozenset(
    {
        "assert",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "crypto",
        "dgram",
        "dns",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "querystring",
        "readline",
        "stream",
        "string_decoder",
        "timers",
        "tls",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "worker_threads",
        "zlib",
    }
)

GO_STDLIB_PREFIXES: Final[tuple[str, ...]] = (
    "archive/",
    "bufio",
    "bytes",
    "compress/",
    "container/",
    "context",
    "crypto",
    "crypto/",
    "database/",
    "encoding",
    "encoding/",
    "errors",
    "flag",
    "fmt",
    "go/",
    "hash",
    "hash/",
    "html",
    "html/",
    "image",
    "io",
    "io/",
    "log",
    "log/",
    "math",
    "math/",
    "mime",
    "mime/",
    "net",
    "net/",
    "os",
    "os/",
    "path",
    "path/",
    "reflect",
    "regexp",
    "runtime",
    "sort",
    "strconv",
    "strings",
    "sync",
    "sync/",
    "syscall",
    "testing",
    "text/",
    "time",
    "unicode",
    "unicode/",
    "unsafe",
)


class DependencyVersion(BaseModel):
    """A version, and how firmly it is known."""

    #: "exact" from a lockfile, "constrained" from a manifest, "unknown" when neither says.
    confidence: str
    #: The value verbatim. Empty when `confidence` is "unknown" — never a substitute.
    value: str = ""
    #: The file the answer came from, so it can be checked. Empty when unknown.
    source: str = ""

    model_config = {"frozen": True}


class DependencyFacts(BaseModel):
    """What the code imports, what the manifests declare, and where they disagree."""

    ecosystem: str
    manifest_path: str = ""
    lockfile_path: str = ""
    #: Third-party packages the code imports, sorted.
    imported: tuple[str, ...] = ()
    #: Packages a manifest declares, sorted.
    declared: tuple[str, ...] = ()
    #: Imported and not declared. The stronger finding: the build is already broken elsewhere.
    undeclared: tuple[str, ...] = ()
    #: Declared and not imported anywhere.
    unused: tuple[str, ...] = ()
    #: Declared packages whose version is a floating constraint or absent entirely.
    unpinned: tuple[str, ...] = ()
    versions: Mapping[str, DependencyVersion] = {}

    model_config = {"frozen": True}


def _python_package(specifier: str) -> str:
    """`fastapi.FastAPI` -> `fastapi`, `a.b.c` -> `a`.

    The distribution name and the import name can differ (`PIL` for `pillow`, `yaml` for `PyYAML`), and
    this does NOT try to map between them — a guessed mapping would produce false undeclared findings.
    The comparison is done on both sides with the same normalisation, so a project whose import and
    distribution names agree is judged correctly and one where they differ is reported for a human.
    """
    return specifier.split(".", 1)[0].strip().lower().replace("_", "-")


def _node_package(specifier: str) -> str:
    """`node:http` -> `http`, `@scope/pkg/sub` -> `@scope/pkg`, `pkg/sub` -> `pkg`."""
    cleaned = specifier.removeprefix("node:").strip()
    if cleaned.startswith("@"):
        parts = cleaned.split("/")
        return "/".join(parts[:2]).lower()
    return cleaned.split("/", 1)[0].lower()


def _is_relative(specifier: str) -> bool:
    return specifier.startswith((".", "/")) or specifier.startswith("..")


#: File extensions that belong to each ecosystem.
#:
#: WHY AN IMPORT HAS TO BE ATTRIBUTED AT ALL. Without this, every unresolved specifier is offered to
#: every ecosystem — and `fastapi` is a perfectly valid npm package name, so a pure Python project was
#: reported as having Node imports that no `package.json` declared, and `dependency_manifest_present`
#: was emitted twice. A finding about nothing is worse than no finding.
#:
#: The importing FILE decides. A `.py` file's imports are Python imports whatever they are called.
ECOSYSTEM_EXTENSIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "python": (".py", ".pyi"),
    "node": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    "go": (".go",),
    "rust": (".rs",),
    "ruby": (".rb",),
    "php": (".php",),
}


def _belongs_to(ecosystem: str, source_path: str) -> bool:
    """Whether a file of this path is one this ecosystem's imports come from."""
    suffixes = ECOSYSTEM_EXTENSIONS.get(ecosystem, ())
    return bool(suffixes) and source_path.replace("\\", "/").lower().endswith(suffixes)


def imported_packages(ecosystem: str, specifiers: Iterable[tuple[str, str, bool]]) -> tuple[str, ...]:
    """Third-party package names from `(source_path, raw_specifier, resolved)` rows.

    `resolved=True` means the import pointed at another file in this repository, so it is not a
    dependency. Relative specifiers and standard-library names are excluded for the same reason: they
    are not things a manifest should declare.

    The FIRST member is the path of the file the import appears in, and it is what decides which
    ecosystem the import belongs to. Attributing by the specifier's shape instead is not possible: the
    same string is a legal package name in more than one ecosystem.
    """
    found: set[str] = set()
    for source_path, specifier, resolved in specifiers:
        if resolved or not specifier or _is_relative(specifier):
            continue
        if not _belongs_to(ecosystem, source_path):
            continue
        if ecosystem == "python":
            name = _python_package(specifier)
            if name and name.replace("-", "_") not in PYTHON_STDLIB and name not in PYTHON_STDLIB:
                found.add(name)
        elif ecosystem == "node":
            name = _node_package(specifier)
            if name and name not in NODE_BUILTINS:
                found.add(name)
        elif ecosystem == "go":
            name = specifier.strip()
            if name and not name.startswith(GO_STDLIB_PREFIXES) and "." in name.split("/", 1)[0]:
                # A Go import path is third-party only when its first segment is a domain. `fmt` is
                # stdlib; `github.com/x/y` is not.
                found.add(name)
    return tuple(sorted(found))


def _parse_requirements(body: str) -> dict[str, DependencyVersion]:
    """`requirements.txt`, keeping the operator's own constraint verbatim."""
    out: dict[str, DependencyVersion] = {}
    for line in body.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped or stripped.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9._-]+)\s*(\[.*?\])?\s*(.*)$", stripped)
        if not match:
            continue
        name = match.group(1).lower().replace("_", "-")
        constraint = (match.group(3) or "").strip()
        if constraint.startswith("=="):
            out[name] = DependencyVersion(confidence="exact", value=constraint, source="requirements.txt")
        elif constraint:
            out[name] = DependencyVersion(confidence="constrained", value=constraint, source="requirements.txt")
        else:
            # Named with no constraint at all. NOT invented: reported as unknown, which is what makes
            # `unpinned` a real finding rather than a style note.
            out[name] = DependencyVersion(confidence="unknown", source="requirements.txt")
    return out


def _parse_pyproject(body: str) -> dict[str, DependencyVersion]:
    try:
        document = tomllib.loads(body)
    except (tomllib.TOMLDecodeError, ValueError):
        # An unparseable manifest yields nothing rather than a partial reading. A half-parsed
        # dependency list would report the rest of the file's packages as undeclared.
        return {}
    out: dict[str, DependencyVersion] = {}
    project = document.get("project")
    entries = project.get("dependencies") if isinstance(project, Mapping) else None
    for entry in entries or []:
        if not isinstance(entry, str):
            continue
        match = re.match(r"^([A-Za-z0-9._-]+)\s*(\[.*?\])?\s*(.*)$", entry.strip())
        if not match:
            continue
        name = match.group(1).lower().replace("_", "-")
        constraint = (match.group(3) or "").strip()
        confidence = "exact" if constraint.startswith("==") else ("constrained" if constraint else "unknown")
        out[name] = DependencyVersion(confidence=confidence, value=constraint, source="pyproject.toml")
    return out


def _parse_package_json(body: str) -> tuple[dict[str, DependencyVersion], dict[str, DependencyVersion]]:
    """`(runtime, dev)`, kept apart because collapsing them is the mistake this exists to avoid.

    A manifest that promotes every devDependency to a runtime dependency ships a test runner into a
    production image, and it is hard to undo later because nothing records which were which.
    """
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}, {}
    if not isinstance(document, Mapping):
        return {}, {}

    def read(section: str) -> dict[str, DependencyVersion]:
        block = document.get(section)
        if not isinstance(block, Mapping):
            return {}
        out: dict[str, DependencyVersion] = {}
        for name, constraint in block.items():
            text = str(constraint).strip()
            exact = bool(re.fullmatch(r"\d+\.\d+\.\d+", text))
            out[str(name).lower()] = DependencyVersion(
                confidence="exact" if exact else ("constrained" if text else "unknown"),
                value=text,
                source="package.json",
            )
        return out

    return read("dependencies"), read("devDependencies")


def _parse_go_mod(body: str) -> dict[str, DependencyVersion]:
    """`go.mod` require directives. A Go module version is always exact by construction."""
    out: dict[str, DependencyVersion] = {}
    in_block = False
    for line in body.splitlines():
        stripped = line.split("//", 1)[0].strip()
        if stripped.startswith("require ("):
            in_block = True
            continue
        if in_block and stripped == ")":
            in_block = False
            continue
        candidate = (
            stripped.removeprefix("require ").strip()
            if stripped.startswith("require ")
            else (stripped if in_block else "")
        )
        if not candidate:
            continue
        parts = candidate.split()
        if len(parts) >= 2:
            out[parts[0]] = DependencyVersion(confidence="exact", value=parts[1], source="go.mod")
    return out


#: Manifest and lockfile names per ecosystem, in the order they are preferred.
ECOSYSTEM_FILES: Final[Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "python": (("pyproject.toml", "requirements.txt"), ("uv.lock", "poetry.lock", "requirements.lock")),
    "node": (("package.json",), ("pnpm-lock.yaml", "package-lock.json", "yarn.lock")),
    "go": (("go.mod",), ("go.sum",)),
    "rust": (("Cargo.toml",), ("Cargo.lock",)),
    "ruby": (("Gemfile",), ("Gemfile.lock",)),
    "php": (("composer.json",), ("composer.lock",)),
}


def _find(paths: Sequence[str], names: Iterable[str]) -> str:
    lowered = {path.replace("\\", "/").lower(): path.replace("\\", "/") for path in paths}
    for name in names:
        for lower, original in sorted(lowered.items()):
            if lower == name.lower() or lower.endswith("/" + name.lower()):
                return original
    return ""


def reconcile(
    *,
    ecosystem: str,
    paths: Sequence[str],
    contents: Mapping[str, str],
    specifiers: Iterable[tuple[str, str, bool]],
) -> DependencyFacts | None:
    """Compare imports against declarations for one ecosystem, or return None when it cannot be judged.

    None means "this module cannot answer for this ecosystem", which is different from "there is nothing
    wrong" and is why the caller emits no check rather than a passing one.
    """
    if ecosystem not in ECOSYSTEM_FILES:
        return None
    manifest_names, lock_names = ECOSYSTEM_FILES[ecosystem]
    manifest_path = _find(paths, manifest_names)
    lockfile_path = _find(paths, lock_names)

    imported = imported_packages(ecosystem, specifiers)
    if not manifest_path:
        # Nothing declares anything, so everything imported is undeclared. That is a true and useful
        # finding rather than an inability to answer.
        return DependencyFacts(
            ecosystem=ecosystem,
            lockfile_path=lockfile_path,
            imported=imported,
            undeclared=imported,
        )

    body = contents.get(manifest_path.lower(), "") or contents.get(manifest_path, "")
    if not body:
        # The manifest is indexed but its body was not loaded, so declarations are unknown. Reporting
        # every import as undeclared here would be an artefact of what was loaded, not a fact about the
        # repository.
        return None

    runtime: dict[str, DependencyVersion] = {}
    if ecosystem == "python":
        runtime = (
            _parse_pyproject(body) if manifest_path.lower().endswith("pyproject.toml") else _parse_requirements(body)
        )
    elif ecosystem == "node":
        runtime, dev = _parse_package_json(body)
        # Dev dependencies count as DECLARED for the undeclared check — importing a test library in a
        # test file is correct — but they are not runtime dependencies and are not reported as unused
        # merely because production code does not import them.
        runtime = {**dev, **runtime}
    elif ecosystem == "go":
        runtime = _parse_go_mod(body)

    if not runtime:
        return None

    declared = tuple(sorted(runtime))
    imported_set = set(imported)
    return DependencyFacts(
        ecosystem=ecosystem,
        manifest_path=manifest_path,
        lockfile_path=lockfile_path,
        imported=imported,
        declared=declared,
        undeclared=tuple(sorted(imported_set - set(declared))),
        unused=tuple(sorted(set(declared) - imported_set)),
        unpinned=tuple(
            sorted(name for name, version in runtime.items() if version.confidence != "exact" and not lockfile_path)
        ),
        versions=runtime,
    )
