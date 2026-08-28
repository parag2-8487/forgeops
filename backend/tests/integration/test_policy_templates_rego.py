# SPDX-License-Identifier: FSL-1.1-ALv2
"""The shipped policy templates must be Rego that compiles and rules that fire.

A template is text a user adopts, so a defect in one becomes a defect in their policy. Both shipped
templates had one, and neither was catchable by reading a unit test that only checked the strings were
non-empty:

* the scheduling template hardcoded `_current_weekday = "Saturday"`, so a policy blocking Friday
  deploys governed Saturdays;
* the file-restrictions template compared a path to a *glob* with `==`, so `**/package.json` matched
  nothing, because no file is literally named that.

Both are the same defect class the rest of this project keeps finding: a control that looks present and
does nothing. So this file runs the real `opa` binary over the shipped text and asserts the decisions.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from src.policies.templates import FILE_RESTRICTIONS_TEMPLATE, SCHEDULING_TEMPLATE, TEMPLATES

#: 2026-08-07T02:30:00Z is a Friday in UTC. The same instant the governance bundle's own tests use, so
#: the two suites cannot drift into asserting different calendars.
FRIDAY = "2026-08-07T02:30:00Z"
WEDNESDAY = "2026-08-05T10:00:00Z"


def _evaluate(rego: str, package: str, document: dict[str, Any], tmp_path: Path) -> str:
    """Ask the real `opa` binary for this template's decision."""
    policy = tmp_path / "policy.rego"
    policy.write_text(rego, encoding="utf-8")
    payload = tmp_path / "input.json"
    payload.write_text(json.dumps(document), encoding="utf-8")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "opa",
            "eval",
            "--fail-defined",
            "--format",
            "json",
            "--data",
            str(policy),
            "--input",
            str(payload),
            f"data.{package}.decision",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # `opa eval` exits 1 with --fail-defined when the expression IS defined, which is the normal case
    # here, so the exit code carries no verdict. A compile error shows up on stderr instead.
    if "rego_parse_error" in completed.stderr or "rego_type_error" in completed.stderr:
        pytest.fail(f"the shipped template does not compile:\n{completed.stderr}")
    parsed = json.loads(completed.stdout)
    return str(parsed["result"][0]["expressions"][0]["value"])


@pytest.fixture(autouse=True)
def _needs_opa_binary() -> None:
    """The OPA BINARY, which is a different capability from the OPA server.

    `require_capability("opa")` asks whether the HTTP server is reachable, and these tests do not use
    it: they run `opa eval` over a temporary file. The backend CI job installs the binary from the
    digest-pinned image, so this cannot skip there — and under `FORGEOPS_REQUIRE_INTEGRATION` a missing
    binary is a failure rather than a skip, which is the rule `tests/integration/capability.py` exists
    to enforce.
    """
    if shutil.which("opa") is not None:
        return
    if os.environ.get("FORGEOPS_REQUIRE_INTEGRATION", "").strip():
        pytest.fail(
            "FORGEOPS_REQUIRE_INTEGRATION is set but the `opa` binary is not on PATH. The backend job "
            "installs it from openpolicyagent/opa at a pinned digest."
        )
    pytest.skip("the `opa` binary is not on PATH")


def test_every_shipped_template_compiles(tmp_path: Path) -> None:
    """A template that does not compile is a policy a user cannot save."""
    assert TEMPLATES, "the template list is empty, so this test would assert nothing"
    for index, template in enumerate(TEMPLATES):
        package = f"forgeops.policy.{template.id}"
        assert package in template.rego_rules, (
            f"template {template.id} does not declare {package}; two policies in "
            "`forgeops.governance` would collide with the real bundle rather than extend it"
        )
        directory = tmp_path / str(index)
        directory.mkdir()
        _evaluate(
            template.rego_rules,
            package,
            {"now_rfc3339": WEDNESDAY, "project": {}, "change_items": []},
            directory,
        )


