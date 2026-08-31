# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every branch of §11.5.5's gate, one case per way an artifact can be wrong.

The gate replaced substring matching, and the value of that change is entirely in the cases a substring
check accepted: a document that mentions the right words, one whose indentation makes it a single scalar,
one whose `metadata` is a string. Each of those is a branch here, stated as the thing it catches rather
than as a line number.

Unit tests rather than integration ones because the subject is a pure function over text. The tool-based
half — `docker compose config`, `helm template`, `tofu validate` — is in
`tests/integration/test_generated_artifacts_validate.py`, and the two answer different questions.
"""

from __future__ import annotations

import pytest
from src.generation.artifact_checks import (
    checker_for,
    validate_artifacts,
    validate_compose,
    validate_dockerfile,
    validate_github_workflow,
    validate_helm_chart_metadata,
    validate_kubernetes,
    validate_opentofu,
)


class _Artifact:
    """The two attributes `validate_artifacts` reads. A stand-in, not a stub of behaviour."""

    def __init__(self, path: str, content: str) -> None:
        self.path = path
        self.content = content


# ── Dockerfile ───────────────────────────────────────────────────────────────────────────────────


def test_a_good_dockerfile_passes() -> None:
    assert validate_dockerfile('FROM alpine:3.20\nUSER 1001\nCMD ["sh"]\n') == []


def test_an_empty_dockerfile_is_reported() -> None:
    assert validate_dockerfile("   \n") == ["Dockerfile is empty"]


def test_a_dockerfile_of_only_comments_is_reported() -> None:
    findings = validate_dockerfile("# FROM alpine\n# USER 1001\n\n")
    assert findings == ["Dockerfile contains no instructions, only comments or blank lines"]


def test_a_commented_from_does_not_satisfy_the_first_instruction() -> None:
    """`startswith("FROM ")` accepted this. The instruction list does not."""
    findings = validate_dockerfile("# FROM alpine:3.20\nRUN echo hi\nUSER 1001\n")
    assert any("first instruction is RUN" in f for f in findings), findings


def test_arg_may_precede_from_and_nothing_else_may() -> None:
    assert validate_dockerfile("ARG V=3.20\nFROM alpine:$V\nUSER 1001\n") == []
    findings = validate_dockerfile("ENV X=1\nFROM alpine:3.20\nUSER 1001\n")
    assert any("first instruction is ENV" in f for f in findings), findings


def test_a_user_inside_a_value_does_not_satisfy_the_user_check() -> None:
    """`"USER " not in dockerfile` accepted this: the word appears in an environment value."""
    findings = validate_dockerfile('FROM alpine:3.20\nENV NOTE="USER 1001 is not set"\nCMD ["sh"]\n')
    assert any("does not drop root" in f for f in findings), findings


def test_instruction_case_is_ignored_as_docker_ignores_it() -> None:
    assert validate_dockerfile("from alpine:3.20\nuser 1001\n") == []


# ── Kubernetes ───────────────────────────────────────────────────────────────────────────────────


def test_a_good_manifest_passes() -> None:
    content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\ndata:\n  k: v\n"
    assert validate_kubernetes(content) == []


def test_a_manifest_that_only_mentions_the_words_is_refused() -> None:
    content = "# apiVersion: v1\n# kind: ConfigMap\nnotes: |\n  metadata: and spec: in a string\n"
    assert validate_kubernetes(content)


def test_unparsable_yaml_is_reported_as_unparsable() -> None:
    findings = validate_kubernetes("apiVersion: v1\n\tkind: bad tab\n")
    assert len(findings) == 1
    assert "not parsable YAML" in findings[0]


def test_an_empty_manifest_declares_no_object() -> None:
    assert validate_kubernetes("---\n# nothing\n") == ["Kubernetes manifest declares no object"]


def test_a_manifest_that_is_a_list_is_not_a_mapping() -> None:
    findings = validate_kubernetes("- apiVersion: v1\n- kind: ConfigMap\n")
    assert any("not a mapping" in f for f in findings), findings


def test_a_missing_metadata_name_is_refused() -> None:
    content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  namespace: x\n"
    findings = validate_kubernetes(content)
    assert any("metadata.name" in f for f in findings), findings


def test_metadata_that_is_a_string_is_refused() -> None:
    findings = validate_kubernetes("apiVersion: v1\nkind: ConfigMap\nmetadata: settings\n")
    assert any("no metadata mapping" in f for f in findings), findings


def test_a_blank_kind_is_refused() -> None:
    content = "apiVersion: v1\nkind: '  '\nmetadata:\n  name: x\n"
    findings = validate_kubernetes(content)
    assert any("no usable kind" in f for f in findings), findings


def test_every_document_of_a_multi_document_manifest_is_checked() -> None:
    content = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: good\n---\napiVersion: v1\nkind: ConfigMap\nmetadata: {}\n"
    )
    findings = validate_kubernetes(content)
    assert any("document 2" in f for f in findings), findings
    assert not any("document 1" in f for f in findings), findings


# ── GitHub Actions ───────────────────────────────────────────────────────────────────────────────

GOOD_WORKFLOW = """---
name: ci
on:
  push:
    branches: [main]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""


