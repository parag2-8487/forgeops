# SPDX-License-Identifier: FSL-1.1-ALv2
"""The manifest and the code must be reconciled, and every finding must name packages.

`file_dependencies` has recorded the import graph since revision `0003` and the readiness score never
read it. So a repository could hold a manifest, a lockfile, and a build that only worked on the machine
where somebody had installed the missing package by hand — and the report had nothing to say about it.

Two findings become possible once the graph is read:

  * an UNDECLARED import: the build already fails on any clean checkout, which is usually CI, or a new
    colleague, or a production image.
  * an UNUSED declaration: install time, image size and attack surface bought for nothing.

One rule governs versions: exact, constrained, or UNKNOWN — never invented. A fabricated version
installs, differs from what was tested, and reports nothing.
"""

from __future__ import annotations

import pytest
from src.core.dependency_manifest import imported_packages, reconcile
from src.core.readiness import IndexEvidence, ReadinessEngine

pytestmark = [pytest.mark.mandatory]

REQUIREMENTS = "fastapi==0.115.0\nuvicorn\nboto3>=1.0\n"

PYTHON_IMPORTS = (
    ("src/main.py", "fastapi", False),
    ("src/main.py", "fastapi.FastAPI", False),
    ("src/main.py", "requests", False),
    ("src/main.py", "uvicorn", False),
    ("src/main.py", "os", False),
    ("src/main.py", "./config", True),
)


def _facts(paths=("requirements.txt",)):
    return reconcile(
        ecosystem="python",
        paths=paths,
        contents={"requirements.txt": REQUIREMENTS},
        specifiers=PYTHON_IMPORTS,
    )


class TestWhatCountsAsADependency:
    def test_the_standard_library_is_not_a_dependency(self) -> None:
        """Flagging every `os` and `sys` as a missing package is the noise that gets a report ignored."""
        assert imported_packages("python", (("a.py", "os", False), ("a.py", "sys", False))) == ()

    def test_a_resolved_import_is_not_a_dependency(self) -> None:
        """`resolved` means the import pointed at another file in this repository."""
        assert imported_packages("python", (("a.py", "myapp.helpers", True),)) == ()

    def test_a_relative_import_is_not_a_dependency(self) -> None:
        rows = (("a.js", "./config", False), ("a.js", "../lib", False))
        assert imported_packages("node", rows) == ()

    def test_a_submodule_import_names_its_package(self) -> None:
        assert imported_packages("python", (("a.py", "fastapi.responses", False),)) == ("fastapi",)
        assert imported_packages("node", (("a.js", "lodash/get", False),)) == ("lodash",)
        # A scoped npm package keeps both segments; truncating to `@scope` names nothing installable.
        assert imported_packages("node", (("a.js", "@scope/pkg/sub", False),)) == ("@scope/pkg",)

    def test_an_import_is_attributed_to_the_language_of_its_file(self) -> None:
        """THE BUG THIS PINS.

        Without attribution every unresolved specifier is offered to every ecosystem, and `fastapi` is a
        perfectly valid npm package name — so a pure Python project was reported as having undeclared
        Node dependencies and `dependency_manifest_present` was emitted twice.
        """
        rows = (("src/main.py", "fastapi", False),)
        assert imported_packages("python", rows) == ("fastapi",)
        assert imported_packages("node", rows) == ()

    def test_a_go_stdlib_path_is_not_a_dependency(self) -> None:
        """A Go import is third-party only when its first segment is a domain."""
        assert imported_packages("go", (("m.go", "net/http", False),)) == ()
        assert imported_packages("go", (("m.go", "github.com/acme/x", False),)) == ("github.com/acme/x",)