def test_no_template_declares_the_governance_package() -> None:
    """`forgeops.governance` is the real bundle's package and must not be reused.

    Two `default decision` definitions in one package is a compile error, and two partial definitions
    would silently combine — so a published template could corrupt the governance decision itself.
    """
    for template in TEMPLATES:
        assert "package forgeops.governance\n" not in template.rego_rules, template.id


def test_the_scheduling_template_blocks_the_day_it_was_configured_with(tmp_path: Path) -> None:
    """The defect this replaces: the weekday was the constant "Saturday", whatever was configured."""
    document = {
        "now_rfc3339": FRIDAY,
        "project": {"blocked_weekdays": ["Friday"], "timezone": "UTC"},
        "change_items": [],
    }
    assert _evaluate(SCHEDULING_TEMPLATE.rego_rules, "forgeops.policy.scheduling", document, tmp_path) == "deny"


def test_the_scheduling_template_allows_a_day_it_was_not_configured_with(tmp_path: Path) -> None:
    """The control. Without it, a rule that denies everything would pass the test above."""
    document = {
        "now_rfc3339": WEDNESDAY,
        "project": {"blocked_weekdays": ["Friday"], "timezone": "UTC"},
        "change_items": [],
    }
    assert _evaluate(SCHEDULING_TEMPLATE.rego_rules, "forgeops.policy.scheduling", document, tmp_path) == "allow"


def test_the_scheduling_template_does_not_block_saturday_by_default(tmp_path: Path) -> None:
    """Named directly, because Saturday is the day the hardcoded constant used to be."""
    saturday = "2026-08-08T02:30:00Z"
    document = {
        "now_rfc3339": saturday,
        "project": {"blocked_weekdays": ["Friday"], "timezone": "UTC"},
        "change_items": [],
    }
    assert _evaluate(SCHEDULING_TEMPLATE.rego_rules, "forgeops.policy.scheduling", document, tmp_path) == "allow", (
        "Saturday is denied on a policy that names only Friday, so a constant is still in play"
    )


def test_the_file_restrictions_template_matches_a_glob_at_any_depth(tmp_path: Path) -> None:
    """The defect this replaces: `==` against a pattern, which matched no real file."""
    document = {
        "now_rfc3339": WEDNESDAY,
        "project": {"protected_globs": ["**/package.json"]},
        "change_items": [{"path": "services/web/package.json", "action": "modify"}],
    }
    assert (
        _evaluate(
            FILE_RESTRICTIONS_TEMPLATE.rego_rules,
            "forgeops.policy.file_restrictions",
            document,
            tmp_path,
        )
        == "deny"
    )


def test_the_file_restrictions_template_allows_an_unprotected_path(tmp_path: Path) -> None:
    document = {
        "now_rfc3339": WEDNESDAY,
        "project": {"protected_globs": ["**/package.json"]},
        "change_items": [{"path": "src/index.ts", "action": "modify"}],
    }
    assert (
        _evaluate(
            FILE_RESTRICTIONS_TEMPLATE.rego_rules,
            "forgeops.policy.file_restrictions",
            document,
            tmp_path,
        )
        == "allow"
    )


def test_a_template_with_no_parameters_configured_allows(tmp_path: Path) -> None:
    """The state a project starts in. An empty parameter set must be a defined allow, not a deny."""
    for template, package in (
        (SCHEDULING_TEMPLATE, "forgeops.policy.scheduling"),
        (FILE_RESTRICTIONS_TEMPLATE, "forgeops.policy.file_restrictions"),
    ):
        directory = tmp_path / package.replace(".", "_")
        directory.mkdir()
        document = {"now_rfc3339": FRIDAY, "project": {}, "change_items": []}
        assert _evaluate(template.rego_rules, package, document, directory) == "allow", template.id


def test_every_template_declares_the_parameters_its_rules_read() -> None:
    """A template whose Rego reads a parameter it does not declare cannot be configured."""
    for template in TEMPLATES:
        for name in template.parameters:
            assert f"input.project.{name}" in template.rego_rules, (
                f"template {template.id} declares {name} and never reads it"
            )