def test_a_good_workflow_passes() -> None:
    assert validate_github_workflow(GOOD_WORKFLOW) == []


def test_the_on_key_is_found_despite_yaml_reading_it_as_true() -> None:
    """YAML 1.1 parses a bare `on` as the boolean True, so `document["on"]` finds nothing.

    Both spellings must pass. The quoted form is what the platform generates, precisely so that linters
    stop objecting; the bare form is what most hand-written workflows use, and rejecting those would make
    the gate refuse the majority of real workflows.
    """
    assert validate_github_workflow(GOOD_WORKFLOW) == []
    # Only the top-level key is requoted. Replacing every "on:" would also hit `runs-on:` and break the
    # job, which is how this test failed the first time it ran.
    quoted = GOOD_WORKFLOW.replace("\non:\n", '\n"on":\n')
    assert '"on":' in quoted, "the fixture no longer contains a top-level `on:` to requote"
    assert validate_github_workflow(quoted) == []


def test_a_workflow_with_no_trigger_can_never_run() -> None:
    content = "---\nname: ci\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: x\n"
    findings = validate_github_workflow(content)
    assert any("no `on` trigger" in f for f in findings), findings


def test_a_workflow_whose_jobs_is_a_string_is_refused() -> None:
    findings = validate_github_workflow('---\nname: ci\non:\n  push: {}\njobs: "build it"\n')
    assert any("no jobs" in f for f in findings), findings


def test_a_job_with_no_runner_is_refused() -> None:
    content = "---\non:\n  push: {}\njobs:\n  build:\n    steps:\n      - run: make\n"
    findings = validate_github_workflow(content)
    assert any("no `runs-on`" in f for f in findings), findings


def test_a_reusable_workflow_call_needs_no_runner_or_steps() -> None:
    content = "---\non:\n  push: {}\njobs:\n  call:\n    uses: owner/repo/.github/workflows/x.yml@v1\n"
    assert validate_github_workflow(content) == []


def test_a_job_with_no_steps_is_refused() -> None:
    content = "---\non:\n  push: {}\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
    findings = validate_github_workflow(content)
    assert any("no steps" in f for f in findings), findings


def test_a_step_with_neither_uses_nor_run_is_refused() -> None:
    content = (
        "---\non:\n  push: {}\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - name: does nothing\n"
    )
    findings = validate_github_workflow(content)
    assert any("neither `uses` nor `run`" in f for f in findings), findings


def test_a_job_that_is_not_a_mapping_is_refused() -> None:
    findings = validate_github_workflow("---\non:\n  push: {}\njobs:\n  build: nonsense\n")
    assert any("not a mapping" in f for f in findings), findings


def test_a_step_that_is_not_a_mapping_is_refused() -> None:
    content = "---\non:\n  push: {}\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - just a string\n"
    findings = validate_github_workflow(content)
    assert any("not a mapping" in f for f in findings), findings


def test_an_empty_or_unparsable_workflow_is_reported() -> None:
    assert validate_github_workflow("") == ["GitHub Actions workflow is empty"]
    assert any("not parsable" in f for f in validate_github_workflow("a: 1\n\tb: 2\n"))
    assert validate_github_workflow("- a\n- b\n") == ["GitHub Actions workflow is not a mapping"]


# ── Helm ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_good_chart_passes() -> None:
    assert validate_helm_chart_metadata("apiVersion: v2\nname: app\nversion: 1.2.3\n") == []


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "latest", "", "1.2.3.4"])
def test_a_non_semver_chart_version_is_refused(version: str) -> None:
    """Helm REQUIRES SemVer 2 rather than preferring it."""
    content = f"apiVersion: v2\nname: app\nversion: {version}\n" if version else "apiVersion: v2\nname: app\n"
    findings = validate_helm_chart_metadata(content)
    assert any("SemVer" in f for f in findings), (version, findings)


@pytest.mark.parametrize("version", ["1.2.3", "0.1.0", "1.2.3-rc1", "1.2.3+build5"])
def test_valid_semver_forms_are_accepted(version: str) -> None:
    assert validate_helm_chart_metadata(f"apiVersion: v2\nname: app\nversion: {version}\n") == []


def test_a_chart_with_a_bad_api_version_or_no_name_is_refused() -> None:
    findings = validate_helm_chart_metadata("apiVersion: v3\nname: app\nversion: 1.0.0\n")
    assert any("apiVersion" in f for f in findings), findings
    findings = validate_helm_chart_metadata("apiVersion: v2\nversion: 1.0.0\n")
    assert any("no name" in f for f in findings), findings


