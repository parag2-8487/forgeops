# SPDX-License-Identifier: FSL-1.1-ALv2
"""The artifacts FR-24 requires must pass the real tools, not just the backend's gate.

FR-24 is P0 and names four things: containerisation, Kubernetes manifests, CI/CD pipelines and
infrastructure as code. Only the first two were generated, so a user asking to make their project
deployable got a Dockerfile and three manifests, nothing that would build the image, and nothing that
would create the cluster the manifests need. The agent's `validate.helm` and `validate.tofu` operations
existed and had nothing to check.

The backend's own gate parses these, which catches a malformed document. It cannot catch a chart that
lints and fails to render, or a module that parses and does not type-check — those need `helm` and
`tofu`. So this file writes the generated artifacts to disk and runs the real binaries over them, which
is the only way to know the platform is producing something a user can use.

`helm template` matters more than `helm lint` here: the generated deployment template calls
`include "<name>.fullname"`, and a chart missing `_helpers.tpl` LINTS CLEANLY and fails to render. That
is exactly the defect a lint-only check would ship.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from src.generation.artifact_checks import validate_artifacts
from src.generation.service import GenerationService


def _require(tool: str) -> None:
    if shutil.which(tool) is not None:
        return
    if os.environ.get("FORGEOPS_REQUIRE_INTEGRATION", "").strip():
        pytest.fail(
            f"FORGEOPS_REQUIRE_INTEGRATION is set but `{tool}` is not on PATH. The agent job installs "
            "kubectl, helm, tofu, trivy and yamllint at pinned versions."
        )
    pytest.skip(f"`{tool}` is not on PATH")


def _write(files: tuple, root: Path) -> None:
    """Write each artifact BYTE FOR BYTE, without translating newlines.

    `Path.write_text` uses the platform's line ending, which on Windows turns every `\\n` the renderers
    emit into `\\r\\n` — and yamllint's `new-lines` rule then reports "wrong new line character" on a
    workflow that is perfectly fine. The platform hands the agent a content string and the agent writes
    it, so the bytes under test here have to be the bytes the renderer produced.
    """
    for artifact in files:
        destination = root / artifact.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(artifact.content.encode("utf-8"))


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, cwd=cwd, capture_output=True, text=True, check=False
    )


@pytest.fixture
def generated(tmp_path: Path) -> tuple:
    files = GenerationService()._render("a python service")
    _write(files, tmp_path)
    return files


def test_the_generated_set_covers_every_kind_fr24_requires(generated: tuple) -> None:
    paths = {artifact.path for artifact in generated}
    assert "Dockerfile" in paths, "containerisation"
    assert any(p.startswith("k8s/") for p in paths), "Kubernetes manifests"
    assert any("/.github/workflows/" in f"/{p}" for p in paths), "CI/CD pipeline"
    assert any(p.endswith("Chart.yaml") for p in paths), "Helm chart"
    assert any(p.endswith(".tf") for p in paths), "infrastructure as code"


def test_every_generated_artifact_passes_the_backend_gate(generated: tuple) -> None:
    """The cheap gate first: if this fails, the tools below would fail for the same reason."""
    assert validate_artifacts(list(generated)) == [], validate_artifacts(list(generated))


def test_the_generated_workflow_passes_yamllint(generated: tuple, tmp_path: Path) -> None:
    _require("yamllint")
    workflow = next(a for a in generated if "/.github/workflows/" in f"/{a.path}")
    config = Path(__file__).resolve().parents[3] / "agent/internal/validator/yamllint-artifacts.yaml"
    assert config.exists(), f"the shared yamllint config is missing at {config}"
    result = _run(["yamllint", "-c", str(config), "-f", "parsable", "--strict", workflow.path], tmp_path)
    assert result.returncode == 0, f"generated workflow fails yamllint:\n{result.stdout}{result.stderr}"


def test_the_generated_chart_lints_and_renders(generated: tuple, tmp_path: Path) -> None:
    """`helm template`, not only `helm lint`. A chart with no `_helpers.tpl` lints and cannot render."""
    _require("helm")
    chart_yaml = next(a for a in generated if a.path.endswith("Chart.yaml"))
    chart_dir = (tmp_path / chart_yaml.path).parent

    lint = _run(["helm", "lint", str(chart_dir)], tmp_path)
    assert lint.returncode == 0, f"helm lint failed:\n{lint.stdout}{lint.stderr}"

    rendered = _run(["helm", "template", "release", str(chart_dir)], tmp_path)
    assert rendered.returncode == 0, f"helm template failed:\n{rendered.stdout}{rendered.stderr}"
    # A render that produces nothing is not a passing render.
    assert "kind: Deployment" in rendered.stdout, rendered.stdout


def test_the_generated_tofu_module_initialises_and_validates(generated: tuple, tmp_path: Path) -> None:
    """`tofu validate` needs provider schemas, so an init is required; `-backend=false` keeps state out."""
    _require("tofu")
    module = next(a for a in generated if a.path.endswith(".tf"))
    module_dir = (tmp_path / module.path).parent

    init = _run(["tofu", "init", "-backend=false", "-input=false", "-no-color"], module_dir)
    if init.returncode != 0:
        # A provider download needs the network. That is an environment fact rather than a defect in
        # the generated module, and reporting it as a failure would make this test a network monitor.
        pytest.skip(f"tofu init could not fetch providers here:\n{init.stdout}{init.stderr}")

    validated = _run(["tofu", "validate", "-no-color"], module_dir)
    assert validated.returncode == 0, f"tofu validate failed:\n{validated.stdout}{validated.stderr}"


def test_the_generated_manifests_declare_named_objects(generated: tuple) -> None:
    """Every manifest must carry a real `metadata.name`, which the substring gate never checked."""
    import yaml

    manifests = [a for a in generated if a.path.startswith("k8s/")]
    assert manifests, "no Kubernetes manifests were generated"
    for artifact in manifests:
        for document in yaml.safe_load_all(artifact.content):
            if document is None:
                continue
            assert document["metadata"]["name"], artifact.path


def test_two_projects_do_not_generate_colliding_names(tmp_path: Path) -> None:
    """The chart and module are named from the project, so two projects must not collide."""
    service = GenerationService()
    first = service._render("a service", {"name": "billing-api", "settings": {}})
    second = service._render("a service", {"name": "search-api", "settings": {}})
    first_charts = {a.path for a in first if "charts/" in a.path}
    second_charts = {a.path for a in second if "charts/" in a.path}
    assert first_charts and second_charts
    assert first_charts.isdisjoint(second_charts), (
        f"two projects generated the same chart paths: {first_charts & second_charts}"
    )
