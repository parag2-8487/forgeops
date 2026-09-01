# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent's real scan report must satisfy the backend's real validators.

WHY THIS EXISTS, AND WHAT IT WOULD HAVE CAUGHT

`agent/internal/scanner/wire_contract_test.go` asserts the Go side never emits `null` where the backend
declares a list. It has forbidden `"dependencies":null` since it was written — and the bug survived anyway,
because its fixture tree contains an import, so `Dependencies` was never empty there and the assertion
never fired.

A tree with NO import edges at all — a repository of manifests and data files, which is an ordinary thing
to scan — produced `"dependencies": null`, and the backend rejected the ENTIRE report with a 422. That
surfaced in CI as:

    fatal: submitting the scan report: the backend refused the scan report (422)

losing the index for every file in the tree. The two halves of the contract were each tested against their
own idea of the other, and neither ran the other's code.

So this builds a report with the REAL Go scanner, over a tree chosen to hit the empty cases, and feeds it to
the REAL pydantic model. No fixture JSON: a checked-in sample would be a third statement of the contract to
keep in step, and it would have been written from the same assumption that produced the bug.

The tree is chosen for what it makes EMPTY:

* no import edges anywhere, so `dependencies` is empty;
* a zero-byte file, so one `chunks` list is empty;
* and a variant with no manifests at all, so `frameworks` and `package_managers` are empty.

`frameworks` and `package_managers` are FR-10's fields, and they reintroduced the same defect the day they
landed: nil on a repository with nothing to detect.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from src.analysis.indexer import ScanReportIn

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_DIR = REPO_ROOT / "agent"

pytestmark = pytest.mark.skipif(
    shutil.which("go") is None,
    reason="the Go toolchain is required to build a real report; the agent job always has it",
)


def _dump(tree: Path) -> dict:
    """Run the real scanner over `tree` and return the report it puts on the wire.

    `go run` rather than a prebuilt binary, so the report comes from the working tree's source and this
    cannot pass against a stale build.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["go", "run", "./cmd/reportdump", "-root", str(tree)],
        cwd=str(AGENT_DIR),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "reportdump failed, so the contract was not checked:\n" + completed.stderr.decode("utf-8", "replace")
        )
    return json.loads(completed.stdout.decode("utf-8"))


def _write(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    # `write_bytes`, because `write_text` translates `\n` to `\r\n` on Windows and the content hash is
    # computed over what the scanner reads.
    path.write_bytes(body.encode("utf-8"))


def test_a_report_with_no_dependencies_validates(tmp_path: Path) -> None:
    """The case the Go-side test claimed to cover and never exercised.

    No source file that imports anything: a `manage.py` was in this tree first and produced an edge for
    `django`, which made the assertion vacuous — the same way the Go test's own fixture does.
    """
    _write(tmp_path, "package.json", '{"dependencies": {"express": "^4.18.2"}}\n')
    _write(tmp_path, "pnpm-lock.yaml", "lockfileVersion: 9.0\n")
    _write(tmp_path, "settings.yaml", "debug: false\n")
    _write(tmp_path, ".gitkeep", "")

    raw = _dump(tmp_path)
    assert raw["dependencies"] == [], "the scanner emitted a dependency edge, so this case is not exercised"

    model = ScanReportIn.model_validate(raw)
    assert model.dependencies == []
    assert model.files, "no files were reported, so the report proves nothing"
    # The manifest is still read, so this tree exercises the empty-dependency case WITHOUT also being an
    # empty-inventory case — those are separate tests because they fail for separate reasons.
    assert {f.name for f in model.inventory.frameworks} >= {"Express"}


def test_a_report_with_no_frameworks_validates(tmp_path: Path) -> None:
    """FR-10's fields, on a tree with nothing to detect."""
    _write(tmp_path, "notes.txt", "just prose\n")
    _write(tmp_path, "data/rows.csv", "a,b\n1,2\n")

    raw = _dump(tmp_path)
    assert raw["inventory"]["frameworks"] == [], "a framework was detected, so this case is not exercised"
    assert raw["inventory"]["package_managers"] == []

    model = ScanReportIn.model_validate(raw)
    assert model.inventory.frameworks == []
    assert model.inventory.package_managers == []


def test_no_list_on_the_wire_is_null(tmp_path: Path) -> None:
    """The property stated positively, over the whole document rather than a named set of keys.

    The Go test enumerates forbidden keys, which is precise and needs extending for every field added — and
    was not extended when FR-10 added two. This walks the serialised document instead, so a new list-valued
    field is covered the day it lands.
    """
    _write(tmp_path, "readme.md", "# nothing\n")
    _write(tmp_path, "empty.py", "")

    raw = _dump(tmp_path)

    nulls: list[str] = []

    def walk(node: object, path: str) -> None:
        if node is None:
            nulls.append(path)
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(raw, "")
    assert nulls == [], (
        "the report carries JSON null at "
        + ", ".join(nulls)
        + ". Go marshals a nil slice as null and pydantic refuses null for a list, so the backend rejects "
        "the whole report with a 422 and the index is lost for every file in it."
    )


def test_the_frameworks_a_real_tree_produces_are_accepted(tmp_path: Path) -> None:
    """The other direction: the VALUES the Go side emits must satisfy the backend's own validators.

    `ScanFrameworkIn.kind` and `confidence` are `Literal`s and `evidence` has `min_length=1`. A mismatch in
    any of them is a 422 for the whole report, and no amount of null-checking would find it.
    """
    _write(tmp_path, "go.mod", "module example.com/demo\n\ngo 1.24\n\nrequire github.com/labstack/echo/v4 v4.12.0\n")
    _write(
        tmp_path, "package.json", '{"dependencies": {"react": "^18.0.0"}, "devDependencies": {"vitest": "^1.0.0"}}\n'
    )
    _write(tmp_path, "pyproject.toml", '[project]\nname = "demo"\ndependencies = ["fastapi>=0.110"]\n')
    _write(tmp_path, "Dockerfile", "FROM alpine:3.20\n")
    _write(tmp_path, "cmd/server/serve.go", "package main\n\nfunc main() {}\n")

    raw = _dump(tmp_path)
    findings = raw["inventory"]["frameworks"]
    assert findings, "no frameworks were detected from a tree carrying four manifests"

    model = ScanReportIn.model_validate(raw)
    names = {f.name for f in model.inventory.frameworks}
    # Declared dependencies, one per ecosystem, so a single-language regression is visible.
    assert {"Echo", "React", "Vitest", "FastAPI", "Docker"} <= names, sorted(names)

    for finding in model.inventory.frameworks:
        assert finding.evidence, f"{finding.name} carries no evidence, which the model forbids"
        assert finding.confidence in {"declared", "inferred"}
        assert finding.kind in {"web", "frontend", "build", "test", "runtime"}

    # FR-11 travels on the same document: the structural rule must find the entry point that no
    # filename-matching rule would.
    assert "cmd/server/serve.go" in model.inventory.entry_points