def test_an_empty_or_unparsable_chart_is_reported() -> None:
    assert validate_helm_chart_metadata("---\n") == ["Chart.yaml declares no chart"]
    assert any("not parsable" in f for f in validate_helm_chart_metadata("a: 1\n\tb: 2\n"))


# ── OpenTofu ─────────────────────────────────────────────────────────────────────────────────────


def test_a_configuration_with_a_top_level_block_passes() -> None:
    assert validate_opentofu('variable "x" {\n  type = string\n}\n') == []


def test_an_empty_configuration_is_reported() -> None:
    assert validate_opentofu("  \n") == ["OpenTofu configuration is empty"]


def test_prose_with_no_block_is_refused() -> None:
    findings = validate_opentofu("this file explains the infrastructure\n")
    assert any("no top-level block" in f for f in findings), findings


def test_unbalanced_braces_are_refused() -> None:
    findings = validate_opentofu('resource "kubernetes_namespace" "x" {\n  metadata {\n')
    assert findings == ["OpenTofu configuration has unbalanced braces"]


# ── Compose ──────────────────────────────────────────────────────────────────────────────────────


def test_a_good_compose_file_passes() -> None:
    assert validate_compose('services:\n  web:\n    image: nginx\n    ports:\n      - "80:80"\n') == []


def test_a_compose_file_with_no_services_is_refused() -> None:
    assert validate_compose("version: '3'\n") == ["Compose file declares no services"]
    assert validate_compose("services: {}\n") == ["Compose file declares no services"]


def test_a_service_with_neither_image_nor_build_is_refused() -> None:
    findings = validate_compose('services:\n  web:\n    ports:\n      - "80:80"\n')
    assert any("neither an image nor a build" in f for f in findings), findings


def test_a_service_with_a_build_and_no_image_passes() -> None:
    assert validate_compose("services:\n  web:\n    build: .\n") == []


def test_a_service_that_is_not_a_mapping_is_refused() -> None:
    findings = validate_compose("services:\n  web: nginx\n")
    assert any("not a mapping" in f for f in findings), findings


def test_ports_that_are_not_a_list_are_refused() -> None:
    findings = validate_compose("services:\n  web:\n    image: nginx\n    ports:\n      host: 80\n")
    assert any("not a list" in f for f in findings), findings


def test_an_unparsable_or_empty_compose_file_is_reported() -> None:
    assert any("not parsable" in f for f in validate_compose("a: 1\n\tb: 2\n"))
    assert validate_compose("---\n") == ["Compose file declares nothing"]
    assert validate_compose("- a\n") == ["Compose file declares nothing"]


# ── dispatch by path ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Dockerfile", validate_dockerfile),
        ("docker-compose.yml", validate_compose),
        ("docker-compose.yaml", validate_compose),
        ("k8s/deployment.yaml", validate_kubernetes),
        ("infra/k8s/service.yaml", validate_kubernetes),
        (".github/workflows/ci.yml", validate_github_workflow),
        ("charts/app/Chart.yaml", validate_helm_chart_metadata),
        ("infra/main.tf", validate_opentofu),
    ],
)
def test_each_generated_kind_has_its_checker(path: str, expected: object) -> None:
    assert checker_for(path) is expected, path


def test_windows_separators_resolve_identically() -> None:
    """The agent ships for windows/amd64, so the same repository must validate the same way."""
    assert checker_for(r".github\workflows\ci.yml") is validate_github_workflow
    assert checker_for(r"charts\app\Chart.yaml") is validate_helm_chart_metadata


def test_an_unrecognised_path_gets_no_invented_rule() -> None:
    """Blocking a file for failing a rule nobody wrote is worse than not checking it."""
    for path in ("README.md", "src/index.ts", "notes.txt", "LICENSE"):
        assert checker_for(path) is None, path


def test_validate_artifacts_prefixes_every_finding_with_its_path() -> None:
    findings = validate_artifacts(
        [
            _Artifact("Dockerfile", "RUN echo hi\n"),
            _Artifact("k8s/deployment.yaml", "---\n# nothing\n"),
            _Artifact("README.md", "anything at all"),
        ]
    )
    assert findings
    assert all(f.startswith(("Dockerfile: ", "k8s/deployment.yaml: ")) for f in findings), findings
    # An unrecognised path contributes nothing rather than being blocked.
    assert not any(f.startswith("README.md") for f in findings)


def test_validate_artifacts_reports_every_finding_not_the_first() -> None:
    """A repair iteration is handed this list; one problem at a time turns one repair into three."""
    findings = validate_artifacts([_Artifact("Dockerfile", 'RUN echo hi\nCMD ["x"]\n')])
    assert len(findings) >= 2, findings


def test_validate_artifacts_over_nothing_is_empty() -> None:
    assert validate_artifacts([]) == []