class TestVersionsAreKnownOrAdmitted:
    def test_the_three_confidences_are_kept_apart(self) -> None:
        facts = _facts()
        assert facts is not None
        assert facts.versions["fastapi"].confidence == "exact"
        assert facts.versions["fastapi"].value == "==0.115.0"
        assert facts.versions["boto3"].confidence == "constrained"
        assert facts.versions["boto3"].value == ">=1.0"
        # Named with no constraint at all: unknown, and NOT filled in.
        assert facts.versions["uvicorn"].confidence == "unknown"
        assert facts.versions["uvicorn"].value == ""

    def test_every_version_names_the_file_it_came_from(self) -> None:
        """A version with no source cannot be checked, and is therefore an assertion."""
        facts = _facts()
        assert facts is not None
        for version in facts.versions.values():
            assert version.source == "requirements.txt"

    def test_an_unparseable_manifest_yields_nothing_rather_than_a_partial_reading(self) -> None:
        """A half-parsed manifest would report the rest of its packages as undeclared."""
        result = reconcile(
            ecosystem="python",
            paths=("pyproject.toml",),
            contents={"pyproject.toml": "this is not toml ]["},
            specifiers=PYTHON_IMPORTS,
        )
        assert result is None

    def test_an_ecosystem_this_cannot_parse_is_not_judged(self) -> None:
        """None means "cannot answer", which the caller must not render as a pass."""
        assert reconcile(ecosystem="elixir", paths=("mix.exs",), contents={}, specifiers=()) is None


class TestTheDisagreementsAreNamed:
    def test_an_undeclared_import_is_reported(self) -> None:
        facts = _facts()
        assert facts is not None
        assert facts.undeclared == ("requests",)

    def test_an_unused_declaration_is_reported(self) -> None:
        facts = _facts()
        assert facts is not None
        assert facts.unused == ("boto3",)

    def test_a_lockfile_satisfies_pinning_even_with_floating_constraints(self) -> None:
        """A lockfile pins the whole resolved tree, so the constraint above it is not the risk."""
        facts = _facts(paths=("requirements.txt", "uv.lock"))
        assert facts is not None
        assert facts.lockfile_path == "uv.lock"
        assert facts.unpinned == ()

    def test_node_dev_dependencies_count_as_declared(self) -> None:
        """Importing a test library in a test file is correct, not an undeclared dependency."""
        facts = reconcile(
            ecosystem="node",
            paths=("package.json",),
            contents={
                "package.json": ('{"dependencies": {"express": "^4.18.2"}, "devDependencies": {"vitest": "^2.0.0"}}')
            },
            specifiers=(("src/a.js", "express", False), ("test/a.test.js", "vitest", False)),
        )
        assert facts is not None
        assert facts.undeclared == ()


class TestTheChecksNameSpecificPackages:
    """ "Dependencies are unpinned" sends a reader to the whole manifest. Naming one sends them to a line."""

    def _checks(self) -> dict:
        evidence = IndexEvidence(
            paths=("requirements.txt", "src/main.py"),
            contents={"requirements.txt": REQUIREMENTS},
            dependency_specifiers=PYTHON_IMPORTS,
        )
        return {c.id: c for c in ReadinessEngine().evaluate(evidence).checks}

    def test_the_undeclared_package_is_named(self) -> None:
        check = self._checks()["every_imported_package_is_declared"]
        assert not check.passed
        assert "requests" in check.found
        assert "requirements.txt" in check.found

    def test_the_unused_package_is_named(self) -> None:
        check = self._checks()["no_unused_declared_packages"]
        assert not check.passed
        assert "boto3" in check.found

    def test_the_unpinned_packages_are_named(self) -> None:
        check = self._checks()["declared_versions_are_pinned"]
        assert not check.passed
        assert "boto3" in check.found
        assert "uvicorn" in check.found

    def test_only_the_detected_ecosystem_is_judged(self) -> None:
        """A Python project must not be told it is missing a package.json."""
        evidence = IndexEvidence(
            paths=("requirements.txt", "src/main.py"),
            contents={"requirements.txt": REQUIREMENTS},
            dependency_specifiers=PYTHON_IMPORTS,
        )
        ids = [c.id for c in ReadinessEngine().evaluate(evidence).checks]
        assert ids.count("dependency_manifest_present") == 1

    def test_a_repository_with_no_dependencies_at_all_is_not_judged(self) -> None:
        """No manifest and no imports means nothing to reconcile, not a failure."""
        ids = {c.id for c in ReadinessEngine().evaluate(IndexEvidence(paths=("README.md",), contents={})).checks}
        assert "every_imported_package_is_declared" not in ids
        assert "dependency_manifest_present" not in ids

    def test_removing_a_dependency_is_not_offered_as_automatic(self) -> None:
        """A package can be required at runtime without appearing in any import.

        A database driver loaded by name is the standard example. Deleting one automatically could break
        a working service in a way that only shows up under load, so this reports and does not act.
        """
        check = self._checks()["no_unused_declared_packages"]
        assert not check.generatable
        assert "loaded by name" in check.blocked_because
